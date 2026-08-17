#!/usr/bin/env python3
"""
stc_aircomp.py — STC-AirComp experiment (design note §8 phase 1: radio-free DSP validation).

N sensors each hold a scalar v_i; they transmit SIMULTANEOUSLY and the air sums them. The AP
recovers the aggregate Σ_i v_i with a CSI-free STLC 2-antenna combine, and we report the
normalized MSE vs SNR — the paper's quality metric. We also compare against a single-antenna
(no-STLC) AirComp baseline to expose STLC's diversity gain under fading + a transmit-power cap.

Run:  python3 experiments/stc_aircomp/stc_aircomp.py --sensors 8 --bits 4
"""
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stc_core as sc                                            # noqa: E402


def run_point(N, bits, snr_db, trials, p_max, diversity, seed):
    """Monte-Carlo one operating point -> NMSE of the recovered Σ v_i."""
    rng = np.random.RandomState(seed)
    ests, trues = [], []
    for _ in range(trials):
        v = rng.rand(N)                                         # sensor values in [0,1]
        q = sc.quantize(v, bits)
        planes = sc.bit_planes(q, bits)                        # (bits, N) in {0,1}
        H = (rng.standard_normal((N, 2)) + 1j * rng.standard_normal((N, 2))) / np.sqrt(2)  # CN(0,1)
        plane_sums = []
        for b in range(0, bits, 2):                            # 2 bit-planes per STLC codeword
            s1 = sc.bpsk(planes[b])
            s2 = sc.bpsk(planes[b + 1]) if b + 1 < bits else np.zeros(N)
            sh1, sh2, _ = sc.aircomp_codeword(s1, s2, H, snr_db, rng,
                                              p_max=p_max, diversity=diversity)
            plane_sums.append(sh1)
            if b + 1 < bits:
                plane_sums.append(sh2)
        est = sc.aggregate(plane_sums, N, bits)
        ests.append(est)
        trues.append(int(q.sum()))                             # true Σ quantized values
    return sc.nmse(ests, trues)


def main():
    ap = argparse.ArgumentParser(description="STC-AirComp radio-free NMSE experiment")
    ap.add_argument("--sensors", type=int, default=8)
    ap.add_argument("--bits", type=int, default=4, help="quantizer bits per sensor value")
    ap.add_argument("--trials", type=int, default=4000)
    ap.add_argument("--p-max", type=float, default=20.0, help="transmit power cap (channel-inv truncation)")
    ap.add_argument("--snr-list", default="-5,0,5,10,15,20,25,30")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "results", "stc_aircomp"))
    a = ap.parse_args()
    snrs = [float(x) for x in a.snr_list.split(",")]

    print(f"[stc] {a.sensors} sensors -> 1 AP (2 ant), {a.bits}-bit values, "
          f"{a.trials} trials/point, P_max={a.p_max}")
    stlc, base = [], []
    for snr in snrs:
        e1 = run_point(a.sensors, a.bits, snr, a.trials, a.p_max, True, a.seed)
        e0 = run_point(a.sensors, a.bits, snr, a.trials, a.p_max, False, a.seed)
        stlc.append(e1); base.append(e0)
        print(f"  SNR={snr:+5.1f} dB   STLC NMSE={e1:.2e}   single-antenna NMSE={e0:.2e}")

    os.makedirs(a.out, exist_ok=True)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 10, "axes.grid": True, "grid.alpha": 0.3,
                         "axes.spines.top": False, "axes.spines.right": False})
    fig, ax = plt.subplots(1, 1, figsize=(7, 4.6))
    ax.semilogy(snrs, base, "o--", color="#c0392b", lw=1.8, label="single-antenna AirComp (no STLC)")
    ax.semilogy(snrs, stlc, "o-", color="#1b7f4b", lw=2.2, label="STLC-AirComp (2-antenna, diversity)")
    ax.set_xlabel("receive SNR (dB)"); ax.set_ylabel("normalized MSE of Σ vᵢ")
    ax.set_title(f"STC-AirComp — {a.sensors} sensors → 1 AP, {a.bits}-bit digital aggregation")
    ax.legend()
    fig.tight_layout()
    png = os.path.join(a.out, "stc_aircomp_nmse.png")
    fig.savefig(png, dpi=140, bbox_inches="tight")
    print(f"[stc] wrote {png}")


if __name__ == "__main__":
    main()
