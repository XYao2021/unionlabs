#!/usr/bin/env python3
"""
ap_multi.py — MULTI-AGENT Access Point for the decentralized setup.

The C++ ACK server is single-client (`net.hpp: listen(srv, 1)`), so it cannot ACK N
concurrent agents. This keeps ALL the multi-client logic in Python and uses the C++
sink purely as the PHY **decoder**:

    C++ sink (role rx, decode + report each burst's payload)
          │  each decoded burst  ->  payload[0] = agent id
          ▼
    ap_multi  ── MultiAckServer: N agents each hold a TCP connection; on a decoded
                 burst it sends "ACK" to THAT agent's connection (routed by id).
                 A collision decodes nothing -> no report -> no ACK -> the agent
                 times out and treats the slot as a collision/loss (the RL signal).

Agents (agent_node.py, RealMediumClient) transmit a one-way ID-tagged burst and wait
for their ACK on this connection. So the reward path is: transmit -> (decoded alone)
ACK / (collided or lost) timeout — exactly the shared-medium semantics, with N agents.

Per-burst agent id: the C++ `role rx --marl-report` prints ONE machine line per CRC-OK
burst — `[BURST] id=<payload byte0> idx=<i> tot=<t> nbytes=<n> hex=<HEX>` — and
`_decode_stream` parses `id=` from it (via `_parse_burst_id`). This is race-free (stdout,
not a file written only at exit) and works for binary/random payloads (the id byte is
usually non-printable). This whole feature is ADDITIVE: `--marl-report` defaults off, so
`role rx` / `sink_arq` / `source_arq` are unchanged; multi-client ACK stays in Python.

Radio-free tests (no devices needed):
    python3 ap_multi.py --self-test   # ACK routing with a synthetic id stream
    python3 ap_multi.py --sim-test    # parser unit test + end-to-end: fake C++ sink
                                      #   subprocess -> _decode_stream -> ACK routing

Hardware: `python3 ap_multi.py --agents 2 --scheme QPSK --rx-args serial=30CD3F7`
Only the real RF decode needs the radios; the parse + routing path is fully sim-validated.
"""
import argparse
import os
import re
import socket
import sys
import threading


# Matches the C++ `role rx --marl-report` per-burst line:
#   [BURST] id=<payload byte0> idx=<i> tot=<t> nbytes=<n> hex=<HEX>
_BURST_RE = re.compile(r"^\[BURST\]\s+id=(-?\d+)\b")


def _parse_burst_id(line):
    """Extract the agent id from one C++ sink output line. Returns the id (>=0) for a
    CRC-OK burst whose payload byte 0 identifies the transmitting agent, or None for
    any non-burst line OR an empty-payload burst (id=-1, no agent id to route to).
    Pure and radio-free so the routing can be trusted independently of the radios."""
    m = _BURST_RE.match(line.strip())
    if not m:
        return None
    aid = int(m.group(1))
    return aid if aid >= 0 else None


