#!/usr/bin/env python3
"""
plot_fec_latency.py — per-packet decode-latency distributions from fec_latency.

Overlaid histograms of decode-call time per packet. Viterbi is a narrow spike
(fixed trellis, constant work); LDPC and turbo are spread out because their
iteration count depends on the noise realization (early termination). The mean
line for each shows the typical cost.

Usage:  python3 tools/plot_fec_latency.py [fec_latency.csv] [--save out.png]
"""
import sys, csv
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

path = sys.argv[1] if (len(sys.argv) > 1 and not sys.argv[1].startswith("--")) else "fec_latency.csv"
save = sys.argv[sys.argv.index("--save") + 1] if "--save" in sys.argv else "fec_latency.png"

data = {}
for row in csv.DictReader(open(path)):
    data.setdefault(row["decoder"], []).append(float(row["us"]))

colors = {"conv (Viterbi)": "#d73027", "LDPC (min-sum)": "#1a9850", "turbo (BCJR)": "#4575b4"}
allv = np.concatenate([np.array(v) for v in data.values()])
lo, hi = np.percentile(allv, 0.5), np.percentile(allv, 99.5)
bins = np.linspace(lo, hi, 60)

fig, ax = plt.subplots(figsize=(10, 5))
for name, vals in data.items():
    v = np.array(vals)
    c = colors.get(name, "gray")
    ax.hist(v, bins=bins, alpha=0.55, color=c, label=f"{name}  (mean {v.mean():.0f} µs)")
    ax.axvline(v.mean(), color=c, ls="--", lw=1.5)

ax.set_xlabel("decode time per packet (µs)")
ax.set_ylabel("count")
ax.set_title("Per-packet decode latency — Viterbi is a fixed spike; "
             "LDPC & turbo spread with iteration count")
ax.legend()
ax.grid(alpha=0.25)
fig.tight_layout()
fig.savefig(save, dpi=130)
print("wrote", save)
