# Ready-to-run commands — per modulation scheme (sc & OFDM)

Copy-paste TX/RX command pairs with **gains tuned per scheme** from over-the-air
testing @915 MHz (two B210s, VERT900 antennas ~10 cm apart). All use
**stop-and-wait ARQ** (TCP ACK on localhost) + **FEC**, and auto-save plots to
`phy_outputs/<scheme>/figure.png`.

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

## Payload type & transmission mode

`--message-type` selects what is sent; `--tx-mode` (role tx) selects burst (finite,
`--tx-reps` cycles with `--interval` gaps) vs continuous (until Ctrl-C). Works with
any scheme/waveform above.

| `--message-type` | Payload | RX prints | ARQ? |
|---|---|---|---|
| `bytes` (default) | given text — default Star Wars crawl, override with `--message "..."` | the text | yes |
| `random` | `--num_bits` random bits (→ bytes, chunked like text) | byte count + hex, CRC-verified | yes |
| `sine` / `cosine` | raw baseband test tone (`--tone-freq`, `--tone-amp`) | spectrum plot (monitor) | no (role tx/rx only) |

```bash
# Custom text
./sdr_system --role source_arq ... --message-type bytes --message "hello world"

# Random-bit throughput test (2000 bits → 250 bytes → 2 chunks), ARQ terminates normally
./sdr_system --role sink_arq   ... --message-type random --num_bits 2000
./sdr_system --role source_arq ... --message-type random --num_bits 2000

# Continuous data loop (never stops until Ctrl-C)
./sdr_system --role tx ... --scheme QPSK --tx-mode continuous

# Test tone: TX a continuous 200 kHz cosine carrier; monitor it on the other radio
./sdr_system --role tx ... --message-type cosine --tone-freq 200e3 --tone-amp 0.5 --tx-mode continuous
./sdr_system --role rx ... --message-type cosine     # monitor: captures + plots the spectrum
```

Notes: `random` uses a fixed seed (reproducible) and rides the same CRC/FEC/ARQ path
as text, so the payload arrives bit-error-free or not at all. Tones are raw waveforms
(no preamble/CRC) — for a clean RX capture use `--tx-mode burst` so each burst triggers
detection, and set both ends to `--message-type sine|cosine`.

## LoRa / CSS (chirp spread spectrum)

Two chirp features share the same base up-chirp:

**1. Chirp jamming waveform** (`--message-type chirp`) — a raw cyclic frequency sweep,
no decode. The most effective broadband jammer; also a candidate test waveform.

```bash
# continuous full-band up-chirp sweep
./sdr_system --role tx --message-type chirp --chirp-bw 1.6e6 --chirp-sf 8 \
  --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 85 --tx-args serial=30CD424
#   --chirp-bw = sweep Hz (0 = full band), --chirp-sf = 7-12 (2^SF/BW symbol dur),
#   --chirp-down = down-chirp.  See applications/jammer/jammer.py for a full jammer.
```

**2. Decodable LoRa data link** (`--waveform lora`) — CSS modulation that *carries data*:
bits → cyclically-shifted up-chirps; the RX dechirps + FFTs (peak bin = symbol), giving
the LoRa processing gain (decodes below the noise floor). Reuses the CRC/FEC framing.

```bash
# RX (start first)
./sdr_system --role rx --waveform lora --lora-sf 8 --lora-sync-word 18 \
  --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 20 --rx-args serial=30CD3F7
#   -> [LoRa RX] chunk 0/1  CRC=OK  payload="..."
# TX
./sdr_system --role tx --waveform lora --lora-sf 8 --lora-sync-word 18 \
  --message "hello LoRa" --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 80 --tx-args serial=30CD424
```

- `--lora-sf` (7–12): spreading factor; higher = more gain/range, longer symbols.
- `--lora-sync-word` (**default 18 = 0x12 private**, 52 = 0x34 public): the network id,
  sent as 2 sync symbols after the preamble. **The RX rejects frames with a different
  word** — must match on both ends. Frame = `preamble(8 up-chirps) + sync word(2 syms) +
  SFD(2 down-chirps) + data`.

The CSS DSP is validated in-repo by `tools/lora_loopback_test.cpp` (clean / timing / CFO
/ −10 dB SNR / sync-word rejection all pass). One-way for now (no ARQ). See
`SYSTEM_REFERENCE.md` §14.

## Channel sensing (occupancy / listen-before-talk)

`--role sense` integrates received power over a `--sense-window` (ms) and prints
`[SENSE] busy=.. power_db=..` per window; `--sense-count 0` streams forever. The
busy threshold is gain-dependent — calibrate it to your idle floor (Python does this
automatically).