class MultiAckServer:
    """TCP server holding one persistent connection per agent. ack(agent_id) sends an
    ACK to that agent. Agents register with 'HELLO <id>' on connect; in slotted mode
    they also send 'TX' each slot they transmit, delivered to the on_intent callback."""

    def __init__(self, num_agents, port=5599, on_intent=None):
        self.N = num_agents
        self.port = port
        self._conns = {}                       # agent_id -> raw socket
        self._lock = threading.Lock()
        self._srv = None
        self._acks = {i: 0 for i in range(num_agents)}
        self.on_intent = on_intent             # called with agent_id on a 'TX' line

    def start(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("0.0.0.0", self.port))
        self._srv.listen(self.N)
        threading.Thread(target=self._accept_loop, daemon=True).start()
        print("[ap_multi] ACK server on :%d — waiting for %d agents" % (self.port, self.N))

    @staticmethod
    def _readline(sock, buf):
        """Read one '\\n'-terminated line from a raw socket, buffering the remainder in
        buf (a single-element list). Returns the decoded line (no newline) or None on
        close/error."""
        while b"\n" not in buf[0]:
            try:
                data = sock.recv(4096)
            except OSError:
                return None
            if not data:
                return None
            buf[0] += data
        line, buf[0] = buf[0].split(b"\n", 1)
        return line.decode("utf-8", "replace").strip()

    def _accept_loop(self):
        while len(self._conns) < self.N:
            conn, addr = self._srv.accept()
            buf = [b""]
            line = self._readline(conn, buf)
            if line and line.startswith("HELLO"):
                aid = int(line.split()[1])
                with self._lock:
                    self._conns[aid] = conn
                print("[ap_multi] agent %d connected (%d/%d)" % (aid, len(self._conns), self.N))
                # Per-agent reader: subsequent 'TX' lines are per-slot transmit intents.
                threading.Thread(target=self._reader, args=(aid, conn, buf), daemon=True).start()

    def _reader(self, aid, conn, buf):
        while True:
            line = self._readline(conn, buf)
            if line is None:
                break
            if line.startswith("TX") and self.on_intent is not None:
                self.on_intent(aid)

    def wait_all(self, timeout=120):
        import time as _t
        t0 = 0
        while len(self._conns) < self.N and t0 < timeout:
            _t.sleep(0.2); t0 += 0.2
        return len(self._conns) == self.N

    def ack(self, agent_id):
        """Send an ACK to one agent (routed by id / slot arbitration)."""
        with self._lock:
            s = self._conns.get(agent_id)
        if s is None:
            print("[ap_multi] would ACK agent %s but no such agent connected — skipped"
                  % agent_id, flush=True)
            return False
        try:
            s.sendall(b"ACK\n")
            self._acks[agent_id] += 1
            print("[ap_multi] ACK sent -> agent %d  (agent %d total ACKs: %d)"
                  % (agent_id, agent_id, self._acks[agent_id]), flush=True)
            return True
        except Exception as e:
            print("[ap_multi] agent %d ACK send FAILED: %s" % (agent_id, e), flush=True)
            return False

    def close(self):
        for s in list(self._conns.values()):
            try:
                s.close()
            except Exception:
                pass
        if self._srv:
            self._srv.close()


