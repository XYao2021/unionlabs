#!/usr/bin/env python3
"""
marl_phy.py — bridge between a MARL random-access policy and the real SDR PHY.

Replaces the MARL simulator's idealized channel with the USRP B210s. The policy
calls three primitives instead of the simulator's channel model:

    sense()            -> channel occupancy (busy/idle, power_db)   [channel_sense]
    transmit_once(pkt) -> bool  (ACK = success / timeout = collision-or-loss)
    AccessPoint        -> a receiver that ACKs each decoded frame (the AP)

SINGLE-SHOT semantics (see ../applications/MARL_RA_Union/INTEGRATION.md §2): the
reliable stop-and-wait ARQ is deliberately NOT used here. For random access the
collision/loss must reach the policy as a *missing ACK* — so we send exactly ONE
burst (`--max-attempts 1`) and let the learned policy own retransmission (a failed
packet stays queued and is retried on a future decision). ACK within the timeout
=> success; no ACK => collision/loss. That boolean is the RL reward signal.

Topology (2 B210s): one radio is the Access Point (RX+ACK), one is an agent (TX).
Real collisions need >=3 radios (N agents + 1 AP); with 2 you validate the loop
and create the busy/collision signal with an external interferer (e.g. tx_tone).

STATUS (validated on hardware): single-shot semantics work — a warm, settled AP
ACKs one 32-byte burst on the first attempt (`Sent=1 Unacked=0`). But the MVP's
per-attempt model (a fresh sink_arq per packet) is UNRELIABLE because a cold sink
isn't settled when the lone burst arrives (its round-trip exceeds the ACK window).
=> A PERSISTENT, warm AP is required, not optional (INTEGRATION.md §6). The clean
next step is a session-based design: one long-lived source<->sink connection that
ACKs successive on-demand packets (a warm sink serves many bursts fine — proven
by the 16-chunk message test). transmit_once()/AccessPoint here are the correct
API and are fine for warm-link smoke tests; wire the policy onto the persistent
session once it exists.
"""
import os
import re
import shlex
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sdr  # noqa: E402
# Re-export the sensing helpers so a policy needs only this one module.
from channel_sense import sense_channel, calibrate_floor, SenseStream  # noqa: E402,F401

# A MARL "packet" is just a token that either gets through or collides — its
# content is irrelevant to the RL, so any fixed size works (must match on both
# ends). Use 125 B: the validated differential-scheme chunk size. (Differential +
# FEC currently mis-frames at very small chunks like 32 B -> CRC always fails.)
PACKET_BYTES = 125
_ACK_UNACKED = re.compile(r"Unacked chunks=(\d+)")


def _phy(a):
    """Shared PHY options for the single-shot random-access link. Default scheme is
    DQPSK: differential encoding is robust to the free-running-clock carrier offset
    (data rides the phase *difference*), so single bursts decode where coherent QPSK
    fails (~83% vs ~17% delivery on this rig without a shared clock)."""
    return dict(
        scheme=a.get("scheme", "DQPSK"), waveform="sc", fec=True,
        rx_freq=a.get("freq", 915e6), tx_freq=a.get("freq", 915e6),
        rx_rate=1.6e6, tx_rate=1.6e6,
        rx_subdev="A:A", tx_subdev="A:A", rx_ant="RX2", tx_ant="TX/RX",
        det_mult=3, ack_transport="tcp", ack_port=a.get("ack_port", 5599),
        bytes_length=a.get("packet_bytes", PACKET_BYTES),
        timer_interval=20, viz=False,
    )


def _scratch(tag):
    return os.path.join(tempfile.gettempdir(), "marl_phy_%s.bin" % tag)


