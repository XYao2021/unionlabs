#!/usr/bin/env python3
"""
phy_flow_example.py — GNU-Radio-style flowgraphs built from the pyphy blocks.

Every PHY stage is a numpy-in / numpy-out function, so you COMPOSE the chain and
drop your own ops between any two blocks (here a digital gain between `modulate`
and the RRC pulse shaper — impossible in the monolithic sdr_system).

Blocks:
  framing   : frame / unframe
  fec       : fec_encode / fec_decode / fec_decode_soft        (conv|ldpc|turbo)
  mod       : modulate / demodulate / soft_llr
  sc filter : rrc_tx / rrc_rx
  sync      : preamble / acq / cfo_correct / phase_correct
  ofdm      : ofdm_mod / ofdm_demod / ofdm_data_per_sym
  radio     : Radio('tx'|'rx', ...).transmit(wave) / .capture(n)   (WITH_UHD build)

Build:  bindings/build.sh   (add WITH_UHD=1 on the lab host for the Radio block)
Run:    PYTHONPATH=bindings python3 python/phy_flow_example.py
"""
import numpy as np
import pyphy

SPS, SYMRATE, BETA = 2, 0.8e6, 0.25

# ============================================================
#  1) Single-carrier flowgraph with sync + a digital-gain insert
# ============================================================
coded = (np.random.RandomState(0).rand(2000) > 0.5).astype(np.uint8)   # 1000 QPSK sym
data  = pyphy.modulate(coded, "QPSK")

data  = data * 0.6                              # <<< your op between modulate and RRC

pre   = pyphy.preamble(5)                       # 31-symbol m-sequence
pkt   = np.concatenate([pre, data]).astype(np.complex64)
wave  = pyphy.rrc_tx(pkt, sps=SPS, beta=BETA)   # pulse shape -> baseband waveform
# --- hand `wave` to the radio here (built WITH_UHD):
#     tx = pyphy.Radio("tx", "serial=30CD424", freq=915e6, rate=SPS*SYMRATE,
#                      symbol_rate=SYMRATE, gain=78, subdev="A:A", ant="TX/RX")
#     tx.transmit(wave); tx.close()

# --- receive chain (loopback here; on hardware: rx.capture(len(wave)+pad)) ---
mf   = pyphy.rrc_rx(wave, sps=SPS, beta=BETA)                # matched filter
ph   = max(range(SPS), key=lambda p: pyphy.acq(mf[p::SPS].astype(np.complex64),
                                               pre, len(data), 1, 15.0)[2])
al, det, peak, tau = pyphy.acq(mf[ph::SPS].astype(np.complex64), pre, len(data), 1, 15.0)
c, cfo = pyphy.cfo_correct(al, pre, SYMRATE, 1, "pilot_ls")  # remove CFO
p, deg = pyphy.phase_correct(c, pre, "QPSK")                 # remove carrier phase
rx = pyphy.demodulate(p[len(pre):][:len(data)].astype(np.complex64), "QPSK")
print(f"SC:   ACQ peak={peak:.1f}/31  CFO={cfo:+.0f}Hz  phase={deg:+.1f}deg  "
      f"bit errors={int(np.sum(rx[:len(coded)]!=coded))}/{len(coded)}")

# ============================================================
#  2) OFDM flowgraph  (coherent QPSK; ofdm_demod does sync/CFO/EQ)
# ============================================================
c2  = (np.random.RandomState(1).rand(2000) > 0.5).astype(np.uint8)
qam = pyphy.modulate(c2, "QPSK")
frame = pyphy.ofdm_mod(qam, fft=64, cp=16)                  # -> OFDM time-domain frame
burst = np.concatenate([np.zeros(40, np.complex64), frame, np.zeros(40, np.complex64)])
qrx, start, cfo_sc = pyphy.ofdm_demod(burst.astype(np.complex64), len(qam), 64, 16)
rb = pyphy.demodulate(qrx.astype(np.complex64), "QPSK")
print(f"OFDM: frame@{start}  CFO={cfo_sc:.3f}sc  "
      f"bit errors={int(np.sum(rb[:len(c2)]!=c2))}/{len(c2)}")

print(f"\nRadio block available in this build: {pyphy.HAS_RADIO}"
      + ("" if pyphy.HAS_RADIO else "   (rebuild with WITH_UHD=1 on the lab host)"))
