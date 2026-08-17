#!/usr/bin/env python3
"""
plot_fec_compare.py — LDPC vs convolutional/Viterbi decoder comparison.

Reads the CSV from `fec_bench` and draws three panels vs Eb/N0:
  1. Coding gain   : CRC-OK % (the BER waterfall) — who corrects more.
  2. Decode cost   : us per 1000 info bits — LDPC is SNR-dependent (BP early-stop),
                     Viterbi is FLAT (fixed trellis).
  3. LDPC BP iters : average iterations to converge (why LDPC gets cheaper).
The RF chain (sync/CFO/phase/constellation) is identical for both codes — the
difference lives entirely in the decoder, which is what these panels isolate.

Usage:  python3 tools/plot_fec_compare.py [fec_compare.csv] [--save out.png]
"""
import sys, csv
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

path = "fec_compare.csv"
save = "fec_compare.png"
args = [a for a in sys.argv[1:] if not a.startswith("--")]
if args:
    path = args[0]
if "--save" in sys.argv:
    save = sys.argv[sys.argv.index("--save") + 1]

rows = list(csv.DictReader(open(path)))
x = np.array([float(r["ebn0_db"]) for r in rows])
def col(k): return np.array([float(r[k]) for r in rows])

series = {  # label: (crc_col, us_col, style)
    "turbo soft":      ("turbo_soft_crc", "turbo_soft_us", dict(color="#4575b4", marker="^", lw=2)),
    "turbo hard":      ("turbo_hard_crc", "turbo_hard_us", dict(color="#91bfdb", marker="^", ls="--")),
    "LDPC soft":       ("ldpc_soft_crc", "ldpc_soft_us", dict(color="#1a9850", marker="o", lw=2)),
    "LDPC hard":       ("ldpc_hard_crc", "ldpc_hard_us", dict(color="#66bd63", marker="o", ls="--")),
    "conv soft (Vit)": ("conv_soft_crc", "conv_soft_us", dict(color="#d73027", marker="s", lw=2)),
    "conv hard (Vit)": ("conv_hard_crc", "conv_hard_us", dict(color="#f46d43", marker="s", ls="--")),
}

fig, ax = plt.subplots(1, 3, figsize=(15, 4.6))

# 1. Coding gain (CRC-OK % vs Eb/N0)
for name, (c, u, st) in series.items():
    ax[0].plot(x, col(c), label=name, **st)
ax[0].set_title("Coding gain — decode success vs SNR")
ax[0].set_xlabel("Eb/N0 (dB)"); ax[0].set_ylabel("CRC-OK (%)")
ax[0].set_ylim(-3, 103); ax[0].grid(alpha=0.3); ax[0].legend(fontsize=8)

# 2. Decode cost (us per 1000 info bits)
for name, (c, u, st) in series.items():
    ax[1].plot(x, col(u), label=name, **st)
ax[1].set_title("Decode cost — time per 1000 info bits")
ax[1].set_xlabel("Eb/N0 (dB)"); ax[1].set_ylabel("µs / 1000 info bits")
ax[1].grid(alpha=0.3); ax[1].legend(fontsize=8)
ax[1].annotate("Viterbi = FLAT (fixed trellis)", xy=(x[len(x)//2], col("conv_soft_us")[len(x)//2]),
               xytext=(0.35, 0.85), textcoords="axes fraction", fontsize=8,
               arrowprops=dict(arrowstyle="->", color="#d73027"))
ax[1].annotate("LDPC falls as SNR rises\n(BP early-terminates)", xy=(x[-2], col("ldpc_soft_us")[-2]),
               xytext=(0.30, 0.35), textcoords="axes fraction", fontsize=8,
               arrowprops=dict(arrowstyle="->", color="#1a9850"))

# 3. Iterative-decoder iterations (LDPC BP + turbo BCJR)
ax[2].plot(x, col("turbo_soft_it"), color="#4575b4", marker="^", lw=2, label="turbo soft (BCJR)")
ax[2].plot(x, col("ldpc_soft_it"), color="#1a9850", marker="o", lw=2, label="LDPC soft (BP)")
ax[2].plot(x, col("ldpc_hard_it"), color="#66bd63", marker="o", ls="--", label="LDPC hard")
ax[2].axhline(50, color="gray", ls=":", lw=1); ax[2].text(x[0], 47, "LDPC cap 50", fontsize=8, color="gray")
ax[2].axhline(6, color="#4575b4", ls=":", lw=1); ax[2].text(x[0], 6.5, "turbo cap 6", fontsize=8, color="#4575b4")
ax[2].set_title("Iterative-decoder iterations to converge")
ax[2].set_xlabel("Eb/N0 (dB)"); ax[2].set_ylabel("avg iterations")
ax[2].grid(alpha=0.3); ax[2].legend(fontsize=8)
ax[2].text(0.5, -0.30, "Viterbi has no iterations (single trellis pass); LDPC & turbo early-terminate",
           transform=ax[2].transAxes, ha="center", fontsize=8, color="gray")

fig.suptitle("Turbo vs LDPC vs convolutional/Viterbi — the difference is in the DECODER "
             "(RF chain is identical)", fontsize=12, y=1.00)
fig.tight_layout(rect=[0, 0.03, 1, 0.97])
fig.savefig(save, dpi=130, bbox_inches="tight")
print("wrote", save)
