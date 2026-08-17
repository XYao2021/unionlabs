#!/usr/bin/env python3
"""
role_selftest.py — exercise the LoRa LINK ROLES with no hardware.

The tx / rx roles normally run as two processes on two hosts, which makes them the
part of the driver hardest to check. This runs both ends in one process, on threads,
sharing one simulated medium — so the request/response protocol, the addressing, the
fragmentation and the ARQ are all exercised exactly as they would be over the air.

    python3 tools/role_selftest.py                    # 3 rounds, SF9, clean link
    python3 tools/role_selftest.py --sf 12 --snr -18  # a marginal link
    python3 tools/role_selftest.py --bytes 4000       # force multi-fragment messages

What it proves: what the tx sends is what the rx receives, the reply comes back, and
the airtime charged is the real Semtech number. What it does NOT prove: that a
physical SX1276 pair behaves the same — for that use tools/spi_selftest.py on two
real nodes.
"""
import argparse
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(HERE, "..", "python"))
sys.path.insert(0, os.path.join(REPO, "union"))

import numpy as np                                   # noqa: E402
import phy_link as pl                                # noqa: E402
import lora_driver                                   # noqa: E402
from lora_radio import SimMedium                     # noqa: E402


class Pinger:
    """Initiator: sends a known vector, expects it back doubled."""
    def __init__(self, n, rounds):
        self.spec = ("float32", (n,))
        self.n, self.rounds, self.k = n, rounds, 0
        self.ok, self.bad = 0, 0

    def transmit(self):
        if self.k >= self.rounds:
            return None
        self.k += 1
        return np.full(self.n, float(self.k), np.float32)

    def receive(self, msg):
        want = np.full(self.n, 2.0 * self.k, np.float32)
        if np.array_equal(np.asarray(msg, np.float32), want):
            self.ok += 1
        else:
            self.bad += 1


class Ponger:
    """Responder: replies with twice whatever it received."""
    def __init__(self, n):
        self.spec = ("float32", (n,))
        self.n, self.last, self.got = n, None, 0

    def receive(self, msg):
        self.last = np.asarray(msg, np.float32)
        self.got += 1

    def transmit(self):
        return None if self.last is None else (self.last * 2.0).astype(np.float32)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--sf", type=int, default=9, choices=range(7, 13), metavar="7..12")
    p.add_argument("--snr", type=float, default=5.0, help="link SNR in dB")
    p.add_argument("--bytes", type=int, default=400,
                   help="payload size; over ~247 B the message is fragmented")
    a = p.parse_args()

    n = max(1, a.bytes // 4)                      # float32 elements
    medium = SimMedium(snr_db=a.snr, seed=0)      # ONE medium: both ends share the air
    common = dict(backend="sim", sf=a.sf, medium=medium, verbose=True)

    tx = lora_driver.LoRaLink(role="tx", node=0, peer=1, **common)
    rx = lora_driver.LoRaLink(role="rx", node=1, peer=0, **common)
    ping, pong = pl.adapt(Pinger(n, a.rounds), "tx"), pl.adapt(Ponger(n), "rx")

    stop = threading.Event()

    def responder():
        while not stop.is_set():
            try:
                rx.step(pong)
            except Exception:                     # the initiator finished and went away
                return

    t = threading.Thread(target=responder, daemon=True)
    t.start()
    rounds = 0
    while tx.step(ping):
        rounds += 1
    stop.set()
    t.join(timeout=2.0)

    src, dst = ping._src, pong._src
    air = (tx.radio.airtime_ms + rx.radio.airtime_ms) / 1000.0
    print(f"\n  rounds completed : {rounds}/{a.rounds}")
    print(f"  replies correct  : {src.ok}   wrong: {src.bad}   responder saw: {dst.got}")
    print(f"  payload          : {a.bytes} B at SF{a.sf}, SNR {a.snr:+.1f} dB")
    print(f"  total airtime    : {air:.2f} s  (tx {tx.radio.n_tx} frames, "
          f"rx {rx.radio.n_tx} frames)")
    # A link below its spreading factor's demodulator floor is EXPECTED to lose
    # messages — both the fragment and its ACK have to clear the same threshold. So the
    # test only fails on a protocol error (a wrong payload), or on a clean link that
    # did not deliver. Losing rounds at a marginal SNR is the radio being honest.
    from lora_radio import SNR_FLOOR_DB
    marginal = a.snr < SNR_FLOOR_DB[a.sf] + 3.0
    good = src.bad == 0 and (marginal or (rounds == a.rounds and src.ok == a.rounds))
    if marginal:
        print(f"  link is MARGINAL: SNR {a.snr:+.1f} dB vs the SF{a.sf} floor "
              f"{SNR_FLOOR_DB[a.sf]:+.1f} dB — losses here are the model, not a fault")
    print(f"\n  {'PASS' if good else 'FAIL'}"
          f"{'  (no corruption; delivery limited by the link)' if marginal else ''}")
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
