#!/usr/bin/env python3
"""
stc_core.py — STLC-based digital over-the-air computation (AirComp), the DSP core of the
AJOU / STC-AirComp application (Lee–Lee–Jung, IEEE WCL vol. 15, 2026).

Unlike every other app in this repo, the receiver does NOT decode one device's packet — the
N sensors transmit *at the same time* and the wireless medium SUMS their signals; that
superposition IS the computation. Each sensor pre-shapes (STLC-precodes) its symbols with its
own local CSI so a fixed, CSI-FREE combine at a 2-antenna access point returns the aggregate.

Pipeline (per the design note INTEGRATION.md §3):
    sensor i:  v_i -> quantize -> bit-planes -> {BPSK symbol per plane}
               STLC-precode each symbol pair (s1,s2) over 2 slots using local CSI (h_i1,h_i2)
               transmit simultaneously with all sensors
    the air:   r_m[t] = Σ_i h_im x_i[t] + noise            (the sum over i = the compute)
    AP:        stlc_combine(r1, r2) -> (ŝ1,ŝ2) ≈ (Σ_i s1_i, Σ_i s2_i)   (CSI-FREE)
               per-plane count k_b = (Re ŝ_b + N)/2  ->  Σ_i v_i = Σ_b 2^b k_b

STLC algebra used below (1 Tx antenna, M=2 Rx antennas, 2 slots, 2 symbols), verified:
    x[1] = (h1* s1 + h2* s2) / β ,  x[2] = (h2* s1* - h1* s2*) / β ,   β = |h1|²+|h2|²
    ŝ1 = r1[1] + conj(r2[2]) = β·s1 / (per-sensor β)   -> with the /β channel-inversion above,
    ŝ2 = r2[1] - conj(r1[2])                              each sensor contributes exactly s to the sum.
Diversity order 2M; the /β normalization equalises sensors so the combine returns Σ s_i.

Pure numpy — validated radio-free first (design §8 phase 1). No radio, no sdr_system.
"""
import numpy as np


# ── digital-AirComp value <-> bit-planes ─────────────────────────────────────
def quantize(values, bits):
    """floats in [0,1] -> integer levels in [0, 2**bits - 1]."""
    L = (1 << bits) - 1
    return np.rint(np.clip(np.asarray(values, float), 0.0, 1.0) * L).astype(np.int64)


def bit_planes(q, bits):
    """q: (N,) ints -> (bits, N) array of {0,1}, plane 0 = LSB."""
    q = np.asarray(q, np.int64)
    return np.stack([(q >> b) & 1 for b in range(bits)], axis=0)


def bpsk(bit):
    """bit {0,1} -> BPSK symbol {-1,+1} so that Σ_i s = 2·(#ones) - N."""
    return 2.0 * np.asarray(bit, float) - 1.0


# ── STLC (transmit-side, local CSI) ──────────────────────────────────────────
def stlc_encode(s1, s2, h1, h2):
    """Sensor precodes symbols (s1,s2) over 2 slots from its own CSI (h1,h2) to the two AP
    antennas. Channel-inversion normalised (÷β) so its combiner contribution equals s
    (making the superposition an unbiased SUM). Returns x[2] (the two slot samples)."""
    beta = np.abs(h1) ** 2 + np.abs(h2) ** 2
    x1 = (np.conj(h1) * s1 + np.conj(h2) * s2) / beta
    x2 = (np.conj(h2) * np.conj(s1) - np.conj(h1) * np.conj(s2)) / beta
    return x1, x2


def stlc_combine(r1, r2):
    """AP-side FIXED, CSI-FREE combine. r1=(r1_slot1,r1_slot2) at antenna 1, r2 at antenna 2.
    Returns (ŝ1, ŝ2). With the encoder's ÷β normalisation, ŝ_k ≈ Σ_i s_k,i."""
    s1 = r1[0] + np.conj(r2[1])
    s2 = r2[0] - np.conj(r1[1])
    return s1, s2


# ── one AirComp codeword: N sensors superpose, 2-antenna AP combines ──────────
def aircomp_codeword(s1_vec, s2_vec, H, snr_db, rng, p_max=None, diversity=True):
    """
    Fire ONE STLC codeword from N sensors simultaneously and combine at the AP.

      s1_vec, s2_vec : (N,) BPSK symbols (two bit-planes) from the N sensors
      H              : (N,2) complex CSI, sensor i -> AP antennas (h_i1, h_i2)
      snr_db         : per-antenna receive SNR (relative to a unit-contribution symbol)
      p_max          : transmit power cap; sensors whose channel-inversion needs more power
                       are TRUNCATED (drop out) — the classic AirComp power-control cost.
      diversity      : True = STLC 2-antenna combine; False = single-antenna baseline (no STLC)

    Returns (ŝ1, ŝ2, n_active) — recovered per-plane sums (complex) and #participating sensors.
    """
    N = H.shape[0]
    beta = np.abs(H[:, 0]) ** 2 + (np.abs(H[:, 1]) ** 2 if diversity else 0.0)
    active = np.ones(N, bool)
    if p_max is not None:
        active = beta >= (1.0 / p_max)          # truncate deep fades (can't invert within P_max)

    r11 = r12 = r21 = r22 = 0j
    for i in range(N):
        if not active[i]:
            continue
        if diversity:
            x1, x2 = stlc_encode(s1_vec[i], s2_vec[i], H[i, 0], H[i, 1])
            r11 += H[i, 0] * x1; r12 += H[i, 0] * x2
            r21 += H[i, 1] * x1; r22 += H[i, 1] * x2
        else:                                    # single-antenna channel-inversion AirComp
            h = H[i, 0]
            r11 += h * (s1_vec[i] / h)           # -> s1_i  (slot 1)
            r12 += h * (s2_vec[i] / h)           # -> s2_i  (slot 2)

    sig = np.sqrt(max(1, active.sum()))          # ~ signal scale for SNR reference
    sigma = sig * 10 ** (-snr_db / 20.0) / np.sqrt(2)
    def noise():
        return sigma * (rng.standard_normal() + 1j * rng.standard_normal())
    r11 += noise(); r12 += noise(); r21 += noise(); r22 += noise()

    if diversity:
        s1, s2 = stlc_combine((r11, r12), (r21, r22))
    else:
        s1, s2 = r11, r12                        # single antenna: no combine, just the sum
    return s1, s2, int(active.sum())


def aggregate(plane_sums, N, bits):
    """plane_sums: list of `bits` recovered Σ-of-BPSK values (complex). Recover per-plane
    counts k_b = round((Re + N)/2) in [0,N], then Σ_i v_i = Σ_b 2^b k_b."""
    est = 0.0
    for b in range(bits):
        kb = np.clip(np.round((np.real(plane_sums[b]) + N) / 2.0), 0, N)
        est += (2 ** b) * kb
    return est


def nmse(est, true):
    est = np.asarray(est, float); true = np.asarray(true, float)
    denom = np.mean(true ** 2)
    return float(np.mean((est - true) ** 2) / denom) if denom > 0 else float("nan")
