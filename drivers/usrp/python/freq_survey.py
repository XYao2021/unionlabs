#!/usr/bin/env python3
"""
freq_survey.py — sweep a frequency band and report where it is quiet.

The pre-flight for an over-the-air experiment: before two testbeds agree on a
carrier, LISTEN across the band each antenna can serve and pick a channel that
is actually clean at BOTH sites. Runs on the existing sensing path
(channel_sense.py -> sdr_system --role sense); nothing transmits.

    python3 freq_survey.py --args addr=192.168.10.2               # ISM sweep
    python3 freq_survey.py --band ism915 --step-mhz 1 --gain 25
    python3 freq_survey.py --start-mhz 905 --stop-mhz 920 --step-mhz 0.5
    python3 freq_survey.py --args serial=30CD3F7 --csv survey.csv
    python3 freq_survey.py --dry-run                              # plan only

Each frequency point is one retune + one energy window, so a default ISM sweep
(902-928 MHz @ 1 MHz) takes a couple of minutes — the radio re-inits per
point. The busy threshold is calibrated once from the quietest measurements of
the sweep itself (median + margin), so an idle band needs no prior floor.

For the CROSS-TESTBED case: run this at each site, keep the CSVs, and pick a
carrier whose power is at the floor in BOTH — the survey prints its per-site
suggestion, the intersection is yours to choose.
"""
import argparse
import csv
import sys
import time

from channel_sense import _run_sense                      # the one sensing path

BANDS = {
    # name: (start_mhz, stop_mhz, why) — the lab's two antenna types get their
    # rated ranges; the VERT2450 is DUAL-band, so it has one entry per band.
    "ism915": (902.0, 928.0, "US ISM — what the platform's radios use by default"),
    "vert900": (824.0, 960.0, "VERT900 antenna's rated range (includes ISM 915)"),
    "vert2450": (2400.0, 2480.0, "VERT2450 antenna, its 2.4 GHz band"),
    "vert2450-5g": (4900.0, 5900.0, "VERT2450 antenna, its 4.9-5.9 GHz band"),
}


def survey(freqs_mhz, window_ms, gain, args, ant, subdev, binary=None):
    rows = []
    t0 = time.time()
    consecutive_fail = 0
    for i, f in enumerate(freqs_mhz):
        radio = dict(rx_args=args, rx_freq=f * 1e6, rx_gain=gain,
                     rx_ant=ant, rx_subdev=subdev)
        if binary:
            radio["binary"] = binary
        try:
            r = _run_sense(window_ms, 1, -999.0, **radio)[0]   # nothing flagged busy yet
            consecutive_fail = 0
        except Exception as e:
            # A tune that fails at a band edge is data; a radio that fails at
            # EVERY point (wrong address, FPGA compat mismatch, no route) is a
            # broken sweep — abort with the error instead of printing it 81x.
            consecutive_fail += 1
            print(f"  {f:9.3f} MHz  FAILED: {e}", file=sys.stderr)
            if i == 0 or consecutive_fail >= 3:
                sys.exit("[survey] aborting: the radio is failing on every "
                         "point — fix the error above, then rerun. (A partial "
                         "band is swept with --start-mhz/--stop-mhz.)")
            continue
        rows.append(dict(freq_mhz=f, power_db=r["power_db"], peak_db=r["peak_db"]))
        done, total = i + 1, len(freqs_mhz)
        eta = (time.time() - t0) / done * (total - done)
        print(f"  {f:9.3f} MHz   avg {r['power_db']:7.2f} dB   peak {r['peak_db']:7.2f} dB"
              f"   [{done}/{total}, ~{eta:.0f}s left]", flush=True)
    return rows


