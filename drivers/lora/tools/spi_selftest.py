#!/usr/bin/env python3
"""
radio_selftest.py — standalone SX1276 link check (NO server, NO protocol).

Run this FIRST, before wiring the radios into the FORGE stack. It proves the
two Pis' SX1276 modules can actually reach each other at a given (SF, CR, BW,
power) and shows you the SNR/RSSI you get indoors — the numbers the optimizer
will later adapt to. If this doesn't work, nothing above it will.

    # on Pi B (receiver) — start this first
    python3 radio_selftest.py --role rx --freq 915.0 --sf 9

    # on Pi A (transmitter)
    python3 radio_selftest.py --role tx --freq 915.0 --sf 9 --power 14

The TX sends "PING <n>" once a second; the RX prints each decode with SNR/RSSI
and a running packet-loss count. Ctrl-C to stop. Only this one file is needed
on each Pi for the check.
"""
from __future__ import annotations
import argparse
import time


def make_radio(freq_mhz: float, sf: int, cr: int, bw: int, power: int,
               cs: str, reset: str):
    import board          # type: ignore
    import busio          # type: ignore
    import digitalio      # type: ignore
    import adafruit_rfm9x  # type: ignore
    spi = busio.SPI(board.SCK, MOSI=board.MOSI, MISO=board.MISO)
    cs_pin = digitalio.DigitalInOut(getattr(board, cs))
    rst_pin = digitalio.DigitalInOut(getattr(board, reset))
    rfm = adafruit_rfm9x.RFM9x(spi, cs_pin, rst_pin, freq_mhz)
    rfm.node = 0xFF
    rfm.destination = 0xFF
    rfm.enable_crc = True
    rfm.spreading_factor = sf
    rfm.coding_rate = cr
    rfm.signal_bandwidth = bw
    rfm.tx_power = max(5, min(23, power))
    print(f"radio: {freq_mhz:.3f} MHz  SF{sf}  CR4/{cr}  BW{bw}  P{rfm.tx_power}dBm")
    return rfm


def snr_of(rfm) -> float:
    snr = getattr(rfm, "last_snr", None)
    if snr is not None:
        return float(snr)
    raw = rfm._read_u8(0x19)  # RegPktSnrValue
    return (raw - 256 if raw > 127 else raw) / 4.0


def main() -> int:
    p = argparse.ArgumentParser(description="SX1276 point-to-point link test.")
    p.add_argument("--role", required=True, choices=("tx", "rx"))
    p.add_argument("--freq", type=float, default=915.0, help="MHz (US915: 915.0)")
    p.add_argument("--sf", type=int, default=9)
    p.add_argument("--cr", type=int, default=5, help="coding-rate denom 5..8")
    p.add_argument("--bw", type=int, default=125_000)
    p.add_argument("--power", type=int, default=14, help="TX power dBm 5..23")
    p.add_argument("--period", type=float, default=1.0, help="tx: seconds between pings")
    p.add_argument("--cs", default="CE0")
    p.add_argument("--reset", default="D25")
    args = p.parse_args()

    rfm = make_radio(args.freq, args.sf, args.cr, args.bw, args.power,
                     args.cs, args.reset)

    if args.role == "tx":
        n = 0
        print("transmitting PINGs — Ctrl-C to stop")
        while True:
            n += 1
            payload = f"PING {n}".encode("ascii")
            t0 = time.monotonic()
            rfm.send(payload)
            dt = (time.monotonic() - t0) * 1000
            print(f"  sent #{n}  ({len(payload)}B, toa~{dt:.0f} ms)")
            time.sleep(max(0.0, args.period))
    else:
        print("listening — Ctrl-C to stop")
        got, last_seen = 0, None
        while True:
            pkt = rfm.receive(timeout=1.0, keep_listening=True, with_header=False)
            if pkt is None:
                continue
            got += 1
            try:
                text = pkt.decode("ascii", "replace")
            except Exception:
                text = repr(pkt)
            seen = None
            if text.startswith("PING "):
                try:
                    seen = int(text.split()[1])
                except (ValueError, IndexError):
                    pass
            gap = ""
            if seen is not None and last_seen is not None and seen > last_seen + 1:
                gap = f"   [MISSED {seen - last_seen - 1}]"
            if seen is not None:
                last_seen = seen
            print(f"  rx '{text}'  SNR={snr_of(rfm):+.1f} dB  RSSI={rfm.last_rssi:.0f} dBm"
                  f"  (decoded {got}){gap}")


if __name__ == "__main__":
    raise SystemExit(main())
