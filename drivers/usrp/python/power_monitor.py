#!/usr/bin/env python3
"""
power_monitor.py — quantify the influence of the B210 power source (USB-only vs
USB + external 6 V DC) with a fixed TX tone + the RX tone monitor.

A weak supply (USB2 / a hub) starves the PA at high --tx-gain and adds phase noise;
external 6 V DC gives full power and a cleaner LO. This runs a constant TX cosine on
one radio and the RX tone monitor on another, parses the per-second
`[MONITOR] tone f = .. kHz  avg power = ..  peak = .. dB` lines, and reports:

    TX power delivered : mean peak dB   (higher = more PA headroom; USB may cap it)
    LO / CFO stability : tone-f mean / std / range (kHz)  (lower spread = cleaner supply)
    amplitude stability: peak-dB std, avg-power std

Run ONCE per power configuration (switch the DC jack between runs), then compare:

    python3 power_monitor.py --tag usb     --seconds 60 --tx-gain 85
    #  ... plug in the 6 V DC ...
    python3 power_monitor.py --tag usb_dc  --seconds 60 --tx-gain 85
    python3 power_monitor.py --compare <usb.csv> <usb_dc.csv>

Both CSVs land in ../experiments/marl_ra/results/ (or --out).
"""
import argparse
import os
import re
import subprocess
import sys
import time

_PHY = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _PHY)
import sdr  # noqa: E402

DEFAULT_OUT = os.path.abspath(os.path.join(_PHY, "..", "experiments", "marl_multi", "results"))
_MON = re.compile(r"tone f =\s*([+-][\d.]+)\s*kHz\s+avg power =\s*([\d.]+)\s+peak =\s*([+-]?[\d.]+)\s*dB")


def _stats(xs):
    import statistics as st
    if not xs:
        return dict(n=0, mean=float("nan"), std=float("nan"), lo=float("nan"), hi=float("nan"))
    return dict(n=len(xs), mean=st.mean(xs), std=(st.pstdev(xs) if len(xs) > 1 else 0.0),
                lo=min(xs), hi=max(xs))


