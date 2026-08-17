#!/usr/bin/env python3
"""
real_channel.py — a reusable REAL-RADIO channel to drop in place of a simulated
random-access channel (MARL, ALOHA/CSMA studies, or any future experiment).

Callable like the detector (`channel_sense`): it composes the three warm SDR
primitives into one object an experiment holds per node.

    sense()      -> {'busy': bool, 'power_db': float}   real channel occupancy
    transmit()   -> bool   (ACK = delivered / no-ACK = collision-or-loss)
    step(action) -> {'busy', 'power_db', 'delivered'}   one decision epoch
    AccessPoint  -> the receiver that ACKs each frame (run on the AP node)

Everything stays WARM (radio started once), so a decision is just a sense read +
(maybe) one burst — no per-packet radio re-init. Semantics are single-shot: one
burst per transmit, ACK=success, timeout=collision/loss; the *policy* owns
retransmission (see ../experiments/marl_ra/INTEGRATION.md).

Topology (a B210 is exclusive to one process):
  - AP node    : AccessPoint on one radio (RX + ACK, always warm).
  - Agent node : RealChannel(tx_args=<agent radio>) — warm transmitter.
  - Sensing needs its OWN listen radio (sense_rx_args), distinct from tx_args and
    the AP. On a 2-radio rig, either add a 3rd radio for sensing or run the agent
    transmit-only (sense() returns None) and take the occupancy from the AP.

Usage:
    # AP node (one terminal):
    from real_channel import AccessPoint
    AccessPoint(rx_args="serial=30CD3F7").serve()

    # Agent node (another terminal / node):
    from real_channel import RealChannel
    with RealChannel(tx_args="serial=30CD424", sense_rx_args=None) as ch:
        out = ch.step(action=policy(obs))     # sense + maybe transmit
        # out['busy'] -> observation, out['delivered'] -> reward signal
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "drivers", "usrp", "python"))
from marl_phy import WarmSource, AccessPoint          # noqa: E402,F401
from channel_sense import SenseStream, calibrate_floor  # noqa: E402,F401


class RealChannel:
    """Reusable real-radio channel for the transmitting agent's side. Keeps a warm
    transmitter (always, if tx_args) and an optional warm sense feed (if
    sense_rx_args is a distinct radio). Context-managed: starts warm on enter,
    stops the radios on exit."""

    def __init__(self, tx_args="serial=30CD424", sense_rx_args=None,
                 tx_gain=85, rx_gain=20, threshold_db=None, ack_host="127.0.0.1",
                 timeout_ms=2000, warmup_s=6.0, binary=None, **opts):
        self.threshold_db = threshold_db
        self._tx = None
        self._sense = None
        if tx_args:
            self._tx = WarmSource(tx_args=tx_args, tx_gain=tx_gain, ack_host=ack_host,
                                  timeout_ms=timeout_ms, warmup_s=warmup_s,
                                  binary=binary, **opts)
        if sense_rx_args:
            if sense_rx_args == tx_args:
                raise ValueError("sense_rx_args must be a DIFFERENT radio than tx_args "
                                 "(a B210 is exclusive per process)")
            self._sense = SenseStream(rx_args=sense_rx_args, rx_gain=rx_gain, binary=binary)

    def calibrate(self, **kw):
        """Set the busy threshold from the current (assumed-idle) sense feed."""
        if self._sense is not None:
            self.threshold_db = self._sense.calibrate(**kw)
        return self.threshold_db

    def sense(self):
        """Real channel occupancy, or None if this node has no sense radio."""
        if self._sense is None:
            return None
        r = self._sense.latest()
        thr = self.threshold_db if self.threshold_db is not None else -30.0
        return {"busy": r["power_db"] > thr, "power_db": r["power_db"]}

    def transmit(self):
        """Fire ONE packet now; True = delivered (ACK), False = collision/loss."""
        if self._tx is None:
            raise RuntimeError("RealChannel has no transmitter (tx_args not set)")
        return self._tx.fire()

    def step(self, action):
        """One decision epoch. action: 1 = transmit, 0 = defer. Returns
        {'busy', 'power_db', 'delivered'} (delivered is None if the agent deferred)."""
        s = self.sense()
        delivered = self.transmit() if action else None
        return {"busy": (s["busy"] if s else None),
                "power_db": (s["power_db"] if s else None),
                "delivered": delivered}

    def close(self):
        if self._tx is not None:
            self._tx.close()
        if self._sense is not None:
            self._sense.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def main(argv):
    import argparse
    import time
    a = argparse.ArgumentParser(description="Real-radio channel (self-test)")
    a.add_argument("role", choices=["ap", "agent"])
    a.add_argument("--tx-args", default="serial=30CD424")
    a.add_argument("--rx-args", default="serial=30CD3F7")   # AP radio
    a.add_argument("--sense-rx-args", default=None, help="agent: distinct listen radio")
    a.add_argument("--tx-gain", type=float, default=85)
    a.add_argument("--rx-gain", type=float, default=20)
    a.add_argument("--scheme", default="DQPSK", help="modulation; MUST match on both ends")
    a.add_argument("--steps", type=int, default=6)
    a.add_argument("--seconds", type=float, default=None)
    args = a.parse_args(argv)

    if args.role == "ap":
        AccessPoint(rx_args=args.rx_args, rx_gain=args.rx_gain,
                    scheme=args.scheme).serve(seconds=args.seconds)
        return
    # agent: a few epochs of "always transmit" to exercise the channel
    ok = 0
    with RealChannel(tx_args=args.tx_args, sense_rx_args=args.sense_rx_args,
                     tx_gain=args.tx_gain, rx_gain=args.rx_gain, scheme=args.scheme) as ch:
        if args.sense_rx_args:
            ch.calibrate()
        for i in range(args.steps):
            out = ch.step(action=1)
            ok += bool(out["delivered"])
            busy = "" if out["busy"] is None else " busy=%s" % out["busy"]
            print("[epoch %d] transmit -> %s%s" % (
                i + 1, "delivered" if out["delivered"] else "collision/loss", busy))
            time.sleep(1)
        print("[agent] delivered %d/%d" % (ok, args.steps))


if __name__ == "__main__":
    main(sys.argv[1:])