class MultiAP:
    """The full multi-agent AP: C++ sink decoder + MultiAckServer routing."""

    def __init__(self, num_agents, rx_args="serial=30CD3F7", rx_gain=20,
                 scheme="QPSK", ack_port=5599, binary=None,
                 rx_subdev="A:A", rx_ant="RX2", rx_freq=915e6,
                 rx_rate=1.6e6, symbol_rate=None, bytes_length=125,
                 slot_host=None, slot_port=5600):
        self.N = num_agents
        self.rx_args = rx_args
        self.rx_gain = rx_gain
        self.scheme = scheme
        self.binary = binary
        # RX front-end geometry: defaults are B210 (subdev A:A). An N210 AP needs
        # rx_subdev="A:0". Rate defaults to 1.6e6 (B210), but an N210 must use an
        # N210-exact rate (2e6 = 100/50) with a matching symbol_rate (1e6), since
        # 1.6e6 snaps on the N210's 100 MHz clock and would mismatch the TX agents.
        self.rx_subdev = rx_subdev
        self.rx_ant = rx_ant
        self.rx_freq = rx_freq
        self.rx_rate = rx_rate
        self.symbol_rate = symbol_rate
        self.bytes_length = bytes_length
        # Slotted logical-collision mode (opt-in): if slot_host is set, the AP joins
        # the shared clock and arbitrates each slot — >=2 transmit intents => collision
        # (slot PASSED, no ACK to anyone); exactly 1 intent whose burst decoded => ACK.
        self.slot_host = slot_host
        self.slot_port = slot_port
        self._intents = set()                  # agents that sent 'TX' this slot
        self._decoded = set()                  # agents whose burst decoded this slot
        self._slot_lock = threading.Lock()
        self.acks = MultiAckServer(num_agents, port=ack_port,
                                   on_intent=self._note_intent if slot_host else None)
        self._sink = None

    def _note_intent(self, agent_id):
        with self._slot_lock:
            self._intents.add(agent_id)

    def _arbitrate(self, slot):
        """Called at each slot boundary: decide the slot that just ended. >=2 intents =
        logical collision (no ACK); exactly 1 intent whose burst decoded = real ACK;
        1 intent no decode = real loss (no ACK). Then reset for the next slot."""
        with self._slot_lock:
            intents = set(self._intents)
            decoded = set(self._decoded)
            self._intents.clear()
            self._decoded.clear()
        if len(intents) >= 2:
            print("[ap_multi] slot %d COLLISION — agents %s transmitted; slot PASSED (no ACK)"
                  % (slot, sorted(intents)), flush=True)
        elif len(intents) == 1:
            a = next(iter(intents))
            if a in decoded:
                self.acks.ack(a)               # lone transmitter, burst decoded -> deliver
            else:
                print("[ap_multi] slot %d: agent %d transmitted alone but burst did NOT decode "
                      "(real loss) — no ACK" % (slot, a), flush=True)
        # len(intents) == 0 -> idle slot, nothing to do

    def _sink_argv(self):
        """Build the C++ sink command: a WARM per-burst decoder (role rx) that never
        auto-stops (stop_on_complete=False, rx_idle_timeout=0) and prints a machine
        line per CRC-OK burst (--marl-report). No C++ ACK — the ACK is routed in
        Python by agent id, so role rx (one-way decode) is exactly right."""
        import shlex
        import sys as _sys
        _sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "drivers", "usrp_uhd", "python"))
        import sdr
        # tx_rate MUST be set even though this is receive-only: every process runs
        # the full rate-chain consistency check (tx_rate == symbol_rate*U/D), and it
        # defaults to 1.6e6 — which FAILS when rx_rate is overridden to an N210 rate.
        kw = dict(role="rx", rx_args=self.rx_args, rx_gain=self.rx_gain,
                  rx_freq=self.rx_freq, rx_rate=self.rx_rate, tx_rate=self.rx_rate,
                  rx_ant=self.rx_ant, rx_subdev=self.rx_subdev,
                  scheme=self.scheme, fec=True, det_mult=3, viz=False,
                  stop_on_complete=False, rx_idle_timeout=0, marl_report=True,
                  bytes_length=self.bytes_length, binary=self.binary)
        if self.symbol_rate is not None:
            kw["symbol_rate"] = self.symbol_rate
        cmd = sdr.SDR(**kw).command()
        return shlex.split(cmd)

    def _decode_stream(self, sink_argv=None):
        """Run the sink and yield the agent id (payload byte 0) of every CRC-OK burst,
        parsed from its '[BURST]' line. A collision decodes nothing -> no line ->
        yields nothing -> no ACK -> the agent times out (its collision signal).
        `sink_argv` lets a test inject a fake decoder so this exact parse+yield path
        runs radio-free; when None it launches the real C++ sink."""
        import subprocess
        argv = sink_argv if sink_argv is not None else self._sink_argv()
        # Inherit stderr (stderr=None) so the sink's UHD / device-open / [CONSISTENCY]
        # errors are VISIBLE. A silent DEVNULL here hid a decoder that exits at
        # startup (e.g. N210 not reachable, rate snapped) — the AP just quit.
        # Read stdout as BINARY (not text=True): the rx role also prints the decoded
        # payload as a quoted string, and a DQPSK payload is raw non-UTF-8 bytes that
        # would crash a text decoder. We decode leniently and parse only [BURST] lines
        # (pure ASCII), so a binary payload byte can never break the loop.
        self._sink = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=None)
        for raw in iter(self._sink.stdout.readline, b""):
            aid = _parse_burst_id(raw.decode("utf-8", "replace"))
            if aid is not None:
                yield aid
        rc = self._sink.wait()
        print("[ap_multi] WARNING: the sink decoder exited (rc=%s) before any burst — "
              "see its output above. Common causes: N210 not found at these --rx-args, "
              "the rate snapped, or a [CONSISTENCY] failure." % rc, file=sys.stderr)

    def serve(self):
        self.acks.start()
        if not self.acks.wait_all():
            print("[ap_multi] not all agents connected — aborting"); return
        if self.slot_host:
            return self._serve_slotted()
        print("[ap_multi] all agents up — decoding + routing ACKs immediately (async, Ctrl-C to stop)")
        try:
            for agent_id in self._decode_stream():
                self.acks.ack(agent_id)
        except KeyboardInterrupt:
            pass
        finally:
            if self._sink:
                self._sink.terminate()
            self.acks.close()

    def _serve_slotted(self):
        """Slotted logical-collision mode. A decode thread records which agents' bursts
        decoded in the current slot; intents arrive via MultiAckServer.on_intent; and
        this loop (driven by the shared slot clock) arbitrates each slot at its boundary.
        No ACK is sent mid-slot — only at arbitration — so a collision deterministically
        passes the slot regardless of RF-overlap timing."""
        import threading as _th
        from slot_sync import SlotClient
        # Decode thread: tag each decoded burst into the current slot's decoded set.
        def _decode_loop():
            for agent_id in self._decode_stream():
                with self._slot_lock:
                    self._decoded.add(agent_id)
        _th.Thread(target=_decode_loop, daemon=True).start()
        slot = SlotClient(self.slot_host, self.slot_port)
        print("[ap_multi] SLOTTED mode — joined clock at %s:%d; arbitrating per slot (Ctrl-C to stop)"
              % (self.slot_host, self.slot_port), flush=True)
        prev = None
        try:
            while True:
                k = slot.wait_slot()
                if k is None:
                    print("[ap_multi] slot clock closed — stopping", flush=True); break
                # A new tick k means slot (k-1) just ended: arbitrate it.
                if prev is not None:
                    self._arbitrate(prev)
                prev = k
        except KeyboardInterrupt:
            pass
        finally:
            slot.close()
            if self._sink:
                self._sink.terminate()
            self.acks.close()


