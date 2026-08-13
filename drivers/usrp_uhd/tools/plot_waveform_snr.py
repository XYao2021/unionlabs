#!/usr/bin/env python3
"""
plot_waveform_snr.py — FAIR SC-vs-OFDM comparison at matched Eb/N0.

Reads fec_waveform_snr.csv (waveform, ebn0_db, crc_pct, uncoded_ber) and plots
both quantities vs the MEASURED Eb/N0. When the SNR is measured (not an injected
noise fraction), SC and OFDM nearly coincide — the large gap seen with the raw
"noise fraction" knob was a calibration artifact, not real physics. The uncoded
panel overlays ideal QPSK BER = Q(sqrt(2·Eb/N0)) for reference.

Usage:  python3 tools/plot_waveform_snr.py [csv] [--save out.png]
"""
import sys, csv, math
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

path = sys.argv[1] if (len(sys.argv) > 1 and not sys.argv[1].startswith("--")) else "fec_waveform_snr.csv"
save = sys.argv[sys.argv.index("--save") + 1] if "--save" in sys.argv else "fec_waveform_snr.png"

rows = list(csv.DictReader(open(path)))
data = {}
for r in rows:
    # keep only the valid region: a dead link gives garbage (negative) Eb/N0
    # and BER ~0.5; drop those.
    ber = float(r["uncoded_ber"]); ebn0 = float(r["ebn0_db"])
    if ber > 0.25 or ebn0 < 0:
        continue
    data.setdefault(r["waveform"], []).append((ebn0, float(r["crc_pct"]), ber))

styles = {"SC": dict(color="#d73027", marker="s"), "OFDM": dict(color="#4575b4", marker="^")}

fig, ax = plt.subplots(1, 2, figsize=(13, 5))

for wf, pts in data.items():
    pts.sort()
    x = np.array([p[0] for p in pts])
    crc = np.array([p[1] for p in pts])
    ber = np.array([p[2] for p in pts])
    st = styles.get(wf, dict(color="gray", marker="o"))
    ax[0].plot(x, crc, lw=2, label=f"{wf} · turbo", **st)
    ax[1].semilogy(x, np.maximum(ber, 1e-5), lw=2, label=f"{wf} uncoded", **st)

# ideal uncoded QPSK reference
xt = np.linspace(0, 12, 100)
q = 0.5 * np.array([math.erfc(math.sqrt(10 ** (e / 10))) for e in xt])
ax[1].semilogy(xt, np.maximum(q, 1e-5), "k--", lw=1.3, label="ideal QPSK theory")

ax[0].set_title("Coded (turbo) — decode success vs MEASURED Eb/N0")
ax[0].set_xlabel("measured Eb/N0 (dB)"); ax[0].set_ylabel("CRC-OK (%)")
ax[0].set_ylim(-3, 103); ax[0].grid(alpha=0.3); ax[0].legend()

ax[1].set_title("Uncoded QPSK BER vs MEASURED Eb/N0")
ax[1].set_xlabel("measured Eb/N0 (dB)"); ax[1].set_ylabel("uncoded BER")
ax[1].set_ylim(1e-4, 1); ax[1].grid(alpha=0.3, which="both"); ax[1].legend()

fig.suptitle("Fair SC vs OFDM at MATCHED Eb/N0 — the curves nearly coincide "
             "(the earlier gap was an unequal-noise artifact)", fontsize=12, y=1.00)
fig.tight_layout(rect=[0, 0, 1, 0.96])
fig.savefig(save, dpi=130, bbox_inches="tight")
print("wrote", save)
