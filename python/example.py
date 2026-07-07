#!/usr/bin/env python3
"""
example.py — how to drive the USRP B210 SDR PHY from Python via sdr.py.

sdr.py is AUTO-GENERATED from `sdr_system --help`, so every C++ option is a
keyword here (hyphens or underscores). Run this file to print commands without
touching hardware; uncomment the .run()/run_pair() calls to actually transmit.
"""
from sdr import SDR, tx, rx, sink_arq, source_arq, both, run_pair, options

# 1) List every exposed option (name, default, help)
print("=== all options ===")
options()

RIG = dict(rx_freq=915e6, tx_freq=915e6, rx_rate=1.6e6, tx_rate=1.6e6,
           rx_subdev="A:A", tx_subdev="A:A", rx_ant="RX2", tx_ant="TX/RX")

# 2) The FOUR role modes — pick whichever you need. .command() just shows the
#    argv (no hardware); .run() actually launches it.
print("\n=== role modes ===")

# (a) TX only  — one radio transmitting
T = tx(tx_args="serial=30CD424", tx_gain=78, scheme="QPSK", fec=True, tx_reps=20, **RIG)
print("TX  :", T.command())        # T.run()

# (b) RX only  — the other radio receiving
R = rx(rx_args="serial=30CD3F7", rx_gain=20, scheme="QPSK", fec=True, det_mult=3, **RIG)
print("RX  :", R.command())        # R.run()

# (c) BOTH in one process — receive AND transmit simultaneously (single-box
#     full-duplex / loopback ARQ)
B = both(tx_args="serial=30CD424", rx_args="serial=30CD3F7",
         tx_gain=78, rx_gain=20, scheme="QPSK", fec=True, **RIG)
print("BOTH:", B.command())        # B.run()

# 3) Two boxes with ARQ OVER THE AIR — separate RX (sink) and TX (source)
#    processes; run_pair() starts the RX first, then the TX, and cleans up.
sink   = sink_arq(rx_args="serial=30CD3F7", tx_args="serial=30CD3F7",
                  rx_gain=20, scheme="QPSK", fec=True, det_mult=3, **RIG)
source = source_arq(tx_args="serial=30CD424", rx_args="serial=30CD424",
                    tx_gain=78, scheme="QPSK", fec=True,
                    ack_host="127.0.0.1", timeout=3000, **RIG)
print("\n=== two-box ARQ (RX + TX at the same time) ===")
print("SINK  :", sink.command())
print("SOURCE:", source.command())
# run_pair(sink, source)        # <-- uncomment to actually run both ends

# 5) Sweep modulations easily
print("\n=== sweep schemes ===")
for scheme in ("BPSK", "QPSK", "8-PSK"):
    print(scheme, "->", source_arq(scheme=scheme, tx_gain=78).command())

# 6) Random-bit throughput test + a test tone
print("\n=== random + tone ===")
print(source_arq(message_type="random", num_bits=2000, scheme="QPSK").command())
print(tx(message_type="cosine", tone_freq=200e3, tx_mode="continuous").command())
# run_pair(rx(message_type="cosine"), tx(message_type="cosine", tone_freq=200e3))