class AgentAckClient:
    """Agent side of the multi-agent AP protocol (the counterpart to MultiAckServer).

    Holds a PERSISTENT socket to ap_multi (registers 'HELLO <id>'), and each fire()
    transmits ONE id-tagged ONE-WAY burst (C++ role tx, NO ARQ) then waits up to
    ack_timeout for the AP's 'ACK'. Returns True (delivered) / False (timeout =
    collision or loss) — exactly the MARL reward signal. This is what makes agent_node
    interoperate with ap_multi; the C++ source_arq ACK is a different (binary) protocol.

    NOTE: role tx --tx-reps 1 re-inits the radio each fire (~2 s). Fine to prove the
    loop; a warm 'role tx --on-demand' (a C++ addition) would make fires ~100-300 ms."""

    def __init__(self, agent_id, tx_args, ap_host="127.0.0.1", ap_port=5599,
                 tx_gain=85, scheme="DQPSK", ack_timeout=3.0, binary=None,
                 warmup_s=6.0, launch_tx=True, announce_intent=False, **opts):
        import sys as _sys
        import tempfile
        _sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "drivers", "usrp_uhd", "python"))
        import sdr
        from marl_phy import known_payload, PACKET_BYTES, _phy
        self.id = agent_id
        self._ack_timeout = ack_timeout
        # Slotted mode: send a 'TX' intent to the AP each fire so it can arbitrate the
        # slot (logical collision). ack_timeout is then set ~1 slot by the caller.
        self._announce = announce_intent
        self._p = None
        # id-tagged payload (byte 0 = agent id) -> temp file for --payload-file
        n = opts.get("packet_bytes", PACKET_BYTES)
        pkt = bytearray(known_payload(n))
        pkt[0] = agent_id & 0xFF
        self._pkt_file = os.path.join(tempfile.gettempdir(), "ap_agent%d.bin" % agent_id)
        with open(self._pkt_file, "wb") as f:
            f.write(bytes(pkt))
        # WARM one-way transmitter: the radio opens ONCE and stays warm; each fire()
        # sends one burst from a settled LO (reliable single-shot, vs a cold re-open
        # per burst). Reuse _phy for a consistent PHY; drop the ARQ-only ack keys.
        phy = _phy(dict(scheme=scheme, **opts))
        for k in ("ack_transport", "ack_port", "timer_interval"):
            phy.pop(k, None)
        cmd = sdr.SDR(role="tx", tx_args=tx_args, tx_gain=tx_gain, on_demand=True,
                      payload_file=self._pkt_file, binary=binary, **phy).command()
        if launch_tx:
            import shlex
            import subprocess
            import time
            self._p = subprocess.Popen(shlex.split(cmd), stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       text=True, bufsize=1)
            time.sleep(warmup_s)                      # let the TX radio warm up
            if self._p.poll() is not None:
                raise RuntimeError("AgentAckClient: warm TX failed to start")
        # Persistent ACK socket to the AP (raw socket; ap_multi sends "ACK\n").
        self._s = socket.create_connection((ap_host, ap_port))
        self._s.sendall(("HELLO %d\n" % agent_id).encode())

    def _transmit_once(self):
        """Fire ONE warm burst: send 'go' to the TX process, wait for its 'SENT'."""
        self._p.stdin.write("go\n")
        self._p.stdin.flush()
        for line in self._p.stdout:
            if line.strip() == "SENT":
                return
        raise RuntimeError("AgentAckClient: warm TX ended without SENT")

    def fire(self):
        """Transmit one id-tagged burst; return True iff the AP ACKs within timeout."""
        import select
        # Drop any stale ACKs from earlier fires so a late ACK can't be miscounted.
        while select.select([self._s], [], [], 0)[0]:
            try:
                if not self._s.recv(4096):
                    break
            except OSError:
                break
        if self._announce:
            try:
                self._s.sendall(b"TX\n")     # per-slot transmit intent for arbitration
            except OSError:
                pass
        self._transmit_once()
        if not select.select([self._s], [], [], self._ack_timeout)[0]:
            print("[agent %d] burst sent -> NO ACK (waited %.2fs: collision or loss)"
                  % (self.id, self._ack_timeout), flush=True)
            return False                     # no ACK -> collision / loss
        try:
            acked = b"ACK" in self._s.recv(4096)
        except OSError:
            acked = False
        print("[agent %d] burst sent -> %s" % (self.id, "ACK received (delivered)"
              if acked else "NO ACK (collision or loss)"), flush=True)
        return acked

    def close(self):
        try:
            if self._p is not None and self._p.poll() is None:
                self._p.stdin.close()        # EOF -> the C++ warm-TX loop exits
                self._p.wait(timeout=3)
        except Exception:
            try:
                self._p.kill()
            except Exception:
                pass
        try:
            self._s.close()
        except Exception:
            pass