def run(tag, seconds=60, tone_freq=200e3, tx_gain=85, rx_gain=20, freq=915e6, rate=1.6e6,
        tx_args="serial=30CD424", rx_args="serial=30CD3F7", out_dir=DEFAULT_OUT,
        tx=True, binary=None):
    os.makedirs(out_dir, exist_ok=True)
    procs = []
    txp = None
    if tx:
        txcmd = sdr.tx(message_type="cosine", tone_freq=tone_freq, tx_mode="continuous",
                       tx_args=tx_args, tx_freq=freq, tx_rate=rate, tx_gain=tx_gain,
                       binary=binary).command()
        import shlex
        txp = subprocess.Popen(shlex.split(txcmd), stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        procs.append(txp)
        time.sleep(4)                                    # let the tone come up
    rxcmd = sdr.rx(message_type="cosine", rx_args=rx_args, rx_freq=freq, rx_rate=rate,
                   rx_gain=rx_gain, binary=binary).command()
    import shlex
    print("[power] tag=%s  tx-gain=%s rx-gain=%s tone=%.0f kHz  %ds  (TX %s)"
          % (tag, tx_gain, rx_gain, tone_freq / 1e3, seconds, "on" if tx else "external"))
    rxp = subprocess.Popen(shlex.split(rxcmd), stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True, bufsize=1)
    procs.append(rxp)

    rows = []
    t0 = time.time()
    try:
        while time.time() - t0 < seconds:
            line = rxp.stdout.readline()
            if not line:
                if rxp.poll() is not None:
                    break
                continue
            m = _MON.search(line)
            if m:
                t = time.time() - t0
                rows.append((t, float(m.group(1)), float(m.group(2)), float(m.group(3))))
                print("[%5.1fs] tone_f=%+7.1f kHz  avg_pow=%.4f  peak=%6.1f dB"
                      % (t, rows[-1][1], rows[-1][2], rows[-1][3]))
    except KeyboardInterrupt:
        print("\n[power] interrupted")
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()

    path = os.path.join(out_dir, "power_%s.csv" % tag)
    with open(path, "w") as f:
        f.write("t_s,tone_f_khz,avg_power,peak_db\n")
        for r in rows:
            f.write("%.2f,%.1f,%.6f,%.2f\n" % r)
    _summary(tag, rows)
    print("[power] wrote %s" % path)
    return rows


def _summary(tag, rows):
    f = _stats([r[1] for r in rows])
    p = _stats([r[3] for r in rows])
    a = _stats([r[2] for r in rows])
    print("\n===========  POWER-SOURCE SUMMARY (%s)  ===========" % tag)
    if not rows:
        print("no monitor samples — is the TX tone on and the RX locked?"); return
    print("samples            : %d" % f["n"])
    print("TX power (peak dB) : mean %+.1f  std %.2f   [higher=more PA power]"
          % (p["mean"], p["std"]))
    print("LO drift (tone kHz): mean %+.1f  std %.2f  range %.1f  [lower spread=cleaner LO]"
          % (f["mean"], f["std"], f["hi"] - f["lo"]))
    print("avg power          : mean %.4f  std %.4f" % (a["mean"], a["std"]))
    print("===================================================")


def _load(path):
    rows = []
    with open(path) as f:
        next(f)
        for ln in f:
            c = ln.strip().split(",")
            if len(c) == 4:
                rows.append(tuple(float(x) for x in c))
    return rows


def compare(a_csv, b_csv):
    a, b = _load(a_csv), _load(b_csv)
    la = os.path.splitext(os.path.basename(a_csv))[0].replace("power_", "")
    lb = os.path.splitext(os.path.basename(b_csv))[0].replace("power_", "")

    def col(rows, i):
        return _stats([r[i] for r in rows])
    print("\n metric                 | %-14s | %-14s | delta" % (la, lb))
    print(" " + "-" * 60)
    pa, pb = col(a, 3), col(b, 3)
    print(" TX power peak dB (mean) | %+13.1f | %+13.1f | %+.1f dB" % (pa["mean"], pb["mean"], pb["mean"] - pa["mean"]))
    print(" TX power std            | %13.2f | %13.2f | %+.2f" % (pa["std"], pb["std"], pb["std"] - pa["std"]))
    fa, fb = col(a, 1), col(b, 1)
    print(" LO drift std (kHz)      | %13.2f | %13.2f | %+.2f" % (fa["std"], fb["std"], fb["std"] - fa["std"]))
    print(" LO drift range (kHz)    | %13.1f | %13.1f | %+.1f" % (fa["hi"] - fa["lo"], fb["hi"] - fb["lo"], (fb["hi"] - fb["lo"]) - (fa["hi"] - fa["lo"])))
    print("\nreading: DC power should raise TX peak dB (more PA headroom) and LOWER the")
    print("LO-drift std/range (cleaner supply → less phase noise / frequency wander).")


def main(argv):
    a = argparse.ArgumentParser(description="B210 power-source A/B (TX tone + RX monitor)")
    a.add_argument("--compare", nargs=2, metavar=("A.csv", "B.csv"),
                   help="compare two prior runs and exit")
    a.add_argument("--tag", default="usb", help="label for this run (e.g. usb, usb_dc)")
    a.add_argument("--seconds", type=float, default=60)
    a.add_argument("--tone-freq", type=float, default=200e3)
    a.add_argument("--tx-gain", type=float, default=85)
    a.add_argument("--rx-gain", type=float, default=20)
    a.add_argument("--freq", type=float, default=915e6)
    a.add_argument("--rate", type=float, default=1.6e6)
    a.add_argument("--tx-args", default="serial=30CD424")
    a.add_argument("--rx-args", default="serial=30CD3F7")
    a.add_argument("--no-tx", action="store_true", help="RX monitor only (drive TX elsewhere)")
    a.add_argument("--out", default=DEFAULT_OUT)
    args = a.parse_args(argv)
    if args.compare:
        compare(args.compare[0], args.compare[1]); return
    run(args.tag, seconds=args.seconds, tone_freq=args.tone_freq, tx_gain=args.tx_gain,
        rx_gain=args.rx_gain, freq=args.freq, rate=args.rate, tx_args=args.tx_args,
        rx_args=args.rx_args, out_dir=args.out, tx=not args.no_tx)


if __name__ == "__main__":
    main(sys.argv[1:])