def report(rows, margin_db):
    """Floor from the sweep's own quietest half; busy = floor + margin."""
    if not rows:
        sys.exit("no successful measurements — is the radio reachable?")
    p = sorted(r["power_db"] for r in rows)
    floor = p[len(p) // 4]                      # lower quartile: robust to a busy band
    thr = floor + margin_db
    print(f"\n  floor ~{floor:.1f} dB (lower quartile) -> busy above {thr:.1f} dB "
          f"(+{margin_db:.0f} dB margin)\n")
    width = 40
    lo = min(r["power_db"] for r in rows)
    hi = max(max(r["power_db"] for r in rows), thr + 1)
    for r in rows:
        n = int((r["power_db"] - lo) / (hi - lo + 1e-9) * width)
        mark = "BUSY" if r["power_db"] > thr else "    "
        print(f"  {r['freq_mhz']:9.3f} MHz  {r['power_db']:7.2f} dB  "
              f"|{'#' * n:<{width}}| {mark}")
    quiet = sorted((r for r in rows if r["power_db"] <= thr),
                   key=lambda r: r["power_db"])
    print()
    if quiet:
        best = quiet[0]
        print(f"  quietest: " + "  ".join(f"{r['freq_mhz']:.3f}MHz({r['power_db']:.1f}dB)"
                                          for r in quiet[:5]))
        print(f"  suggestion:  --freq {best['freq_mhz']:g}")
    else:
        print("  every point is above the busy threshold — this band is loud here;"
              " sweep another band or expect interference.")
    return thr


def main():
    ap = argparse.ArgumentParser(description="sweep a band, report where it is quiet")
    ap.add_argument("--band", choices=sorted(BANDS), default="ism915",
                    help="preset range (default ism915). "
                         + " · ".join(f"{k}: {v[2]}" for k, v in sorted(BANDS.items())))
    ap.add_argument("--start-mhz", type=float, default=None, help="override the preset")
    ap.add_argument("--stop-mhz", type=float, default=None)
    ap.add_argument("--step-mhz", type=float, default=1.0)
    ap.add_argument("--window-ms", type=float, default=10.0,
                    help="energy-integration window per point (default 10)")
    ap.add_argument("--gain", type=float, default=25.0, help="RX gain, dB")
    ap.add_argument("--args", default="", help="UHD device args (serial=… / addr=…)")
    ap.add_argument("--device", choices=["b210", "n210", "x310"], default="b210",
                    help="sets the RF-channel default: b210 -> subdev A:A, "
                         "n210/x310 -> A:0 (same defaults as radio.sh)")
    ap.add_argument("--rx-ant", default="RX2", metavar="PORT",
                    help="the CONNECTOR the antenna is on (default RX2; use "
                         "TX/RX when that is where it is plugged)")
    ap.add_argument("--subdev", default=None, metavar="SPEC",
                    help="RF channel, overriding the --device default "
                         "(B210: A:A = RF A, A:B = RF B)")
    ap.add_argument("--margin-db", type=float, default=6.0,
                    help="busy threshold = sweep floor + this (default 6)")
    ap.add_argument("--csv", default=None, help="also write freq,power_db,peak_db rows")
    ap.add_argument("--binary", default=None, help="path to sdr_system if not the default")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, touch nothing")
    a = ap.parse_args()

    start, stop, _ = BANDS[a.band]
    if a.start_mhz is not None:
        start = a.start_mhz
    if a.stop_mhz is not None:
        stop = a.stop_mhz
    if not (0 < start < stop):
        sys.exit(f"bad range {start}..{stop} MHz")
    n = int(round((stop - start) / a.step_mhz)) + 1
    freqs = [round(start + i * a.step_mhz, 6) for i in range(n)]
    subdev = a.subdev or {"b210": "A:A", "n210": "A:0", "x310": "A:0"}[a.device]

    print(f"[survey] {start:g}-{stop:g} MHz, {len(freqs)} points @ {a.step_mhz:g} MHz, "
          f"window {a.window_ms:g} ms, gain {a.gain:g} dB, "
          f"ant {a.rx_ant}, subdev {subdev}"
          + (f", radio {a.args}" if a.args else "")
          + f"  (~{len(freqs) * 2:.0f}s: each point re-inits the radio)")
    if a.dry_run:
        return
    rows = survey(freqs, a.window_ms, a.gain, a.args, a.rx_ant, subdev, a.binary)
    report(rows, a.margin_db)
    if a.csv and rows:
        with open(a.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["freq_mhz", "power_db", "peak_db"])
            w.writeheader(); w.writerows(rows)
        print(f"  wrote {a.csv} ({len(rows)} rows) — keep one per site, compare")


if __name__ == "__main__":
    main()
