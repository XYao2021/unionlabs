# Ready-to-run commands — per modulation scheme (sc & OFDM)

Copy-paste TX/RX command pairs with **gains tuned per scheme** from over-the-air
testing @915 MHz (two B210s, VERT900 antennas ~10 cm apart). All use
**stop-and-wait ARQ** (TCP ACK on localhost) + **FEC**, and auto-save plots to
`viz/<scheme>/figure.png`.

**Higher-order QAM (16-QAM and up) is intentionally omitted** — those need a
cleaner link (SMA cable + attenuator); the ~10 cm OTA link floors at ~28–31 %
EVM, too noisy for them. See the cable-link note at the bottom.

## Fixed setup (this rig)

| Role | UHD serial | subdev | antenna |
|---|---|---|---|
| RX / sink   | `30CD3F7` | `A:A` | `RX2`   |
| TX / source | `30CD424` | `A:A` | `TX/RX` |

Common: `--rx-freq/--tx-freq 915e6`, `--rx-rate/--tx-rate 1.6e6`,
`--ack-transport tcp --ack-port 5599 --ack-host 127.0.0.1 --det-mult 3 --fec true`.

**Always start the sink (RX) first**, then the source (TX). Run each in its own terminal.

## Tuned gains per scheme (why they differ)

| Scheme | Waveform | `--rx-gain` | `--tx-gain` | extra | OTA status |
|---|---|---|---|---|---|
| BPSK    | sc   | 20 | 78 | — | ✅ very robust |
| QPSK    | sc   | 20 | 78 | — | ✅ solid (5/5) |
| 8-PSK   | sc   | 16 | 86 | — | ✅ usable — a few retransmits (EVM ~16 %) |
| DBPSK   | sc   | 20 | 78 | — | ✅ solid (5/5) |
| DQPSK   | sc   | 20 | 78 | — | ✅ solid (5/5) |
| 8-DPSK  | sc   | 16 | 86 | — | ✅ usable — a few retransmits |
| BPSK    | ofdm | 22 | 80 | `--ofdm-tx-peak 0.5` | ✅ robust |
| QPSK    | ofdm | 22 | 80 | `--ofdm-tx-peak 0.5` | ✅ solid |

The pattern: **8-ary schemes (8-PSK/8-DPSK) need more TX power (86) and lower RX
gain (16)** — a stronger signal buys the SNR their tighter decision regions
demand, without overdriving the front end. The robust schemes are comfortable at
20/78. OFDM keeps TX a touch lower + `--ofdm-tx-peak 0.5` because of its high PAPR.

Differential schemes run on the default `--eq_type None` (single-carrier, flat
link). **Don't combine differential with OFDM** — OFDM's pilots already handle
phase, and differential fights its per-symbol CPE tracking.

---

# Single-carrier (`--waveform sc`, the default)

## BPSK — sc
```bash
# RX / sink  (terminal 1)
./sdr_system --role sink_arq --rx-args serial=30CD3F7 --tx-args serial=30CD3F7 \
  --rx-subdev A:A --rx-ant RX2 --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 20 \
  --scheme BPSK --fec true --ack-transport tcp --ack-port 5599 --det-mult 3

# TX / source  (terminal 2)
./sdr_system --role source_arq --tx-args serial=30CD424 --rx-args serial=30CD424 \
  --tx-subdev A:A --tx-ant TX/RX --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 78 \
  --scheme BPSK --fec true --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599 --timeout 3000
```

## QPSK — sc
```bash
# RX / sink
./sdr_system --role sink_arq --rx-args serial=30CD3F7 --tx-args serial=30CD3F7 \
  --rx-subdev A:A --rx-ant RX2 --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 20 \
  --scheme QPSK --fec true --ack-transport tcp --ack-port 5599 --det-mult 3

# TX / source
./sdr_system --role source_arq --tx-args serial=30CD424 --rx-args serial=30CD424 \
  --tx-subdev A:A --tx-ant TX/RX --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 78 \
  --scheme QPSK --fec true --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599 --timeout 3000
```

## 8-PSK — sc  (more TX power, lower RX gain)
```bash
# RX / sink
./sdr_system --role sink_arq --rx-args serial=30CD3F7 --tx-args serial=30CD3F7 \
  --rx-subdev A:A --rx-ant RX2 --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 16 \
  --scheme 8-PSK --fec true --ack-transport tcp --ack-port 5599 --det-mult 3

# TX / source
./sdr_system --role source_arq --tx-args serial=30CD424 --rx-args serial=30CD424 \
  --tx-subdev A:A --tx-ant TX/RX --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 86 \
  --scheme 8-PSK --fec true --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599 --timeout 3000
```

## DBPSK — sc  (differential; no PLL needed)
```bash
# RX / sink
./sdr_system --role sink_arq --rx-args serial=30CD3F7 --tx-args serial=30CD3F7 \
  --rx-subdev A:A --rx-ant RX2 --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 20 \
  --scheme DBPSK --fec true --ack-transport tcp --ack-port 5599 --det-mult 3

# TX / source
./sdr_system --role source_arq --tx-args serial=30CD424 --rx-args serial=30CD424 \
  --tx-subdev A:A --tx-ant TX/RX --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 78 \
  --scheme DBPSK --fec true --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599 --timeout 3000
```

