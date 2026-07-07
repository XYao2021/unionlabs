#!/usr/bin/env python3
"""
make_reference_figures.py — generate the explanatory plots embedded in
SYSTEM_REFERENCE.md / .pdf. Writes PNGs into figures/.

Pure numpy + matplotlib (no SDR deps). Run:  python3 tools/make_reference_figures.py
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from numpy.fft import fft, fftshift, fftfreq

OUT = os.path.join(os.path.dirname(__file__), "..", "figures")
os.makedirs(OUT, exist_ok=True)
plt.rcParams.update({"figure.dpi": 130, "font.size": 10, "axes.grid": True,
                     "grid.alpha": 0.3, "figure.facecolor": "white",
                     "axes.facecolor": "white"})

def save(fig, name):
    p = os.path.join(OUT, name)
    fig.tight_layout(); fig.savefig(p, bbox_inches="tight"); plt.close(fig)
    print("wrote", os.path.relpath(p))

def Q(x):
    from math import erfc, sqrt
    return 0.5 * np.array([erfc(v / np.sqrt(2)) for v in np.atleast_1d(x)])


# ── 1. Constellations + decision regions + d_min (§2) ───────────────────────
def fig_constellations():
    rng = np.random.default_rng(1)
    fig, ax = plt.subplots(1, 2, figsize=(10, 5))
    # QPSK
    q = (np.array([1+1j, 1-1j, -1+1j, -1-1j]) / np.sqrt(2))
    for a, pts, name, evm in [(ax[0], q, "QPSK  (2 bits/sym)", 0.28),
                              (ax[1],
                               (np.array([x+1j*y for y in (-3,-1,1,3) for x in (-3,-1,1,3)])
                                / np.sqrt(10)), "16-QAM  (4 bits/sym)", 0.28)]:
        # noisy cloud at the given EVM
        M = len(pts)
        idx = rng.integers(0, M, 1500)
        noise = evm * (rng.standard_normal(1500) + 1j*rng.standard_normal(1500)) / np.sqrt(2)
        y = pts[idx] + noise
        a.plot(y.real, y.imag, '.', ms=2, color="tab:blue", alpha=0.25)
        a.plot(pts.real, pts.imag, 'o', ms=9, mfc="none", mec="k", mew=1.6)
        # decision-region grid lines (midpoints between levels)
        lv = np.unique(pts.real)
        for b in (lv[:-1] + lv[1:]) / 2:
            a.axvline(b, color="tab:red", lw=0.7, ls="--")
            a.axhline(b, color="tab:red", lw=0.7, ls="--")
        # d_min arrow between two adjacent points
        if M == 4:
            p0, p1 = q[3], q[1]   # (-1-1j),( 1-1j) horizontal pair
        else:
            p0, p1 = pts[0], pts[1]
        a.annotate("", xy=(p1.real, p1.imag), xytext=(p0.real, p0.imag),
                   arrowprops=dict(arrowstyle="<->", color="tab:green", lw=1.8))
        a.text((p0.real+p1.real)/2, (p0.imag+p1.imag)/2 - 0.18, r"$d_{min}$",
               color="tab:green", ha="center", fontsize=11)
        a.set_title(name); a.set_xlabel("I"); a.set_ylabel("Q")
        a.set_aspect("equal"); a.set_xlim(-1.6, 1.6); a.set_ylim(-1.6, 1.6)
    fig.suptitle("Minimum-distance decision: denser constellations shrink "
                 r"$d_{min}$ (same noise cloud, EVM $\approx$ 28%)", fontsize=11)
    save(fig, "constellations.png")


# ── 2. RRC pulse + spectrum (§3.1) ──────────────────────────────────────────
def rrc(beta, sps, span):
    N = span * sps
    t = (np.arange(-N/2, N/2 + 1)) / sps
    h = np.zeros_like(t)
    for i, ti in enumerate(t):
        if abs(ti) < 1e-8:
            h[i] = 1 - beta + 4*beta/np.pi
        elif beta > 0 and abs(abs(ti) - 1/(4*beta)) < 1e-6:
            h[i] = (beta/np.sqrt(2))*((1+2/np.pi)*np.sin(np.pi/(4*beta))
                    + (1-2/np.pi)*np.cos(np.pi/(4*beta)))
        else:
            h[i] = (np.sin(np.pi*ti*(1-beta)) + 4*beta*ti*np.cos(np.pi*ti*(1+beta))) \
                   / (np.pi*ti*(1-(4*beta*ti)**2))
    return t, h/np.max(h)

def fig_rrc():
    fig, ax = plt.subplots(1, 2, figsize=(11, 4.3))
    sps, span = 16, 8
    for beta in (0.25, 0.5, 1.0):
        t, h = rrc(beta, sps, span)
        ax[0].plot(t, h, lw=1.3, label=fr"$\beta$={beta}")
        H = fftshift(fft(h, 4096)); H = np.abs(H)/np.max(np.abs(H))
        f = fftshift(fftfreq(4096, d=1/sps))
        ax[1].plot(f, 20*np.log10(H + 1e-6), lw=1.3, label=fr"$\beta$={beta}")
    for k in range(-span, span+1):
        ax[0].axvline(k, color="gray", lw=0.3, ls=":")
    ax[0].axhline(0, color="k", lw=0.5)
    ax[0].set_title("RRC impulse response\n(zero crossings at integer symbol times = no ISI)")
    ax[0].set_xlabel("time  (symbols)"); ax[0].set_ylabel("amplitude"); ax[0].legend()
    ax[1].set_title("RRC magnitude spectrum\n(occupied BW = (1+$\\beta$)·$R_{sym}$)")
    ax[1].set_xlabel("frequency  (× symbol rate)"); ax[1].set_ylabel("dB")
    ax[1].set_xlim(-1.2, 1.2); ax[1].set_ylim(-60, 3); ax[1].legend()
    save(fig, "rrc.png")


# ── 3. OFDM subcarrier orthogonality (§3.2) ─────────────────────────────────
def fig_ofdm_orth():
    N = 16
    fig, ax = plt.subplots(figsize=(11, 4.3))
    ff = np.linspace(-4, 4, 2000)
    def dirichlet(nu):
        num, den = np.sin(np.pi*nu), N*np.sin(np.pi*nu/N)
        return np.abs(np.where(np.abs(den) < 1e-9, 1.0, num/den))
    for k in range(-4, 5):
        ax.plot(ff, dirichlet(ff-k), lw=1.1, alpha=0.85)
    for k in range(-4, 5):
        ax.plot(k, 1, "ko", ms=4); ax.axvline(k, color="gray", lw=0.3, ls=":")
    ax.set_title("OFDM subcarrier orthogonality — each subcarrier's sinc peaks at its own "
                 "index\nand is exactly ZERO at every other subcarrier")
    ax.set_xlabel(r"subcarrier index  ($\Delta f = f_s/N$ spacing)")
    ax.set_ylabel("|subcarrier response|"); ax.set_xlim(-4, 4); ax.set_ylim(-0.05, 1.1)
    save(fig, "ofdm_orthogonality.png")


# ── 4. CFO effect on the constellation (§5.0 / 5.4) ─────────────────────────
def fig_cfo():
    rng = np.random.default_rng(3)
    q = (np.array([1+1j,1-1j,-1+1j,-1-1j])/np.sqrt(2))
    Nn = 800
    s = q[rng.integers(0,4,Nn)] + 0.05*(rng.standard_normal(Nn)+1j*rng.standard_normal(Nn))
    fig, ax = plt.subplots(1, 3, figsize=(12, 4.2))
    for a, eps, title in [(ax[0], 0.0, "no CFO"),
                          (ax[1], 2e-4, r"small residual CFO ($\epsilon$)"),
                          (ax[2], 8e-4, "larger residual CFO")]:
        n = np.arange(Nn)
        y = s * np.exp(1j*2*np.pi*eps*n)
        a.plot(y.real, y.imag, '.', ms=2.5, color="tab:red", alpha=0.4)
        a.plot(q.real, q.imag, 'o', ms=9, mfc="none", mec="k", mew=1.6)
        a.set_title(title); a.set_aspect("equal")
        a.set_xlim(-1.3,1.3); a.set_ylim(-1.3,1.3); a.set_xlabel("I")
    ax[0].set_ylabel("Q")
    fig.suptitle(r"A residual CFO makes the carrier phase ramp $\varphi[n]=2\pi\,(\Delta f/f_s)\,n$"
                 " — it spins the constellation into a ring", fontsize=11)
    save(fig, "cfo_effect.png")


# ── 5. Gardner TED S-curve (§5.3) ───────────────────────────────────────────
def fig_gardner():
    beta, sps, span = 0.25, 32, 12
    rng = np.random.default_rng(5)
    Nsym = 4000
    syms = rng.choice([-1, 1], Nsym).astype(float)   # BPSK
    up = np.zeros(Nsym*sps); up[::sps] = syms
    _, h = rrc(beta, sps, span)
    wave = np.convolve(up, h, mode="same")
    offs = np.linspace(-0.5, 0.5, 61)
    err = []
    base = span*sps//2
    for d in offs:
        di = int(round(d*sps))
        centers = np.arange(base, len(wave)-sps, sps) + di
        centers = centers[(centers-sps//2 > 0) & (centers+sps < len(wave))]
        yk   = wave[centers]
        ykm1 = wave[centers-sps]
        mid  = wave[centers-sps//2]
        err.append(np.mean((yk - ykm1) * mid))
    err = np.array(err); err /= np.max(np.abs(err))
    fig, ax = plt.subplots(figsize=(8, 4.4))
    ax.plot(offs, err, lw=1.8, color="tab:purple")
    ax.axhline(0, color="k", lw=0.6); ax.axvline(0, color="tab:green", lw=1, ls="--")
    ax.plot(0, 0, "go", ms=8)
    ax.annotate("lock point\n(e = 0; sign of e\ntells loop which way)", xy=(0, 0),
                xytext=(0.13, 0.42), arrowprops=dict(arrowstyle="->"), fontsize=9)
    ax.set_title("Gardner timing-error detector S-curve\n"
                 r"$e = \mathrm{Re}\{(y_k-y_{k-1})\,y_{k-1/2}^*\}$ vs timing offset")
    ax.set_xlabel("normalized timing offset  (fraction of a symbol)")
    ax.set_ylabel("mean detector output  e  (normalized)")
    save(fig, "gardner_scurve.png")


# ── 6. Schmidl & Cox timing metric (§5.6) ───────────────────────────────────
def fig_schmidl_cox():
    N, cp = 64, 16
    rng = np.random.default_rng(7)
    even = np.zeros(N, complex)
    for k in range(2, N, 2):
        even[k] = rng.choice([-1, 1]) * np.sqrt(2)
    half = np.fft.ifft(even)                 # two identical halves in time
    sym = np.concatenate([half[-cp:], half]) # + CP
    burst = np.concatenate([0.05*(rng.standard_normal(120)+1j*rng.standard_normal(120))/np.sqrt(2),
                            sym,
                            0.05*(rng.standard_normal(120)+1j*rng.standard_normal(120))/np.sqrt(2)])
    eps = 0.15
    burst = burst * np.exp(1j*2*np.pi*eps*np.arange(len(burst))/N)
    L = N//2; M = np.zeros(len(burst)-N)
    for d in range(len(M)):
        P = np.sum(burst[d:d+L] * np.conj(burst[d+L:d+2*L]))
        R = np.sum(np.abs(burst[d+L:d+2*L])**2)
        M[d] = (np.abs(P)**2) / (R**2 + 1e-12)
    fig, ax = plt.subplots(figsize=(8.5, 4.2))
    ax.plot(M, lw=1.6, color="tab:blue")
    ax.set_title("Schmidl & Cox timing metric  "
                 r"$M(d)=|P(d)|^2/R(d)^2$"
                 "\n(plateau where the two identical halves align → frame start; "
                 r"$\hat\epsilon=\angle P/\pi$)")
    ax.set_xlabel("sample offset  d"); ax.set_ylabel("M(d)")
    ax.set_ylim(-0.05, 1.1)
    save(fig, "schmidl_cox.png")


# ── 7. Second-order PLL convergence (§5.5) ──────────────────────────────────
def fig_pll():
    Bn_T, zeta = 0.02, 0.707
    theta = Bn_T/(zeta + 1/(4*zeta))
    a = 4*zeta*theta/(1+2*zeta*theta+theta**2)
    b = 4*theta**2/(1+2*zeta*theta+theta**2)
    Nn = 600
    ramp = 0.02                    # residual-CFO phase step per symbol (rad)
    phi_in = 0.8 + ramp*np.arange(Nn)   # static offset + ramp
    phi, freq = 0.0, 0.0
    err = np.zeros(Nn)
    for n in range(Nn):
        e = phi_in[n] - phi        # phase detector (small-angle)
        err[n] = e
        freq += b*e
        phi  += a*e + freq
    fig, ax = plt.subplots(figsize=(8.5, 4.2))
    ax.plot(err, lw=1.4, color="tab:orange")
    ax.axhline(0, color="k", lw=0.6)
    ax.set_title("Second-order phase PLL tracking a static offset + CFO ramp\n"
                 fr"($B_nT$={Bn_T}, $\zeta$={zeta} → $\alpha$={a:.3f}, $\beta$={b:.4f});"
                 " residual error → 0")
    ax.set_xlabel("symbol index  n"); ax.set_ylabel("phase error  e[n]  (rad)")
    save(fig, "pll_convergence.png")


# ── 8. Energy detector trace (§4) ───────────────────────────────────────────
def fig_energy():
    rng = np.random.default_rng(9)
    Nn = 4000
    noise = 0.1
    p = noise*(rng.standard_normal(Nn)**2 + rng.standard_normal(Nn)**2)/2
    for a, b in [(1200, 1700), (2600, 3100)]:
        p[a:b] += 1.0*(rng.standard_normal(b-a)**2 + rng.standard_normal(b-a)**2)/2
    alpha = 0.95
    filt = np.zeros(Nn); filt[0] = p[0]
    for n in range(1, Nn):
        filt[n] = (1-alpha)*p[n] + alpha*filt[n-1]
    nf = noise      # measured idle noise floor
    thr = nf*5      # adaptive threshold = noise_floor × multiplier
    fig, ax = plt.subplots(figsize=(10, 4.2))
    ax.plot(filt, lw=1.0, color="tab:blue", label="smoothed power (IIR, $\\alpha$=0.95)")
    ax.axhline(nf, color="gray", ls=":", lw=1.2, label="noise floor (EMA)")
    ax.axhline(thr, color="tab:red", ls="--", lw=1.3, label=r"threshold = noise$\times$5")
    ax.fill_between(np.arange(Nn), 0, filt.max()*1.05, where=filt > thr,
                    color="tab:green", alpha=0.12)
    ax.set_title("Energy detector: burst gating by a hypothesis test on smoothed power")
    ax.set_xlabel("sample n"); ax.set_ylabel("power"); ax.set_ylim(0, filt.max()*1.1)
    ax.legend(loc="upper right", fontsize=8)
    save(fig, "energy_detector.png")


# ── 9. BER vs Eb/N0 — order costs SNR (§2) ──────────────────────────────────
def fig_ber():
    ebn0_db = np.linspace(0, 22, 100)
    ebn0 = 10**(ebn0_db/10)
    def qam_ber(M):
        k = np.log2(M)
        return (4/k)*(1-1/np.sqrt(M))*Q(np.sqrt(3*k/(M-1)*ebn0))
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.semilogy(ebn0_db, Q(np.sqrt(2*ebn0)), lw=1.6, label="QPSK (2 b/sym)")
    ax.semilogy(ebn0_db, qam_ber(16), lw=1.6, label="16-QAM (4 b/sym)")
    ax.semilogy(ebn0_db, qam_ber(64), lw=1.6, label="64-QAM (6 b/sym)")
    ax.axhline(1e-3, color="gray", lw=0.8, ls=":")
    ax.set_ylim(1e-6, 1); ax.set_xlim(0, 22)
    ax.set_title("Bit error rate vs $E_b/N_0$ — each extra bit/symbol needs more SNR")
    ax.set_xlabel(r"$E_b/N_0$  (dB)"); ax.set_ylabel("BER"); ax.legend()
    save(fig, "ber_curves.png")


if __name__ == "__main__":
    fig_constellations(); fig_rrc(); fig_ofdm_orth(); fig_cfo(); fig_gardner()
    fig_schmidl_cox(); fig_pll(); fig_energy(); fig_ber()
    print("done.")
