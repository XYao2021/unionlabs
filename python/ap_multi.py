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

The ACK-routing logic is radio-free and self-testable:
    python3 ap_multi.py --self-test          # validates routing with a fake decoder

Hardware (tomorrow): `python3 ap_multi.py --agents 2 --scheme QPSK --rx-args serial=30CD3F7`
The sink-decode reader (`_decode_stream`) is the one part that needs the radios in the
loop — it is isolated so the routing above can be trusted independently.
"""
import argparse
import os
import socket
import sys
import threading


class MultiAckServer:
    """TCP server holding one persistent connection per agent. ack(agent_id) sends an
    ACK to that agent. Agents register by sending a line 'HELLO <id>' on connect."""

    def __init__(self, num_agents, port=5599):
        self.N = num_agents
        self.port = port
        self._conns = {}                       # agent_id -> file object
        self._lock = threading.Lock()
        self._srv = None
        self._acks = {i: 0 for i in range(num_agents)}

    def start(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("0.0.0.0", self.port))
        self._srv.listen(self.N)
        threading.Thread(target=self._accept_loop, daemon=True).start()
        print("[ap_multi] ACK server on :%d — waiting for %d agents" % (self.port, self.N))

    def _accept_loop(self):
        while len(self._conns) < self.N:
            conn, addr = self._srv.accept()
            f = conn.makefile("rwb")
            line = f.readline().decode().strip()
            if line.startswith("HELLO"):
                aid = int(line.split()[1])
                with self._lock:
                    self._conns[aid] = f
                print("[ap_multi] agent %d connected (%d/%d)" % (aid, len(self._conns), self.N))

    def wait_all(self, timeout=120):
        import time as _t
        t0 = 0
        while len(self._conns) < self.N and t0 < timeout:
            _t.sleep(0.2); t0 += 0.2
        return len(self._conns) == self.N

    def ack(self, agent_id):
        """Send an ACK to one agent (routed by id from the decoded payload)."""
        with self._lock:
            f = self._conns.get(agent_id)
        if f is None:
            return False
        try:
            f.write(b"ACK\n"); f.flush()
            self._acks[agent_id] += 1
            return True
        except Exception:
            return False

    def close(self):
        for f in list(self._conns.values()):
            try:
                f.close()
            except Exception:
                pass
        if self._srv:
            self._srv.close()


class MultiAP:
    """The full multi-agent AP: C++ sink decoder + MultiAckServer routing."""

    def __init__(self, num_agents, rx_args="serial=30CD3F7", rx_gain=20,
                 scheme="QPSK", ack_port=5599, binary=None):
        self.N = num_agents
        self.rx_args = rx_args
        self.rx_gain = rx_gain
        self.scheme = scheme
        self.binary = binary
        self.acks = MultiAckServer(num_agents, port=ack_port)
        self._sink = None

    def _decode_stream(self):
        """HARDWARE: run the C++ sink as a decoder and yield the agent id (payload
        byte 0) of every successfully decoded burst. Isolated so the routing above is
        trusted without radios. Uses role rx with a per-burst binary out-file; a
        collision decodes nothing -> yields nothing -> no ACK."""
        import shlex
        import subprocess
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import sdr
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "applications", "MARL_RA_Union", "results", ".ap_rx.bin")
        cmd = sdr.SDR(role="rx", rx_args=self.rx_args, rx_gain=self.rx_gain,
                      rx_freq=915e6, rx_rate=1.6e6, rx_ant="RX2", rx_subdev="A:A",
                      scheme=self.scheme, fec=True, det_mult=3, viz=False,
                      stop_on_complete=False, out_file=out, binary=self.binary).command()
        self._sink = subprocess.Popen(shlex.split(cmd), stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True, bufsize=1)
        # The sink prints a line per decoded burst; parse the agent id from the payload
        # it writes. (Exact marker depends on the rx role's per-burst output — validate
        # and adjust against the real sink output tomorrow.)
        import re
        pat = re.compile(r"payload_len=|Received chunk|\[SINK\]")
        for line in self._sink.stdout:
            if pat.search(line):
                try:
                    with open(out, "rb") as fh:
                        data = fh.read()
                    if data:
                        yield data[0] & 0xFF        # agent id = payload byte 0
                except Exception:
                    pass

    def serve(self):
        self.acks.start()
        if not self.acks.wait_all():
            print("[ap_multi] not all agents connected — aborting"); return
        print("[ap_multi] all agents up — decoding + routing ACKs (Ctrl-C to stop)")
        try:
            for agent_id in self._decode_stream():
                self.acks.ack(agent_id)
        except KeyboardInterrupt:
            pass
        finally:
            if self._sink:
                self._sink.terminate()
            self.acks.close()


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


def main(argv):
    a = argparse.ArgumentParser(description="Multi-agent Access Point (ACK routing by agent id)")
    a.add_argument("--self-test", action="store_true", help="radio-free routing test")
    a.add_argument("--agents", type=int, default=2)
    a.add_argument("--rx-args", default="serial=30CD3F7")
    a.add_argument("--rx-gain", type=float, default=20)
    a.add_argument("--scheme", default="QPSK")
    a.add_argument("--ack-port", type=int, default=5599)
    args = a.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    MultiAP(args.agents, rx_args=args.rx_args, rx_gain=args.rx_gain,
            scheme=args.scheme, ack_port=args.ack_port).serve()


if __name__ == "__main__":
    main(sys.argv[1:])
