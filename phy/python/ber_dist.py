#!/usr/bin/env python3
"""ber_dist.py — compare the BER *distribution* of two (or more) ber_monitor runs.

min/median/max hides that these links are bimodal (clean bursts vs carrier-lock
failures). This reads the per-burst CSVs written by ber_monitor.py and shows the
full shape: a pre-FEC and post-FEC BER histogram, a pre-FEC CDF, and a stats table
(detection rate, delivery rate, and the fraction of bursts that are clean <1% vs
garbage >10%).

    python3 ber_dist.py QPSK=../applications/MARL_RA_Union/results/ber_cmp_QPSK.csv \
                        DQPSK=../applications/MARL_RA_Union/results/ber_cmp_DQPSK.csv
    # or positional CSVs (labels taken from the filename stem):
    python3 ber_dist.py results/ber_cmp_QPSK.csv results/ber_cmp_DQPSK.csv
"""
import csv
import os
import sys

DEFAULT_OUT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "applications", "MARL_RA_Union", "results"))


def load(path):
    """Return (all_rows, detected_rows). Each row: dict with detected/pre/post/crc."""
    allr, det = [], []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            row = dict(detected=r["detected"] == "1",
                       pre=float(r["pre_fec_pct"]) if r["pre_fec_pct"] else None,
                       post=float(r["post_fec_pct"]) if r["post_fec_pct"] else None,
                       crc=r["crc"])
            allr.append(row)
            if row["detected"] and row["pre"] is not None:
                det.append(row)
    return allr, det


def stats(label, allr, det):
    n = len(allr)
    ndet = len(det)
    npass = sum(r["crc"] == "PASS" for r in allr)
    pre = sorted(r["pre"] for r in det)
    post = sorted(r["post"] for r in det)

    def pct(xs, p):
        if not xs:
            return float("nan")
        k = min(len(xs) - 1, int(round(p / 100.0 * (len(xs) - 1))))
        return xs[k]

    clean = sum(r["pre"] < 1.0 for r in det)          # essentially error-free channel
    garbage = sum(r["pre"] > 10.0 for r in det)        # past the FEC threshold
    return dict(
        label=label, n=n, ndet=ndet, npass=npass,
        detect=100.0 * ndet / n if n else 0,
        deliver=100.0 * npass / n if n else 0,
        pre_med=pct(pre, 50), pre_p90=pct(pre, 90),
        post_med=pct(post, 50),
        clean=100.0 * clean / ndet if ndet else 0,
        garbage=100.0 * garbage / ndet if ndet else 0)


def print_table(rows):
    print("\n%-8s %5s %8s %9s %9s %9s %9s %9s" % (
        "scheme", "n", "detect%", "deliver%", "preMED%", "preP90%", "clean%", "garbage%"))
    print("-" * 74)
    for s in rows:
        print("%-8s %5d %7.0f %8.0f %9.2f %9.2f %8.0f %9.0f" % (
            s["label"], s["n"], s["detect"], s["deliver"],
            s["pre_med"], s["pre_p90"], s["clean"], s["garbage"]))
    print("\nclean%   = detected bursts with pre-FEC BER < 1%  (carrier locked, ~error-free)")
    print("garbage% = detected bursts with pre-FEC BER > 10% (lock failed, past FEC limit)")
    print("deliver% = CRC pass over ALL fired bursts (the single-shot success rate)")


def plot(series, out_path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        print("[ber_dist] matplotlib unavailable (%s) — table only" % e)
        return
    colors = ["tab:blue", "tab:orange", "tab:green", "tab:red"]
    bins = list(range(0, 81, 5))                       # 0..80% in 5% bins

    fig, ax = plt.subplots(1, 3, figsize=(15, 4.6))
    for k, (label, allr, det) in enumerate(series):
        c = colors[k % len(colors)]
        pre = [r["pre"] for r in det]
        post = [r["post"] for r in det]
        ax[0].hist(pre, bins=bins, alpha=0.55, label=label, color=c)
        ax[1].hist(post, bins=bins, alpha=0.55, label=label, color=c)
        # CDF of pre-FEC BER
        xs = sorted(pre)
        ys = [100.0 * (i + 1) / len(xs) for i in range(len(xs))] if xs else []
        ax[2].plot(xs, ys, "-", color=c, lw=1.8, label=label)

    ax[0].set_title("pre-FEC (channel) BER distribution")
    ax[0].set_xlabel("BER (%)"); ax[0].set_ylabel("bursts")
    ax[1].set_title("post-FEC (payload) BER distribution")
    ax[1].set_xlabel("BER (%)")
    ax[2].set_title("pre-FEC BER — CDF")
    ax[2].set_xlabel("BER (%)"); ax[2].set_ylabel("% of detected bursts ≤ x")
    ax[2].axvline(11, color="0.5", ls="--", lw=1)      # rate-1/2 K=7 correction limit
    ax[2].text(11.5, 8, "FEC limit ~11%", color="0.4", fontsize=8)
    for a in ax:
        a.legend(fontsize=9); a.grid(alpha=0.3)
    fig.suptitle("BER distribution — bimodal: clean (~0%) vs carrier-lock-fail (~50%)",
                 fontsize=11)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    print("[ber_dist] wrote %s" % out_path)


def main(argv):
    if not argv:
        print(__doc__); return
    series = []
    for a in argv:
        if "=" in a and not a.startswith("/"):
            label, path = a.split("=", 1)
        else:
            path, label = a, os.path.splitext(os.path.basename(a))[0].replace("ber_cmp_", "")
        allr, det = load(path)
        series.append((label, allr, det))

    print_table([stats(lbl, allr, det) for (lbl, allr, det) in series])
    plot(series, os.path.join(DEFAULT_OUT, "ber_qpsk_vs_dqpsk_dist.png"))


if __name__ == "__main__":
    main(sys.argv[1:])
