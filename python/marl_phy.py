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
# content is irrelevant to the RL, so fix a small size (must match on both ends).
PACKET_BYTES = 32
_ACK_UNACKED = re.compile(r"Unacked chunks=(\d+)")


def _phy(a):
    """Shared PHY options for the single-shot random-access link."""
    return dict(
        scheme=a.get("scheme", "QPSK"), waveform="sc", fec=True,
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
def transmit_once(payload=None, tx_args="serial=30CD424", tx_gain=78,
                  ack_host="127.0.0.1", timeout_ms=1500, binary=None, **opts):
    """Send ONE frame; return True iff it was ACKed (no collision/loss). No PHY
    retransmission — `--max-attempts 1`. `payload` defaults to a fixed-size token."""
    pkt = payload if payload is not None else b"MARL" + bytes(PACKET_BYTES - 4)
    tmp = _scratch("tx")
    with open(tmp, "wb") as f:
        f.write(pkt)
    cmd = sdr.SDR(role="source_arq", tx_args=tx_args, rx_args=tx_args, tx_gain=tx_gain,
                  ack_host=ack_host, timeout=timeout_ms, max_attempts=1,
                  payload_file=tmp, binary=binary, **_phy(opts)).command()
    p = subprocess.run(shlex.split(cmd), capture_output=True, text=True)
    m = _ACK_UNACKED.search(p.stdout)
    if not m:                                    # source never reached "Done" (radio error)
        raise RuntimeError("transmit_once: no ARQ result\n" + (p.stderr or p.stdout)[-500:])
    return int(m.group(1)) == 0                  # 0 unacked => ACKed => success


# ─────────────────────────────────────────────────────────────────────────────
#  Access-Point side: receive + ACK each decoded frame
# ─────────────────────────────────────────────────────────────────────────────
class AccessPoint:
    """The receiver the agents transmit to. Listens for one frame, ACKs it if it
    decodes (CRC OK), and returns its bytes. A collision/garble => no decode =>
    no ACK (the transmitting agent then sees a timeout). Loop it with serve()."""

    def __init__(self, rx_args="serial=30CD3F7", rx_gain=20, ack_port=5599,
                 binary=None, **opts):
        self.rx_args = rx_args
        self.rx_gain = rx_gain
        self.opts = {**opts, "ack_port": ack_port}
        self.binary = binary
        self.tmp = _scratch("ap")

    def receive_once(self, timeout_s=30.0):
        """Wait up to timeout_s for one frame; ACK it and return its bytes, or None
        if nothing decoded in time."""
        if os.path.exists(self.tmp):
            os.remove(self.tmp)
        cmd = sdr.SDR(role="sink_arq", rx_args=self.rx_args, tx_args=self.rx_args,
                      rx_gain=self.rx_gain, out_file=self.tmp, binary=self.binary,
                      **_phy(self.opts)).command()
        try:
            subprocess.run(shlex.split(cmd), capture_output=True, text=True,
                           timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return None                          # no frame arrived in the window
        if os.path.exists(self.tmp):
            with open(self.tmp, "rb") as f:
                return f.read()
        return None

    def serve(self, on_packet=None, max_packets=None, timeout_s=30.0):
        """Persistent AP: ACK frames forever (or max_packets), calling on_packet(bytes)
        for each. Ctrl-C to stop. (Re-inits the radio per frame in this MVP.)"""
        n = 0
        print("[AP] serving on %s (Ctrl-C to stop)" % self.rx_args)
        try:
            while max_packets is None or n < max_packets:
                pkt = self.receive_once(timeout_s=timeout_s)
                if pkt is None:
                    print("[AP] (idle window, no frame)")
                    continue
                n += 1
                print("[AP] frame %d ACKed (%d bytes)" % (n, len(pkt)))
                if on_packet:
                    on_packet(pkt)
        except KeyboardInterrupt:
            print("\n[AP] stopped after %d frames" % n)
        return n


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
                 tx_gain=78, rx_gain=30, threshold_db=None, ack_host="127.0.0.1",
                 timeout_ms=1500, **opts):
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
    a.add_argument("role", choices=["ap", "agent"])
    a.add_argument("--tx-args", default="serial=30CD424")
    a.add_argument("--rx-args", default="serial=30CD3F7")
    a.add_argument("--tx-gain", type=float, default=78)
    a.add_argument("--rx-gain", type=float, default=20)
    a.add_argument("--attempts", type=int, default=5, help="agent: transmit_once trials")
    a.add_argument("--packets", type=int, default=None, help="ap: stop after N frames")
    args = a.parse_args(argv)

    if args.role == "ap":
        AccessPoint(rx_args=args.rx_args, rx_gain=args.rx_gain).serve(max_packets=args.packets)
    else:
        ok = 0
        for i in range(args.attempts):
            acked = transmit_once(tx_args=args.tx_args, tx_gain=args.tx_gain)
            ok += acked
            print("[agent] attempt %d -> %s" % (i + 1, "ACK (success)" if acked
                                                 else "no ACK (collision/loss)"))
        print("[agent] %d/%d ACKed" % (ok, args.attempts))


if __name__ == "__main__":
    main(sys.argv[1:])