def _self_test():
    """Radio-free: start the ACK server, connect N fake agents, feed a synthetic
    decode stream (some solo bursts, some 'collisions' = nothing decoded), and verify
    each agent gets exactly its own ACKs. Validates the routing logic."""
    import time
    N = 3
    ap = MultiAckServer(N, port=5699)
    ap.start()
    got = {i: 0 for i in range(N)}
    conns = []

    def fake_agent(aid):
        s = socket.create_connection(("127.0.0.1", 5699))
        f = s.makefile("rwb")
        f.write(("HELLO %d\n" % aid).encode()); f.flush()
        conns.append((s, f))
        while True:
            line = f.readline()
            if not line:
                break
            if line.strip() == b"ACK":
                got[aid] += 1

    for i in range(N):
        threading.Thread(target=fake_agent, args=(i,), daemon=True).start()
    ap.wait_all(timeout=5)
    # synthetic decode stream: agent ids that "decoded alone" (None = collision, skipped)
    stream = [0, 1, 2, 0, None, 1, None, 2, 2, 0]
    expect = {0: 3, 1: 2, 2: 3}
    for aid in stream:
        if aid is not None:
            ap.ack(aid)
        time.sleep(0.02)
    time.sleep(0.3)
    ok = all(got[i] == expect[i] for i in range(N))
    print("[self-test] delivered ACKs per agent: %s  expected: %s  ->  %s"
          % (got, expect, "PASS" if ok else "FAIL"))
    ap.close()
    return ok


def _parse_unit_test():
    """Unit-test the pure parser on realistic C++ lines (incl. the real format,
    noise lines, and an empty-payload burst that must be skipped)."""
    cases = [
        ("[BURST] id=0 idx=0 tot=1 nbytes=125 hex=00AB12", 0),
        ("[BURST] id=1 idx=0 tot=1 nbytes=125 hex=01CD", 1),
        ("[BURST] id=17 idx=2 tot=4 nbytes=125 hex=11FF", 17),
        ("  [BURST] id=2 idx=0 tot=1 nbytes=8 hex=02", 2),          # leading space
        ("[BURST] id=-1 idx=0 tot=1 nbytes=0 hex=", None),          # empty payload
        ("[RX] chunk 1/1  [CRC OK, new]  (1/1 verified)", None),    # normal rx line
        ("[USRP RX] OVERFLOW detected!", None),
        ("random noise", None),
    ]
    ok = all(_parse_burst_id(line) == want for line, want in cases)
    print("[parse-test] %s" % ("PASS" if ok else "FAIL"))
    if not ok:
        for line, want in cases:
            got = _parse_burst_id(line)
            print("   %-55r  got=%s want=%s %s"
                  % (line, got, want, "" if got == want else "<-- MISMATCH"))
    return ok


