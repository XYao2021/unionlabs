#!/usr/bin/env python3
"""
freq_scan.py — sweep a frequency RANGE and report received power at each step,
so you can pick a QUIET carrier for your link before running a real transfer.

It retunes the USRP RX across the band and, at each frequency, integrates the
received power over a short window (drives `sdr_system --role sense`). It then
prints a table + an ASCII spectrum and highlights the quietest candidates.

Works on any device — pass its address:  N210/X310 -> addr=..., B210 -> serial=...

Examples:
  # 900 MHz ISM band, 1 MHz steps, on the N210:
  python3 freq_scan.py --start 902 --stop 928 --step 1 --rx-args addr=192.168.20.2
  # 2.4 GHz band on a B210, finer steps, save a plot:
  python3 freq_scan.py --start 2400 --stop 2483 --step 2 \
      --rx-args serial=30CD3F7 --rx-gain 40 --plot
Frequencies are in MHz. Each step launches the sensing binary once (~1-2 s),
so a wide fine scan takes a while — start coarse, then zoom into the quiet part.
"""
import sys, os, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from channel_sense import sense_channel


def main():
    ap = argparse.ArgumentParser(description="USRP frequency-range signal scan")
    ap.add_argument("--start", type=float, required=True, help="start frequency (MHz)")
    ap.add_argument("--stop",  type=float, required=True, help="stop frequency (MHz)")
    ap.add_argument("--step",  type=float, default=1.0,   help="step size (MHz), default 1")
    ap.add_argument("--rx-args", default="addr=192.168.20.2",
                    help="device: addr=... (N210/X310) or serial=... (B210)")
    ap.add_argument("--rx-gain", type=float, default=30, help="RX gain dB (N210 max 31.5)")
    ap.add_argument("--rx-rate", type=float, default=2e6,
                    help="RX sample rate Hz = analysis bandwidth per step (N210: 2e6=100/50)")
    ap.add_argument("--window", type=float, default=20.0, help="integration window ms per step")
    ap.add_argument("--top", type=int, default=5, help="how many quietest freqs to highlight")
    ap.add_argument("--plot", action="store_true", help="also save a power-vs-freq PNG")
    ap.add_argument("--save", default="freq_scan.png")
    a = ap.parse_args()

    # build the frequency grid
    freqs, f = [], a.start
    while f <= a.stop + 1e-9:
        freqs.append(round(f, 6)); f += a.step

    print(f"# scan {a.start}-{a.stop} MHz  |  {len(freqs)} steps of {a.step} MHz  |  "
          f"rx-args={a.rx_args} gain={a.rx_gain}dB bw={a.rx_rate/1e6}MHz window={a.window}ms")
    print(f"# note: each step retunes + senses (one binary launch); wide fine scans are slow.\n")
    print(f"{'MHz':>10} | {'power_dB':>8} | {'peak_dB':>7} | occupancy (dB above floor)")

    rows = []
    for fm in freqs:
        try:
            r = sense_channel(window_ms=a.window, rx_args=a.rx_args, rx_freq=fm * 1e6,
                              rx_gain=a.rx_gain, rx_rate=a.rx_rate)
            rows.append((fm, r["power_db"], r["peak_db"]))
            print(f"{fm:10.3f} | {r['power_db']:8.2f} | {r['peak_db']:7.2f} | ...", flush=True)
        except Exception as e:
            print(f"{fm:10.3f} |   (sense failed: {e})", flush=True)

    if not rows:
        print("\nno measurements — check the device args / that the radio is reachable.")
        return

    # reference floor = mean of the quietest quartile; occupancy is dB above it
    pw = sorted(x[1] for x in rows)
    q = max(1, len(pw) // 4)
    floor = sum(pw[:q]) / q

    print(f"\n{'MHz':>10} | {'power_dB':>8} | occupancy")
    for fm, p, pk in rows:
        rel = p - floor
        bar = "#" * min(50, max(0, int(round(rel))))
        flag = "BUSY " if rel > 6 else ("~busy" if rel > 3 else "quiet")
        print(f"{fm:10.3f} | {p:8.2f} | {flag} {bar}")

    quiet = sorted(rows, key=lambda x: x[1])[:a.top]
    print(f"\nnoise floor ~= {floor:.1f} dB.  Quietest {a.top} carriers (best for your link):")
    for fm, p, pk in quiet:
        print(f"    {fm:10.3f} MHz    power {p:.2f} dB   ({p-floor:+.1f} dB vs floor)")

    if a.plot:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            xs = [r[0] for r in rows]; ys = [r[1] for r in rows]
            plt.figure(figsize=(11, 4.2))
            plt.plot(xs, ys, "-o", ms=3, color="#2b7bba", label="received power")
            plt.axhline(floor, ls="--", color="gray", label=f"floor {floor:.1f} dB")
            plt.axhline(floor + 6, ls=":", color="#d62728", label="busy (+6 dB)")
            for fm, p, pk in quiet:
                plt.plot(fm, p, "*", color="#1a9850", ms=13)
            plt.xlabel("frequency (MHz)"); plt.ylabel("received power (dB)")
            plt.title("USRP frequency scan — green ★ = quietest candidates")
            plt.legend(fontsize=8); plt.grid(alpha=0.3); plt.tight_layout()
            plt.savefig(a.save, dpi=130)
            print(f"\nwrote {a.save}")
        except Exception as e:
            print(f"(plot skipped: {e})")


if __name__ == "__main__":
    main()