# ─────────────────────────────────────────────────────────────────────────────
#  Agent side: transmit exactly one burst, report ACK (success) vs timeout
# ─────────────────────────────────────────────────────────────────────────────
def transmit_once(payload=None, tx_args="serial=30CD424", tx_gain=85,
                  ack_host="127.0.0.1", timeout_ms=2000, binary=None, **opts):
    """Send ONE frame at a warm Access Point; return True iff it was ACKed (no
    collision/loss). No PHY retransmission — `--max-attempts 1`. `payload` defaults
    to a fixed-size token. Retries once on a USB-release race (the previous fire's
    source not fully closed yet)."""
    import time
    pkt = payload if payload is not None else b"MARL" + bytes(PACKET_BYTES - 4)
    tmp = _scratch("tx")
    with open(tmp, "wb") as f:
        f.write(pkt)
    cmd = sdr.SDR(role="source_arq", tx_args=tx_args, rx_args=tx_args, tx_gain=tx_gain,
                  ack_host=ack_host, timeout=timeout_ms, max_attempts=1,
                  payload_file=tmp, binary=binary, **_phy(opts)).command()
    for attempt in range(2):
        p = subprocess.run(shlex.split(cmd), capture_output=True, text=True)
        m = _ACK_UNACKED.search(p.stdout)
        if m:
            return int(m.group(1)) == 0          # 0 unacked => ACKed => success
        if "No devices found" in (p.stdout + p.stderr) and attempt == 0:
            time.sleep(3)                        # TX radio not released yet — wait, retry
            continue
        raise RuntimeError("transmit_once: no ARQ result\n" + (p.stderr or p.stdout)[-500:])


# ─────────────────────────────────────────────────────────────────────────────
#  Access-Point side: receive + ACK each decoded frame
# ─────────────────────────────────────────────────────────────────────────────
class AccessPoint:
    """The persistent, WARM receiver the agents transmit to. Runs ONE
    `sink_arq --serve-forever` process: the radio starts once and stays settled,
    re-accepting a source per fire and ACKing each decoded frame. A collision/
    garble => no decode => no ACK (the agent sees a timeout). This is what makes
    fire-on-demand single-shot reliable (a cold, per-frame sink misses the burst)."""

    def __init__(self, rx_args="serial=30CD3F7", rx_gain=20, ack_port=5599,
                 ber_expected=None, binary=None, **opts):
        self.rx_args = rx_args
        self.rx_gain = rx_gain
        self.opts = {**opts, "ack_port": ack_port}
        self.binary = binary
        self._p = None
        # ber_expected: known TX payload (bytes) or a file path -> per-burst BER
        self.ber_file = None
        if ber_expected is not None:
            if isinstance(ber_expected, (bytes, bytearray)):
                self.ber_file = _scratch("ber_expected")
                with open(self.ber_file, "wb") as f:
                    f.write(ber_expected)
            else:
                self.ber_file = ber_expected

    def start(self, warmup_s=6.0, log="/tmp/marl_ap_sink.log"):
        """Launch the persistent warm sink and wait `warmup_s` for the radio to
        settle (AGC + noise floor) before returning — a fire against a cold sink
        is missed. Output goes to `log` for inspection. Call once; keep it running."""
        import time
        extra = {"ber_expected": self.ber_file} if self.ber_file else {}
        cmd = sdr.SDR(role="sink_arq", rx_args=self.rx_args, tx_args=self.rx_args,
                      rx_gain=self.rx_gain, serve_forever=True, binary=self.binary,
                      **extra, **_phy(self.opts)).command()
        self._log = open(log, "w") if log else subprocess.DEVNULL
        self._p = subprocess.Popen(shlex.split(cmd), stdout=self._log,
                                   stderr=subprocess.STDOUT)
        time.sleep(warmup_s)                     # let the RX pipeline warm up
        if self._p.poll() is not None:
            raise RuntimeError("AccessPoint failed to start (see %s)" % log)
        return self

    def stop(self):
        if self._p and self._p.poll() is None:
            self._p.terminate()
            try:
                self._p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._p.kill()

    def serve(self, seconds=None):
        """Run the warm AP until Ctrl-C (or `seconds`). It ACKs every decodable
        fire from any agent while it's up."""
        import time
        self.start()
        print("[AP] persistent warm access point on %s (Ctrl-C to stop)" % self.rx_args)
        try:
            t0 = time.time()
            while seconds is None or time.time() - t0 < seconds:
                if self._p.poll() is not None:
                    print("[AP] sink exited"); break
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n[AP] stopping")
        finally:
            self.stop()

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()


