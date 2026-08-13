#!/usr/bin/env python3
"""
plot_evidence.py — render the stage-by-stage RX proof from chain_evidence.

For each run dir (<out>/SC_QPSK, <out>/OFDM_QPSK) it draws a row:
  [ RX constellation BEFORE correction | RX constellation AFTER correction |
    a text panel with the sync / CFO / phase / EVM / BER / CRC numbers ].
A tight AFTER cluster on the ideal points, matching CFO est≈true, and CRC=OK
together demonstrate sync + freq/phase offset + equalization + demod + LDPC decode
all succeeded.

Usage:  python3 tools/plot_evidence.py [out_dir] [--save out.png]
"""
import sys, os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def load(path):
    if not os.path.exists(path):
        return None
    d = np.loadtxt(path, comments="#")
    if d is None or d.size == 0:
        return None
    if d.ndim == 1:
        d = d.reshape(1, -1)
    return d[:, 0] + 1j * d[:, 1]


def read_stages(path):
    kv = {}
    if os.path.exists(path):
        for line in open(path):
            if "=" in line:
                k, v = line.strip().split("=", 1)
                kv[k] = v
    return kv


def scatter(ax, syms, ideal, title):
    if syms is not None and len(syms):
        s = syms / (np.sqrt(np.mean(np.abs(syms) ** 2)) + 1e-12)
        ax.scatter(s.real, s.imag, s=4, alpha=0.35, color="#2b7bba", linewidths=0)
    if ideal is not None:
        i = ideal / (np.sqrt(np.mean(np.abs(ideal) ** 2)) + 1e-12)
        ax.scatter(i.real, i.imag, s=140, marker="x", color="#d62728", linewidths=2, zorder=5)
    ax.set_title(title, fontsize=10)
    ax.set_xlim(-2, 2); ax.set_ylim(-2, 2)
    ax.set_aspect("equal"); ax.grid(alpha=0.25)
    ax.axhline(0, color="k", lw=0.5); ax.axvline(0, color="k", lw=0.5)


def report_text(kv):
    def g(k, d="—"): return kv.get(k, d)
    L = []
    wf = g("waveform"); L.append(f"{wf}  ·  {g('scheme')}  ·  FEC {g('fec')}")
    L.append("")
    L.append(f"SYNC        : {g('sync')}" +
             (f"   ACQ peak {g('acq_peak')}/{g('acq_pmax')}, tau={g('tau')}"
              if "acq_peak" in kv else f"   start={g('start')}"))
    if "cfo_true_hz" in kv:
        L.append(f"FREQ (CFO)  : true {g('cfo_true_hz')} Hz  ->  est {g('cfo_est_hz')} Hz")
        L.append(f"PHASE       : est {g('phase_est_deg')} deg")
    else:
        L.append(f"FREQ (CFO)  : true {g('cfo_true_sc')} sc  ->  est {g('cfo_est_sc')} sc")
    L.append(f"EVM (post)  : {g('evm_pct')} %")
    if "bits_pre" in kv:
        L.append(f"DEMOD bits  : pre-corr {g('bits_pre')}  ->  post-corr {g('bits_demod')}  / {g('coded_bits')}")
    else:
        L.append(f"DEMOD bits  : post-EQ {g('bits_demod')} / {g('coded_bits')}")
    L.append(f"FEC decode  : info err  hard {g('info_err_hard')}  soft {g('info_err_soft')}  / {g('info_bits')}")
    ch, cs = g("crc_hard"), g("crc_soft")
    L.append("")
    L.append(f"CRC         : hard {ch}     soft {cs}")
    ok = (ch == "OK" or cs == "OK")
    L.append("")
    L.append("RESULT: " + ("✓ CHAIN VERIFIED" if ok else "✗ decode failed"))
    return "\n".join(L), ok


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    base = args[0] if args else "evidence"
    save = "evidence/chain_evidence.png"
    if "--save" in sys.argv:
        save = sys.argv[sys.argv.index("--save") + 1]

    runs = [d for d in ("SC_QPSK", "OFDM_QPSK") if os.path.isdir(os.path.join(base, d))]
    if not runs:
        print(f"no run dirs under {base}/"); sys.exit(1)

    fig, axes = plt.subplots(len(runs), 3, figsize=(13, 4.2 * len(runs)))
    if len(runs) == 1:
        axes = axes.reshape(1, -1)

    for r, run in enumerate(runs):
        d = os.path.join(base, run)
        ideal = load(os.path.join(d, "ideal.txt"))
        pre = load(os.path.join(d, "rx_pre.txt"))
        post = load(os.path.join(d, "rx_post.txt"))
        kv = read_stages(os.path.join(d, "stages.txt"))
        scatter(axes[r, 0], pre, ideal, f"{run}: RX BEFORE correction")
        scatter(axes[r, 1], post, ideal, f"{run}: RX AFTER correction")
        txt, ok = report_text(kv)
        axes[r, 2].axis("off")
        axes[r, 2].text(0.0, 0.98, txt, va="top", ha="left", family="monospace",
                        fontsize=9.5,
                        bbox=dict(boxstyle="round", fc="#eaf6ea" if ok else "#fdeaea",
                                  ec="#4c9a4c" if ok else "#c0392b"))

    fec = ""
    for run in runs:
        kv = read_stages(os.path.join(base, run, "stages.txt"))
        if kv.get("fec"): fec = kv["fec"]; break
    fig.suptitle(f"RX chain evidence ({fec}) — sync · CFO · phase · demod · FEC decode",
                 fontsize=13, y=0.995)
    fig.tight_layout(rect=[0, 0, 1, 0.98])
    os.makedirs(os.path.dirname(save) or ".", exist_ok=True)
    fig.savefig(save, dpi=130)
    print(f"wrote {save}")


if __name__ == "__main__":
    main()
