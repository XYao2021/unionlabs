#!/usr/bin/env python3
"""
stc_aircomp/app.py — STC-AirComp as an uploaded algorithm (the COMPUTE / aggregation archetype).

Unlike echo/fl/clip (data transfer) or marl (control/ACK), this app does over-the-air
COMPUTATION: N sensors transmit SIMULTANEOUSLY and the wireless medium sums them; the access
point recovers the aggregate Σ_i v_i with a CSI-free STLC 2-antenna combine. The algorithm side
is tiny — each sensor only declares *what value it contributes* (produce) and *reads back the
network-wide sum* (on_aggregate). All the STLC precoding / superposition / combine lives in the
reused library `applications/STC_AirComp_Union/stc_core.py`.

Connection to the PHY: the sensors have NO radio code. The multi-node driver `run()` below hands
the simultaneously-transmitted symbols to the medium via `stc_core.aircomp_codeword(...)` — the
marked PORT TO THE PHY, where the superposition (the sum) happens. Swap that simulated superposition
for a real 2-antenna capture over USRPs and the same algorithm computes over the air.

Run:  ./run.sh --algo stc_aircomp --role aircomp --agents 8 --snr-db 15 --steps 200
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "applications", "STC_AirComp_Union"))
import stc_core as sc                                            # the reused DSP library  # noqa: E402

_next = [0]                                                      # distinct seed per sensor


class Sensor:
    """One AirComp sensor. It only knows its own scalar measurement — no radio, no STLC."""

    def __init__(self, role="sensor", seed=0):
        self.role = role
        self.rng = np.random.RandomState(seed)
        self.val = 0.0
        self.aggregate = None

    def produce(self):
        """WHAT TO TRANSMIT: this sensor's current measurement in [0,1]."""
        self.val = float(self.rng.rand())
        return self.val

    def on_aggregate(self, est):
        """WHAT IT RECEIVES: the recovered network-wide sum Σ_i v_i."""
        self.aggregate = est


def make(role="sensor"):
    i = _next[0]; _next[0] += 1
    return Sensor(role, seed=200 + i)


def run(sensors, snr_db=15.0, bits=4, p_max=20.0, steps=200, seed=0, verbose=True):
    """MULTI-NODE COMPUTE driver: every round all sensors produce a value and transmit at once;
    the air sums them; the AP combines + de-quantizes to Σ_i v_i. Returns NMSE over the run."""
    N = len(sensors)
    rng = np.random.RandomState(seed)
    sq_err, sq_true = 0.0, 0.0
    for t in range(steps):
        vals = np.array([s.produce() for s in sensors], float)     # each sensor's scalar
        q = sc.quantize(vals, bits)
        planes = sc.bit_planes(q, bits)
        H = (rng.standard_normal((N, 2)) + 1j * rng.standard_normal((N, 2))) / np.sqrt(2)  # CSI
        plane_sums = []
        for b in range(0, bits, 2):                                # 2 bit-planes per STLC codeword
            s1 = sc.bpsk(planes[b])
            s2 = sc.bpsk(planes[b + 1]) if b + 1 < bits else np.zeros(N)
            # ╔══════════════════════ PORT TO THE PHY ══════════════════════╗
            # ║ the N sensors transmit SIMULTANEOUSLY; the medium sums them  ║
            # ║ (this superposition IS the computation) and the 2-antenna AP ║
            # ║ combines CSI-free. Swap for a real capture2() over USRPs.    ║
            sh1, sh2, _ = sc.aircomp_codeword(s1, s2, H, snr_db, rng, p_max=p_max, diversity=True)
            # ╚═════════════════════════════════════════════════════════════╝
            plane_sums.append(sh1)
            if b + 1 < bits:
                plane_sums.append(sh2)
        est = sc.aggregate(plane_sums, N, bits)                    # recovered Σ v_i
        true = float(q.sum())
        for s in sensors:
            s.on_aggregate(est)                                    # each sensor reads the aggregate
        sq_err += (est - true) ** 2
        sq_true += true ** 2
    nmse = sq_err / sq_true if sq_true > 0 else float("nan")
    if verbose:
        print(f"[stc_aircomp] {N} sensors -> 1 AP, {bits}-bit, SNR={snr_db} dB, {steps} rounds: "
              f"NMSE(Σvᵢ)={nmse:.2e}")
    return dict(nmse=nmse, sensors=N, steps=steps)