# ─────────────────────────────────────────────────────────────────────────────
#  Warm transmitter: radio stays warm, fires ONE packet per fire() (no re-init)
# ─────────────────────────────────────────────────────────────────────────────
class WarmSource:
    """Persistent WARM transmitter — the agent-side dual of AccessPoint. Runs one
    `source_arq --on-demand` process: the radio starts once and stays warm, and
    each fire() sends exactly ONE packet (a fresh ACK connection per fire lets a
    --serve-forever AP re-accept) and returns True/False from the process's
    'RESULT acked=' line. No ~2 s radio re-init per packet.

        with WarmSource(tx_args="serial=30CD424") as tx:
            acked = tx.fire()      # one burst on command; True=ACK, False=collision/loss
    """

    def __init__(self, payload=None, tx_args="serial=30CD424", tx_gain=85,
                 ack_host="127.0.0.1", timeout_ms=2000, binary=None,
                 warmup_s=6.0, **opts):
        import time
        pkt = payload if payload is not None else b"MARL" + bytes(PACKET_BYTES - 4)
        tmp = _scratch("warmtx")
        with open(tmp, "wb") as f:
            f.write(pkt)
        cmd = sdr.SDR(role="source_arq", tx_args=tx_args, rx_args=tx_args, tx_gain=tx_gain,
                      ack_host=ack_host, timeout=timeout_ms, max_attempts=1, on_demand=True,
                      payload_file=tmp, binary=binary, **_phy(opts)).command()
        self._p = subprocess.Popen(shlex.split(cmd), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, bufsize=1)
        time.sleep(warmup_s)                     # let the TX radio warm up
        if self._p.poll() is not None:
            raise RuntimeError("WarmSource failed to start")

    def fire(self):
        """Send ONE packet now; return True if ACKed (success), False otherwise."""
        self._p.stdin.write("go\n")
        self._p.stdin.flush()
        for line in self._p.stdout:              # read until the RESULT line
            m = re.search(r"RESULT acked=(\d)", line)
            if m:
                return m.group(1) == "1"
        raise RuntimeError("WarmSource: process ended without a RESULT")

    def close(self):
        if self._p and self._p.poll() is None:
            try:
                self._p.stdin.close()            # EOF -> the C++ loop exits
            except Exception:
                pass
            try:
                self._p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._p.kill()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# ─────────────────────────────────────────────────────────────────────────────
#  BER probe: measure real per-burst bit-error-rate over the link
# ─────────────────────────────────────────────────────────────────────────────
_BER_LINE = re.compile(
    r"\[BER\] pre-FEC=([\d.]+)%.*?post-FEC payload=([\d.]+)%.*?CRC=(\w+)")


def ber_probe(n=10, payload=None, tx_args="serial=30CD424", rx_args="serial=30CD3F7",
              tx_gain=85, rx_gain=40, scheme="DQPSK", period_s=2.0,
              ap_log="/tmp/marl_ap_sink.log", binary=None, **opts):
    """Measure per-burst BER over the link. Runs a warm AP with a KNOWN payload as
    ground truth (--ber-expected), fires `n` copies of it, and parses the sink's
    [BER] lines. Returns a list of {pre_fec, post_fec, crc} and prints min/median/max
    of the pre-FEC (channel) and post-FEC (payload) BER. Answers 'how corrupt are the
    CRC-failed frames' — low BER = nearly right, high BER = garbage."""
    import time
    pkt = payload if payload is not None else b"MARL" + bytes(PACKET_BYTES - 4)
    ap = AccessPoint(rx_args=rx_args, rx_gain=rx_gain, ber_expected=pkt, scheme=scheme,
                     binary=binary, **opts)
    ap.start(log=ap_log)
    try:
        for _ in range(n):
            try:
                transmit_once(payload=pkt, tx_args=tx_args, tx_gain=tx_gain,
                              scheme=scheme, binary=binary, **opts)
            except RuntimeError:
                pass
            time.sleep(period_s)
    finally:
        ap.stop()

    rows = []
    for line in open(ap_log):
        m = _BER_LINE.search(line)
        if m:
            rows.append({"pre_fec": float(m.group(1)), "post_fec": float(m.group(2)),
                         "crc": m.group(3)})
    if not rows:
        print("[ber_probe] 0 bursts decoded of %d fired — link too weak to detect "
              "(move radios closer / raise gain / better window)" % n)
        return rows
    import statistics as st
    pre = [r["pre_fec"] for r in rows]
    post = [r["post_fec"] for r in rows]
    npass = sum(r["crc"] == "PASS" for r in rows)
    print("[ber_probe] %d/%d fired bursts decoded  |  CRC pass %d/%d"
          % (len(rows), n, npass, len(rows)))
    print("[ber_probe] pre-FEC  (channel) BER  min/median/max = %.2f / %.2f / %.2f %%"
          % (min(pre), st.median(pre), max(pre)))
    print("[ber_probe] post-FEC (payload) BER  min/median/max = %.2f / %.2f / %.2f %%"
          % (min(post), st.median(post), max(post)))
    return rows