```bash
# one 5-window measurement (raw CLI). Busy if avg power > --sense-threshold-db
./sdr_system --role sense --rx-args serial=30CD3F7 --rx-freq 915e6 --rx-rate 1.6e6 \
  --rx-gain 30 --sense-window 10 --sense-count 5 --sense-threshold-db -6 --viz false

# persistent feed (stream until Ctrl-C) — for a live occupancy monitor
./sdr_system --role sense --rx-args serial=30CD3F7 --rx-gain 30 --sense-count 0 --viz false
```

Python wrapper (recommended — auto-calibrates the threshold, importable helpers):

```bash
python3 channel_sense.py --calibrate                     # measure the idle floor
python3 channel_sense.py --count 10                      # calibrate, then sense 10 windows
python3 channel_sense.py --p 0.5 --count 8               # p-persistent sense->decide loop
```

```python
from channel_sense import SenseStream           # persistent feed, one radio init
with SenseStream(rx_args="serial=30CD3F7") as s:
    thr = s.calibrate()
    go, r = s.should_transmit(p=0.5, threshold_db=thr)   # idle -> TX w.p. p; busy -> defer
```

Validated: idle ≈ −12 dB → not busy; an active carrier ≈ −3 dB → busy. See
`SYSTEM_REFERENCE.md` §4.1.

## Link BER diagnostic (`--ber-expected`)

Measure the *real* per-burst bit-error-rate. Give the sink the KNOWN transmitted
payload and it prints, for **every** decoded burst (CRC pass or fail), the pre-FEC
(raw channel) and post-FEC (payload) BER vs ground truth — so you can tell whether
a CRC-failed frame is nearly right (a few residual bits) or garbage (FEC overwhelmed).

```bash
# RX / sink: known payload as ground truth
./sdr_system --role sink_arq --rx-args serial=30CD3F7 --tx-args serial=30CD3F7 \
  --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 40 --scheme DQPSK --fec true \
  --ack-transport tcp --ack-port 5599 --det-mult 3 --bytes-length 125 --timer_interval 20 \
  --serve-forever --ber-expected known_payload.bin --viz false
#   -> [BER] pre-FEC=1.20% ... post-FEC payload=0.30% ... CRC=FAIL   (marginal: nearly right)
#   -> [BER] pre-FEC=24.0% ... post-FEC payload=42.3% ... CRC=FAIL   (broken: garbage, FEC gave up)

# TX / source: send that same known payload
./sdr_system --role source_arq --tx-args serial=30CD424 --rx-args serial=30CD424 ... \
  --scheme DQPSK --bytes-length 125 --payload-file known_payload.bin --max-attempts 1
```

Python (single-box probe — runs its own warm AP + fires N known packets, reports
min/median/max BER):

```bash
python3 marl_phy.py ber --attempts 20 --scheme DQPSK
```
```python
from marl_phy import ber_probe
rows = ber_probe(n=20, scheme="DQPSK")     # [{pre_fec, post_fec, crc}, ...]
```

Note: **post-FEC > pre-FEC** means the raw BER exceeded the rate-½ code's ~11%
threshold — Viterbi then *amplifies* errors (catastrophic FEC failure) → garbage.
See `SYSTEM_REFERENCE.md` §8.1.

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

---

# Other USRP models — N210 / X310 / X410

The **DSP is device-independent**: every `--scheme / --waveform / --fec / --ack-* / --preamble /
--det-mult / eq / timing / phase` flag is exactly as in the B210 sections above. Only the
**hardware addressing** changes — device args, subdev, antenna, and (importantly) the **gain
range**. The blocks below are **templates adapted from the B210 commands and NOT yet tested on
those models** — confirm the exact subdev / antenna / gain range of your unit first with:
```bash
uhd_usrp_probe --args addr=<ip>      # prints subdev names, antenna ports, gain ranges
```

## What changes per model

| Model | Transport · `--*-args` | `--*-subdev` | `--*-ant` | Gain range* | Reference clock |
|---|---|---|---|---|---|
| **B210** (this rig) | USB · `serial=30CD424` | `A:A` | `TX/RX`, `RX2` | 0–89.8 dB | internal TCXO (no ext ref) |
| **N210** | 1 GbE · `addr=192.168.10.2` | `A:0` | `TX/RX`, `RX2` | ~0–31.5 dB (SBX/WBX/UBX) | `REF IN` 10 MHz + PPS, or MIMO cable |
| **X310** | 10 GbE / PCIe · `addr=192.168.40.2` | `A:0` (or `B:0`; 2 slots) | `TX/RX`, `RX2` | ~0–31.5 dB (UBX/SBX) | `REF IN` + PPS, GPSDO option |
| **X410** | 100 GbE (QSFP28) · `addr=192.168.10.2` | integrated ZBX — use `--rx-channel/--tx-channel` | `TX/RX0`, `RX1` (per ch.) | ~0–60 dB (ZBX) | `REF IN` + PPS, internal, White Rabbit |