## DQPSK — sc  (differential)
```bash
# RX / sink
./sdr_system --role sink_arq --rx-args serial=30CD3F7 --tx-args serial=30CD3F7 \
  --rx-subdev A:A --rx-ant RX2 --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 20 \
  --scheme DQPSK --fec true --ack-transport tcp --ack-port 5599 --det-mult 3

# TX / source
./sdr_system --role source_arq --tx-args serial=30CD424 --rx-args serial=30CD424 \
  --tx-subdev A:A --tx-ant TX/RX --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 78 \
  --scheme DQPSK --fec true --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599 --timeout 3000
```

## 8-DPSK — sc  (differential; more TX power like 8-PSK)
```bash
# RX / sink
./sdr_system --role sink_arq --rx-args serial=30CD3F7 --tx-args serial=30CD3F7 \
  --rx-subdev A:A --rx-ant RX2 --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 16 \
  --scheme 8-DPSK --fec true --ack-transport tcp --ack-port 5599 --det-mult 3

# TX / source
./sdr_system --role source_arq --tx-args serial=30CD424 --rx-args serial=30CD424 \
  --tx-subdev A:A --tx-ant TX/RX --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 86 \
  --scheme 8-DPSK --fec true --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599 --timeout 3000
```

---

# OFDM (`--waveform ofdm`, 64 subcarriers, CP 16)

## BPSK — OFDM
```bash
# RX / sink
./sdr_system --role sink_arq --rx-args serial=30CD3F7 --tx-args serial=30CD3F7 \
  --rx-subdev A:A --rx-ant RX2 --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 22 \
  --waveform ofdm --ofdm-fft 64 --ofdm-cp 16 --scheme BPSK --fec true \
  --ack-transport tcp --ack-port 5599 --det-mult 3

# TX / source
./sdr_system --role source_arq --tx-args serial=30CD424 --rx-args serial=30CD424 \
  --tx-subdev A:A --tx-ant TX/RX --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 80 \
  --waveform ofdm --ofdm-fft 64 --ofdm-cp 16 --ofdm-tx-peak 0.5 --scheme BPSK --fec true \
  --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599 --timeout 3000
```

## QPSK — OFDM
```bash
# RX / sink
./sdr_system --role sink_arq --rx-args serial=30CD3F7 --tx-args serial=30CD3F7 \
  --rx-subdev A:A --rx-ant RX2 --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 22 \
  --waveform ofdm --ofdm-fft 64 --ofdm-cp 16 --scheme QPSK --fec true \
  --ack-transport tcp --ack-port 5599 --det-mult 3

# TX / source
./sdr_system --role source_arq --tx-args serial=30CD424 --rx-args serial=30CD424 \
  --tx-subdev A:A --tx-ant TX/RX --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 85 \
  --waveform ofdm --ofdm-fft 64 --ofdm-cp 16 --ofdm-tx-peak 0.5 --scheme QPSK --fec true \
  --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599 --timeout 3000
```

---

## Tips
- Run from `build/` (the binary is `build/sdr_system`); prefix `./`.
- Watch the RX `Peak=` line: post-AGC PAPR of ~1.2–1.4 is normal (not ADC
  clipping). If chunks never decode *and* the raw front end looks saturated,
  lower `--rx-gain`. If detection never fires (`bursts=0`), raise a gain.
- The sink auto-stops once all chunks are CRC-verified and writes the figure.
- Plots off: add `--viz false`. Separate runs: change `--viz-dir`.
- **One-way (no ACK)** instead of ARQ: use `--role rx` / `--role tx --tx-reps 20`
  (drop the `--ack-*` flags).

## 16-QAM and higher — blocked without a shared clock
**Validated ceiling on this rig is QPSK / 8-PSK.** 16-QAM+ does **not** decode, and a clean cable
is necessary but not sufficient. On a direct cable the SNR is excellent and QPSK works, but the two
B210s are **free-running** (independent TCXOs) and the TX carrier leakage beats at that CFO — a
drifting near-DC tone that rotates the constellation (16-QAM can't tolerate it; the phase PLL can't
lock 16 points) and dominates the AGC (OFDM → blob). Static DC removal, an RX DC-block high-pass
(`--dc-block`), and a manual TX LO-leakage null (`--tx-dc-i/--tx-dc-q`) were all tried and none
unblock it (details in `SYSTEM_REFERENCE.md` §13).

**The fix is a shared 10 MHz reference:** feed one 10 MHz source (signal generator / GPSDO /
OctoClock) into both radios' `REF IN` and run both with `--ref external`. That makes CFO ≈ 0 and
turns the leakage into a removable static DC, so dense QAM decodes. Once that's in place, start from
`--rx-gain 21 --tx-gain 80`, lower `--rx-gain` / add attenuation until the RX front end isn't
saturated, keep `--fec true`; higher orders need progressively lower EVM (16-QAM ≲ 12 %, 64-QAM ≲
6 %, 256-QAM ≲ 3 %).