# ─────────────────────────────────────────────────────────────────────────────
#  Agent facade: sense + transmit, for a policy to call each decision epoch
# ─────────────────────────────────────────────────────────────────────────────
class MarlRadio:
    """Agent-side facade: one object a policy uses per decision epoch.
        r = MarlRadio(tx_args=..., rx_args=...)
        obs_busy = r.sense()['busy']          # channel occupancy for the observation
        if policy_says_go:
            acked = r.transmit()              # one burst; True=success, False=collision
    """

    def __init__(self, tx_args="serial=30CD424", rx_args="serial=30CD3F7",
                 tx_gain=85, rx_gain=20, threshold_db=None, ack_host="127.0.0.1",
                 timeout_ms=2000, **opts):
        self.tx_args = tx_args
        self.rx_args = rx_args
        self.tx_gain = tx_gain
        self.rx_gain = rx_gain
        self.ack_host = ack_host
        self.timeout_ms = timeout_ms
        self.opts = opts
        self.threshold_db = threshold_db

    def calibrate(self, **kw):
        self.threshold_db = calibrate_floor(rx_args=self.rx_args, rx_gain=self.rx_gain, **kw)
        return self.threshold_db

    def sense(self):
        thr = self.threshold_db if self.threshold_db is not None else -30.0
        return sense_channel(threshold_db=thr, rx_args=self.rx_args, rx_gain=self.rx_gain)

    def transmit(self, payload=None):
        return transmit_once(payload=payload, tx_args=self.tx_args, tx_gain=self.tx_gain,
                             ack_host=self.ack_host, timeout_ms=self.timeout_ms, **self.opts)


# ─────────────────────────────────────────────────────────────────────────────
def main(argv):
    try:
        sys.stdout.reconfigure(line_buffering=True)   # live progress when piped to a file
    except Exception:
        pass
    import argparse
    a = argparse.ArgumentParser(description="MARL <-> SDR PHY bridge (self-test)")
    a.add_argument("role", choices=["ap", "agent", "ber"])
    a.add_argument("--tx-args", default="serial=30CD424")
    a.add_argument("--rx-args", default="serial=30CD3F7")
    a.add_argument("--tx-gain", type=float, default=85)
    a.add_argument("--rx-gain", type=float, default=20)
    a.add_argument("--attempts", type=int, default=5, help="agent/ber: number of fires")
    a.add_argument("--scheme", default="DQPSK", help="modulation (must match both ends)")
    a.add_argument("--seconds", type=float, default=None, help="ap: run for N s (else Ctrl-C)")
    a.add_argument("--warm", action="store_true",
                   help="agent: keep the TX radio warm (WarmSource, fire on command)")
    args = a.parse_args(argv)

    if args.role == "ber":
        # single-box BER probe: runs its own warm AP + fires known packets
        ber_probe(n=args.attempts, tx_args=args.tx_args, rx_args=args.rx_args,
                  tx_gain=args.tx_gain, rx_gain=args.rx_gain, scheme=args.scheme)
    elif args.role == "ap":
        AccessPoint(rx_args=args.rx_args, rx_gain=args.rx_gain,
                    scheme=args.scheme).serve(seconds=args.seconds)
    elif args.warm:
        import time
        ok = 0
        with WarmSource(tx_args=args.tx_args, tx_gain=args.tx_gain,
                        scheme=args.scheme) as tx:
            for i in range(args.attempts):
                acked = tx.fire()
                ok += acked
                print("[agent] fire %d -> %s" % (i + 1, "ACK (success)" if acked
                                                 else "no ACK (collision/loss)"))
                time.sleep(1)                    # radio stays warm; short gap between fires
        print("[agent] %d/%d ACKed (warm source)" % (ok, args.attempts))
    else:
        import time
        ok = 0
        for i in range(args.attempts):
            acked = transmit_once(tx_args=args.tx_args, tx_gain=args.tx_gain,
                                  scheme=args.scheme)
            ok += acked
            print("[agent] fire %d -> %s" % (i + 1, "ACK (success)" if acked
                                             else "no ACK (collision/loss)"))
            time.sleep(3)                        # per-attempt: let the TX radio release
        print("[agent] %d/%d ACKed" % (ok, args.attempts))


if __name__ == "__main__":
    main(sys.argv[1:])
