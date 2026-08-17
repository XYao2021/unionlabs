#!/usr/bin/env python3
"""
plot_viz.py — visualize the signals dumped by `sdr_system --viz`.

Reads results/phy_outputs/{tx_symbols,tx_wave,rx_symbols,rx_wave}.txt (each "real imag" per line)
and draws a 2x3 grid:  rows = TX / RX,  cols = time domain / spectrum / constellation.
The spectrum is the FFT of the waveform (shows the RRC pulse shape or the OFDM
subcarrier band). The constellation shows the modulation points — clean clusters
on the RX side demonstrate correct sync / CFO / equalization / demod.

Usage:
    python3 tools/plot_viz.py [viz_dir] [--fs 1.6e6] [--save out.png]
Requires: numpy, matplotlib.
"""
import sys, os
import numpy as np
import matplotlib.pyplot as plt


def load(path):
    if not os.path.exists(path):
        return None
    d = np.loadtxt(path, comments="#")
    if d.ndim == 1:
        d = d.reshape(1, -1)
    return d[:, 0] + 1j * d[:, 1]


def read_meta(viz_dir):
    meta = {}
    p = os.path.join(viz_dir, "meta.txt")
    if os.path.exists(p):
        for line in open(p):
            parts = line.split()
            if len(parts) >= 2:
                meta[parts[0]] = parts[1]
    return meta


def evm_pct(syms, ideal):
    """RMS EVM (%) of syms vs the nearest ideal constellation point.
    syms is power-normalized to match the (unit-power) ideal constellation."""
    if syms is None or ideal is None or len(syms) == 0:
        return None
    s = syms / (np.sqrt(np.mean(np.abs(syms) ** 2)) + 1e-12)
    i = ideal / (np.sqrt(np.mean(np.abs(ideal) ** 2)) + 1e-12)
    # nearest ideal point per symbol
    err = np.array([np.min(np.abs(r - i)) for r in s])
    ref = np.sqrt(np.mean(np.abs(i) ** 2))
    return 100.0 * np.sqrt(np.mean(err ** 2)) / ref


def spectrum_db(x, fs):
    n = len(x)
    if n < 8:
        return np.array([0]), np.array([0])
    w = np.hanning(n)
    X = np.fft.fftshift(np.fft.fft(x * w))
    mag = 20 * np.log10(np.abs(X) + 1e-9)
    mag -= mag.max()
    f = np.fft.fftshift(np.fft.fftfreq(n, d=1.0 / fs))
    return f, mag


def main():
    args = [a for a in sys.argv[1:]]
    viz_dir = "results/phy_outputs"      # must match sdr_system's --viz-dir default
    fs = 1.6e6
    save = None
    i = 0
    while i < len(args):
        if args[i] == "--fs":
            fs = float(args[i + 1]); i += 2
        elif args[i] == "--save":
            save = args[i + 1]; i += 2
        else:
            viz_dir = args[i]; i += 1

    sig = {
        "tx_wave":    load(os.path.join(viz_dir, "tx_wave.txt")),
        "tx_symbols": load(os.path.join(viz_dir, "tx_symbols.txt")),
        "rx_wave":    load(os.path.join(viz_dir, "rx_wave.txt")),
        "rx_symbols": load(os.path.join(viz_dir, "rx_symbols.txt")),
    }
    if all(v is None for v in sig.values()):
        print(f"No viz files found in '{viz_dir}/'. Run sdr_system with --viz first.")
        sys.exit(1)

    meta  = read_meta(viz_dir)
    ideal = load(os.path.join(viz_dir, "ideal.txt"))
    scheme = meta.get("scheme", "")
    if "fs" in meta:
        try: fs = float(meta["fs"])
        except ValueError: pass

    fig, ax = plt.subplots(2, 3, figsize=(15, 8))
    title = f"SDR signals  ({scheme}  fs={fs/1e6:.2f} MHz"
    if meta.get("waveform"): title += f"  {meta['waveform']}"
    if meta.get("fec") == "1": title += "  +FEC"
    fig.suptitle(title + ")", fontsize=13)

    for row, side in enumerate(["tx", "rx"]):
        wave = sig[f"{side}_wave"]
        syms = sig[f"{side}_symbols"]
        color = "tab:blue" if side == "tx" else "tab:red"

        # time domain (I and Q)
        a = ax[row][0]
        if wave is not None:
            m = min(len(wave), 600)
            a.plot(np.real(wave[:m]), color=color, lw=0.8, label="I")
            a.plot(np.imag(wave[:m]), color="tab:gray", lw=0.8, label="Q")
            a.legend(loc="upper right", fontsize=8)
        a.set_title(f"{side.upper()} time domain"); a.set_xlabel("sample"); a.set_ylabel("amplitude")

        # spectrum
        a = ax[row][1]
        if wave is not None:
            f, mag = spectrum_db(wave, fs)
            a.plot(f / 1e6, mag, color=color, lw=0.8)
            a.set_ylim(-60, 3)
        a.set_title(f"{side.upper()} spectrum"); a.set_xlabel("freq (MHz)"); a.set_ylabel("dB")
        a.grid(True, alpha=0.3)

        # constellation (+ ideal points overlaid, + EVM readout)
        a = ax[row][2]
        title = f"{side.upper()} constellation"
        if syms is not None:
            # normalize to unit average power so it overlays the ideal points
            s = syms / (np.sqrt(np.mean(np.abs(syms) ** 2)) + 1e-12)
            i_norm = ideal / (np.sqrt(np.mean(np.abs(ideal) ** 2)) + 1e-12) if ideal is not None else None
            a.scatter(np.real(s), np.imag(s), s=6, color=color, alpha=0.35)
            if i_norm is not None:
                a.scatter(np.real(i_norm), np.imag(i_norm), s=90, facecolors="none",
                          edgecolors="k", marker="o", linewidths=1.2, label="ideal")
                a.legend(loc="upper right", fontsize=8)
            e = evm_pct(syms, ideal)
            if e is not None:
                title += f"   EVM={e:.1f}%"
            lim = 1.7
            a.set_xlim(-lim, lim); a.set_ylim(-lim, lim)
            a.axhline(0, color="k", lw=0.4); a.axvline(0, color="k", lw=0.4)
            a.set_aspect("equal")
        a.set_title(title); a.set_xlabel("I"); a.set_ylabel("Q")
        a.grid(True, alpha=0.3)

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    if save:
        plt.savefig(save, dpi=120)
        print(f"saved {save}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
