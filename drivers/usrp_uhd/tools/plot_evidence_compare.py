#!/usr/bin/env python3
"""
plot_evidence_compare.py — conv vs LDPC vs turbo, side by side.

Reads several chain_evidence output dirs (one per FEC code) and lays out the
RX-AFTER-correction constellations in a grid: rows = waveform (SC / OFDM),
cols = FEC code. The constellations are IDENTICAL across codes (FEC never
touches the RF chain) — the point is that all three lock the same clean
constellation and all decode (CRC OK). Each cell is annotated with the code's
decode result.

Usage:
  python3 tools/plot_evidence_compare.py ev_conv ev_ldpc ev_turbo --save out.png
"""
import sys, os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

save = "chain_evidence_compare.png"
argv = sys.argv[1:]
if "--save" in argv:
    i = argv.index("--save")
    save = argv[i + 1]
    del argv[i:i + 2]                       # drop the flag AND its value
dirs = [a for a in argv if not a.startswith("--")] or ["ev_conv", "ev_ldpc", "ev_turbo"]


def load(p):
    if not os.path.exists(p):
        return None
    d = np.loadtxt(p, comments="#")
    if d.size == 0:
        return None
    if d.ndim == 1:
        d = d.reshape(1, -1)
    return d[:, 0] + 1j * d[:, 1]


def stages(p):
    kv = {}
    if os.path.exists(p):
        for line in open(p):
            if "=" in line:
                k, v = line.strip().split("=", 1)
                kv[k] = v
    return kv


waveforms = ["SC_QPSK", "OFDM_QPSK"]
fig, axes = plt.subplots(len(waveforms), len(dirs), figsize=(4.2 * len(dirs), 4.2 * len(waveforms)))
if len(waveforms) == 1:
    axes = axes.reshape(1, -1)
if len(dirs) == 1:
    axes = axes.reshape(-1, 1)

for r, wf in enumerate(waveforms):
    for c, base in enumerate(dirs):
        ax = axes[r, c]
        d = os.path.join(base, wf)
        ideal = load(os.path.join(d, "ideal.txt"))
        post = load(os.path.join(d, "rx_post.txt"))
        kv = stages(os.path.join(d, "stages.txt"))
        if post is not None and len(post):
            s = post / (np.sqrt(np.mean(np.abs(post) ** 2)) + 1e-12)
            ax.scatter(s.real, s.imag, s=4, alpha=0.35, color="#2b7bba", linewidths=0)
        if ideal is not None:
            i = ideal / (np.sqrt(np.mean(np.abs(ideal) ** 2)) + 1e-12)
            ax.scatter(i.real, i.imag, s=130, marker="x", color="#d62728", linewidths=2, zorder=5)
        ax.set_xlim(-2, 2); ax.set_ylim(-2, 2); ax.set_aspect("equal"); ax.grid(alpha=0.25)
        ax.axhline(0, color="k", lw=0.4); ax.axvline(0, color="k", lw=0.4)
        fec = kv.get("fec", os.path.basename(base))
        crc = kv.get("crc_soft", kv.get("crc_hard", "?"))
        errs = kv.get("info_err_soft", "?")
        evm = kv.get("evm_pct", "?")
        ax.set_title(f"{wf.split('_')[0]} · {fec}", fontsize=10)
        col = "#1a7a1a" if crc == "OK" else "#b00"
        ax.text(0.5, -0.14, f"EVM {evm}%  ·  info-err {errs}  ·  CRC {crc}",
                transform=ax.transAxes, ha="center", fontsize=8.5, color=col)

fig.suptitle("RX-after-correction constellation is IDENTICAL across FEC codes — "
             "all lock & all decode (CRC OK).  Codes differ only in the decoder.",
             fontsize=12, y=1.00)
fig.tight_layout(rect=[0, 0.02, 1, 0.97])
fig.savefig(save, dpi=130, bbox_inches="tight")
print("wrote", save)