def _sim_test():
    """Radio-free END-TO-END: a FAKE sink subprocess emits real-format [BURST] lines
    (interleaved with noise and 'collision' gaps = no line), driven through the ACTUAL
    _decode_stream parser + MultiAckServer routing. Verifies each agent gets exactly
    its own ACKs — the whole hardware path except the radio."""
    import time
    N = 3
    # Fake sink: prints assorted stdout lines then exits. ids 0/1/2 decode; the id=-1
    # line (empty payload) and the noise lines must NOT produce an ACK.
    lines = [
        "[RX] One-way receive (CRC-verified), scheme QPSK.",
        "[BURST] id=0 idx=0 tot=1 nbytes=125 hex=00AB",
        "[BURST] id=1 idx=0 tot=1 nbytes=125 hex=01CD",
        "some noise line from the sink",
        "[BURST] id=2 idx=0 tot=1 nbytes=125 hex=02EF",
        "[BURST] id=0 idx=0 tot=1 nbytes=125 hex=0099",   # a duplicate from agent 0
        "[BURST] id=-1 idx=0 tot=1 nbytes=0 hex=",        # empty payload -> skipped
        "[BURST] id=1 idx=0 tot=1 nbytes=125 hex=0111",
        "[BURST] id=2 idx=0 tot=1 nbytes=125 hex=0222",
        "[BURST] id=2 idx=0 tot=1 nbytes=125 hex=0233",
    ]
    prog = "import sys,time\n" + "".join(
        "print(%r); sys.stdout.flush(); time.sleep(0.02)\n" % l for l in lines)
    argv = [sys.executable, "-c", prog]

    ap = MultiAP(N, ack_port=5899)
    ap.acks.start()
    got = {i: 0 for i in range(N)}

    def fake_agent(aid):
        s = socket.create_connection(("127.0.0.1", 5899))
        f = s.makefile("rwb")
        f.write(("HELLO %d\n" % aid).encode()); f.flush()
        while True:
            line = f.readline()
            if not line:
                break
            if line.strip() == b"ACK":
                got[aid] += 1

    for i in range(N):
        threading.Thread(target=fake_agent, args=(i,), daemon=True).start()
    if not ap.acks.wait_all(timeout=5):
        print("[sim-test] agents did not all connect -> FAIL"); return False

    for aid in ap._decode_stream(sink_argv=argv):   # real parse + route path
        ap.acks.ack(aid)
    time.sleep(0.3)

    expect = {0: 2, 1: 2, 2: 3}                      # id=-1 + noise produce no ACK
    ok = all(got[i] == expect[i] for i in range(N))
    print("[sim-test] delivered ACKs per agent: %s  expected: %s  ->  %s"
          % (got, expect, "PASS" if ok else "FAIL"))
    ap.acks.close()
    return ok


def _agent_sim_test():
    """Radio-free: AgentAckClient <-> MultiAckServer handshake. Monkeypatch the RF
    transmit so a 'delivered' fire makes the AP ACK (after a short decode delay) and a
    'collision' fire makes it stay silent; verify fire() returns True on ACK and False
    on timeout. Exercises the real HELLO + wait-for-ACK path without radios."""
    import threading
    srv = MultiAckServer(1, port=5999)
    srv.start()
    ag = AgentAckClient(0, tx_args="serial=TEST", ap_host="127.0.0.1", ap_port=5999,
                        ack_timeout=0.8, launch_tx=False)   # no real radio in the sim
    if not srv.wait_all(timeout=5):
        print("[agent-sim] agent did not register -> FAIL"); return False

    def patch(deliver):
        def _tx():
            if deliver:
                threading.Timer(0.05, lambda: srv.ack(0)).start()   # AP decodes -> ACK
        ag._transmit_once = _tx

    patch(True);  d1 = ag.fire()      # decoded alone -> ACK
    patch(False); d2 = ag.fire()      # collision/loss -> no ACK -> timeout
    patch(True);  d3 = ag.fire()      # decoded again -> ACK (no drift after a timeout)
    ok = (d1 is True and d2 is False and d3 is True)
    print("[agent-sim] fire() results=%s  expected=[True, False, True]  ->  %s"
          % ([d1, d2, d3], "PASS" if ok else "FAIL"))
    ag.close(); srv.close()
    return ok


