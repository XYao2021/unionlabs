#!/usr/bin/env python3
"""
ofdm_spectrum.py — reveal the individual OFDM subcarriers ("multiple peaks",
like the theoretical analysis) from a captured signal.

WHY the normal spectrum looks like a filled band, not peaks:
  This modem uses N=64 with 62 active subcarriers (all bins except DC and
  Nyquist), each carrying random data. FFT the whole multi-symbol waveform
  with a smoothing window and the 62 random-amplitude spikes merge into a
  solid band. That IS the true spectrum of a fully-loaded OFDM signal.

To resolve the subcarriers you must analyze ONE OFDM symbol in isolation:
  strip its cyclic prefix, take exactly N samples, apply NO window
  (the OFDM symbol is already a rectangular sum of complex exponentials),
  and zero-pad the FFT. Each subcarrier then shows up as its own sinc lobe,
  and — the key orthogonality property — every lobe peaks exactly where all
  the others pass through zero.

We read tx_wave.txt (the TX-side capture, which is aligned to the frame
start: [SC-sync | channel-est | data...], each symbol N+CP samples). The
channel-estimation symbol (index 1) carries known BPSK on EVERY active
subcarrier, so it gives the cleanest, flat comb — that's the default.

Usage:
    python3 tools/ofdm_spectrum.py viz/QPSK [--fft 64] [--cp 16] \
            [--sym 1] [--pad 32] [--save out.png]
Requires: numpy, matplotlib.
"""
import sys, os
import numpy as np
import matplotlib.pyplot as plt


def load_iq(path):
    d = np.loadtxt(path, comments="#")
    if d.ndim == 1:
        d = d.reshape(1, -1)
    return d[:, 0] + 1j * d[:, 1]


def main():
    a = sys.argv[1:]
    viz_dir, N, cp, sym, pad, save = "viz", 64, 16, 1, 32, None
    i = 0
    while i < len(a):
        if   a[i] == "--fft":  N   = int(a[i+1]);   i += 2
        elif a[i] == "--cp":   cp  = int(a[i+1]);   i += 2
        elif a[i] == "--sym":  sym = int(a[i+1]);   i += 2
        elif a[i] == "--pad":  pad = int(a[i+1]);   i += 2
        elif a[i] == "--save": save = a[i+1];       i += 2
        else:                  viz_dir = a[i];      i += 1

    path = os.path.join(viz_dir, "tx_wave.txt")
    if not os.path.exists(path):
        print(f"no tx_wave.txt in '{viz_dir}/'. Run sdr_system --waveform ofdm --viz first.")
        sys.exit(1)
    wave = load_iq(path)

    sym_len = N + cp
    # Symbol `sym` useful part: skip the whole-frame preamble symbols' CP too.
    start = sym * sym_len + cp
    if start + N > len(wave):
        print(f"capture too short for symbol {sym} (have {len(wave)} samples, "
              f"need {start+N}). Try a smaller --sym.")
        sys.exit(1)
    x = wave[start:start + N]                       # one symbol, CP removed

    # Zero-padded FFT, NO window (rectangular) -> sinc interpolation between
    # subcarriers reveals the individual lobes and their orthogonality.
    Z = N * pad
    X = np.fft.fftshift(np.fft.fft(x, Z))
    mag = np.abs(X) / N
    # frequency axis in *subcarrier* units: -N/2 .. +N/2
    f = np.fft.fftshift(np.fft.fftfreq(Z, d=1.0 / N))

    # subcarrier-center samples (integer bins) = the "peaks"
    Xc = np.fft.fftshift(np.fft.fft(x, N))
    kc = np.fft.fftshift(np.fft.fftfreq(N, d=1.0 / N))
    magc = np.abs(Xc) / N

    fig, ax = plt.subplots(2, 1, figsize=(13, 9))
    fig.suptitle(f"OFDM subcarrier structure — one symbol (N={N}, CP={cp}, "
                 f"symbol #{sym}), {viz_dir}", fontsize=13)

    # ── full band: the comb of active subcarriers, DC + Nyquist nulled ──
    ax[0].plot(f, mag, lw=0.9, color="tab:blue", label="zero-padded FFT (sinc lobes)")
    ax[0].stem(kc, magc, linefmt="tab:red", markerfmt="ro", basefmt=" ",
               label="subcarrier centers (peaks)")
    ax[0].set_title("Each active subcarrier is its own peak "
                    "(random data → different heights; DC & Nyquist are nulled)")
    ax[0].set_xlabel("subcarrier index  (k = f / Δf,  Δf = fs/N)")
    ax[0].set_ylabel("|X(f)|")
    ax[0].set_xlim(-N/2, N/2)
    ax[0].grid(True, alpha=0.3); ax[0].legend(loc="upper right", fontsize=8)

    # ── orthogonality (theory): each subcarrier's OWN sinc, drawn separately.
    # For an N-sample rectangular symbol, subcarrier k has spectrum
    #   D_N(f-k) = sin(πN(f-k)/N) / (N·sin(π(f-k)/N))   (Dirichlet kernel),
    # which peaks at f=k and is EXACTLY zero at every other integer subcarrier
    # → that is why the subcarriers don't interfere despite overlapping.
    ff = np.linspace(-6, 6, 4000)
    def dirichlet(nu):                       # nu = f - k, in subcarrier units
        num = np.sin(np.pi * nu)
        den = N * np.sin(np.pi * nu / N)
        out = np.where(np.abs(den) < 1e-12, 1.0, num / den)
        return np.abs(out)
    for k in range(-5, 6):
        ax[1].plot(ff, dirichlet(ff - k), lw=1.0, alpha=0.8)
    for k in range(-5, 6):
        ax[1].plot(k, 1.0, "ko", ms=4)
        ax[1].axvline(k, color="gray", lw=0.4, ls=":")
    ax[1].set_title("Orthogonality (theory): each subcarrier's sinc peaks at its own "
                    "index and is exactly ZERO at every other subcarrier")
    ax[1].set_xlabel("subcarrier index  (Δf = fs/N spacing)")
    ax[1].set_ylabel("|subcarrier response|")
    ax[1].set_xlim(-6, 6); ax[1].set_ylim(-0.05, 1.1); ax[1].grid(True, alpha=0.3)

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    if save:
        plt.savefig(save, dpi=120); print(f"saved {save}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