\*Gain is **daughterboard-specific**. The classic SBX/WBX/UBX cards top out at **31.5 dB**, so the
B210's tx-gain 78–86 is far out of range there — start mid-to-high and tune by the same EVM /
`Peak=` method. For 915 MHz use a card that covers it (SBX 0.4–4.4 GHz, UBX 10 MHz–6 GHz, X410 ZBX
1 MHz–8 GHz; note **CBX is 1.2–6 GHz and does *not* cover 915 MHz**).

## Sharing a clock is easier on these → dense QAM
An external clock is **not** needed for basic operation and is **not** a per-model requirement —
every USRP runs standalone on its internal oscillator. It is needed only to make **two separate
radios frequency-coherent**: two independent oscillators drift apart (that *is* the CFO), and one
10 MHz reference fed to *both* radios locks them together (CFO → ~0), which is what dense QAM needs.
That's the B210 §13 limitation — not that the B210 can't (it also supports `--ref external`, via
small onboard connectors), but that we have **no 10 MHz source**.

The N210/X310/X410 just make this convenient: dedicated **front-panel SMA `REF IN` + `PPS IN`**,
and the X310/X410 offer an **internal GPSDO** (a self-contained 10 MHz source). Feed one 10 MHz
reference into both units' `REF IN`, add **`--ref external`** to both commands, and
**16-QAM / 64-QAM / 256-QAM decode**. (`--ref external` is a standard UHD option on all models,
including the B210; you always supply the 10 MHz source yourself unless a GPSDO is installed.)

## Template — QPSK single-carrier
Swap `--scheme` (BPSK / 8-PSK / DBPSK / DQPSK / …) and add the OFDM flags exactly as in the B210
sections. Each unit has its own IP: the sink uses radio A's address for both `--rx-args` and
`--tx-args`, the source uses radio B's. Drop `--ref external` if you don't have a shared clock
(then you're limited to QPSK/8-PSK, same as the B210).

**N210** (two units, shared 10 MHz):
```bash
# RX / sink   (radio A = 192.168.10.2)
./sdr_system --role sink_arq --rx-args addr=192.168.10.2 --tx-args addr=192.168.10.2 \
  --rx-subdev A:0 --rx-ant RX2 --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 20 --ref external \
  --scheme QPSK --fec true --ack-transport tcp --ack-port 5599 --det-mult 3
# TX / source (radio B = 192.168.20.2)
./sdr_system --role source_arq --tx-args addr=192.168.20.2 --rx-args addr=192.168.20.2 \
  --tx-subdev A:0 --tx-ant TX/RX --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 28 --ref external \
  --scheme QPSK --fec true --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599 --timeout 3000
```

**X310** (two units, shared 10 MHz — subdev `A:0`, 10 GbE addresses):
```bash
# RX / sink   (radio A = 192.168.40.2)
./sdr_system --role sink_arq --rx-args addr=192.168.40.2 --tx-args addr=192.168.40.2 \
  --rx-subdev A:0 --rx-ant RX2 --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 20 --ref external \
  --scheme QPSK --fec true --ack-transport tcp --ack-port 5599 --det-mult 3
# TX / source (radio B = 192.168.40.3)
./sdr_system --role source_arq --tx-args addr=192.168.40.3 --rx-args addr=192.168.40.3 \
  --tx-subdev A:0 --tx-ant TX/RX --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 25 --ref external \
  --scheme QPSK --fec true --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599 --timeout 3000
```

**X410** (RFSoC / ZBX — channel-based; confirm antenna names + subdev with `uhd_usrp_probe`):
```bash
# RX / sink   (radio A)
./sdr_system --role sink_arq --rx-args addr=192.168.10.2 --tx-args addr=192.168.10.2 \
  --rx-subdev A:0 --rx-channel 0 --rx-ant RX1 --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 30 --ref external \
  --scheme QPSK --fec true --ack-transport tcp --ack-port 5599 --det-mult 3
# TX / source (radio B)
./sdr_system --role source_arq --tx-args addr=192.168.10.3 --rx-args addr=192.168.10.3 \
  --tx-subdev A:0 --tx-channel 0 --tx-ant TX/RX0 --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 30 --ref external \
  --scheme QPSK --fec true --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599 --timeout 3000
```

## Sample-rate note (per master clock)
The pipeline needs `tx-rate == rx-rate` with an integer samples/symbol (default `0.8e6 sym × U/D 2 =
1.6e6`). 1.6 MHz divides the **X310** master clock (200 MHz) exactly, but **N210** (100 MHz) and
**X410** (245.76 / 250 MHz) will snap 1.6 MHz to the nearest valid rate — UHD prints the actual
rate on start-up. If it snaps, set `--symbol_rate` so that `rate = symbol_rate × U/D` holds at the
achieved rate (keeping integer sps), and use the same values on both ends.