def _slot_sim_test():
    """Radio-free: drive MultiAP's per-slot arbitration directly and verify the logical
    rules — collision (>=2 intents) passes the slot with NO ACK even if one burst decoded
    (capture), a lone decoded transmitter is ACKed, and a lone transmitter whose burst did
    not decode gets NO ACK (real loss)."""
    import time
    N = 2
    ap = MultiAP(N, ack_port=5911, slot_host="dummy")   # slot_host enables intent wiring
    ap.acks.start()
    got = {i: 0 for i in range(N)}

    def fake_agent(aid):
        s = socket.create_connection(("127.0.0.1", 5911))
        s.sendall(("HELLO %d\n" % aid).encode())
        while True:
            try:
                data = s.recv(4096)
            except OSError:
                break
            if not data:
                break
            got[aid] += data.count(b"ACK")

    for i in range(N):
        threading.Thread(target=fake_agent, args=(i,), daemon=True).start()
    if not ap.acks.wait_all(timeout=5):
        print("[slot-sim] agents did not connect -> FAIL"); return False

    # slot 0: agent 0 alone, burst decoded -> ACK 0
    ap._note_intent(0); ap._decoded.add(0); ap._arbitrate(0)
    # slot 1: agents 0 AND 1 both transmit (0 even decoded via capture) -> COLLISION, no ACK
    ap._note_intent(0); ap._note_intent(1); ap._decoded.add(0); ap._arbitrate(1)
    # slot 2: agent 1 alone but burst did NOT decode (real loss) -> no ACK
    ap._note_intent(1); ap._arbitrate(2)
    time.sleep(0.3)

    ok = (got[0] == 1 and got[1] == 0)
    print("[slot-sim] ACKs per agent: %s  expected {0: 1, 1: 0} (collision + loss suppressed)  ->  %s"
          % (got, "PASS" if ok else "FAIL"))
    ap.acks.close()
    return ok


def main(argv):
    a = argparse.ArgumentParser(description="Multi-agent Access Point (ACK routing by agent id)")
    a.add_argument("--self-test", action="store_true", help="radio-free routing test")
    a.add_argument("--sim-test", action="store_true",
                   help="radio-free end-to-end test: fake sink -> parser -> ACK routing")
    a.add_argument("--agents", type=int, default=2)
    a.add_argument("--rx-args", default="serial=30CD3F7")
    a.add_argument("--rx-gain", type=float, default=20)
    a.add_argument("--scheme", default="QPSK")
    a.add_argument("--ack-port", type=int, default=5599)
    a.add_argument("--rx-subdev", default="A:A", help="B210=A:A, N210=A:0")
    a.add_argument("--rx-ant", default="RX2")
    a.add_argument("--rx-freq", type=float, default=915e6)
    a.add_argument("--rx-rate", type=float, default=1.6e6,
                   help="N210 AP must use an N210-exact rate, e.g. 2e6 (=100/50)")
    a.add_argument("--symbol-rate", type=float, default=None,
                   help="set with a non-default --rx-rate so rx_rate==symbol_rate*U/D (e.g. 1e6 for 2e6)")
    a.add_argument("--bytes-length", type=int, default=125,
                   help="payload bytes per packet — MUST match the agents (default 125)")
    a.add_argument("--slot-host", default=None,
                   help="host running slot_sync.py; set to run SLOTTED (logical collision). Omit = async immediate-ACK")
    a.add_argument("--slot-port", type=int, default=5600)
    args = a.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    if args.sim_test:
        ok = (_parse_unit_test() and _sim_test() and _agent_sim_test()
              and _slot_sim_test())
        sys.exit(0 if ok else 1)
    MultiAP(args.agents, rx_args=args.rx_args, rx_gain=args.rx_gain,
            scheme=args.scheme, ack_port=args.ack_port,
            rx_subdev=args.rx_subdev, rx_ant=args.rx_ant, rx_freq=args.rx_freq,
            rx_rate=args.rx_rate, symbol_rate=args.symbol_rate,
            bytes_length=args.bytes_length,
            slot_host=args.slot_host, slot_port=args.slot_port).serve()


if __name__ == "__main__":
    main(sys.argv[1:])
