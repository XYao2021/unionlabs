# Running on two B210s (real hardware)

The `sim/` TCP demo was a stand-in for the radio. On real hardware you use the
full pipeline binary `sdr_system` (built by CMake), which has the RF front-end
the sim omitted — pulse shaping on TX; energy detection, AGC, matched filter and
timing recovery on RX — plus all the fixes (sync-before-offset reorder, the
phase-tracker fix, and the extra modulations).

A new `--role` flag lets one process drive one radio:

- `--role tx` : open only the TX B210 and run the TX pipeline (transmit only).
- `--role rx` : open only the RX B210 and run the RX pipeline (receive + decode).
- `--role both` : original single-box full-duplex/loopback (unchanged).

`tx`/`rx` run a simple **one-way** link (no stop-and-wait ACKs): the TX cycles
through the message `--tx-reps` times; the RX decodes bursts as they arrive and
reassembles. Subdev defaults to `A:A` (**RF A**) on both.

## Build

```bash
cd unionlabs
mkdir build && cd build
cmake ..            # add -DBOOST_ROOT=/your/boost/prefix if needed
make -j             # -> ./sdr_system
```

## Wiring (do this first)

Best: an **SMA cable with a 30–40 dB attenuator** from the TX **TX/RX** port of
the transmit B210 to the **RX2** port of the receive B210. A direct cable with
no attenuator can overload the RX; open-air antennas may be subject to local RF
regulations. Both radios use their own internal reference, so there will be a
real carrier frequency offset between them — that is exactly what the CFO stage
corrects.

## Run — two terminals

The RX front-end now runs at an **integer number of samples/symbol**. With the
default `--symbol_rate 0.8e6` and `--U 2 --D 1`, the wire rate is
`tx_rate = rx_rate = 2 * symbol_rate = 1.6e6` (2 samples/symbol). **`tx-rate` and
`rx-rate` must be equal and must equal `symbol_rate * U/D`** — the startup
`[CONSISTENCY]` check enforces this and prints the exact values to use.

Terminal 1 — RX B210 (serial 30CD3F7), RF A, start this first:
```bash
./sdr_system --role rx \
    --rx-args "serial=30CD3F7" --rx-subdev "A:A" --rx-ant "RX2" \
    --rx-freq 2.45e9 --rx-rate 1.6e6 --rx-gain 40 \
    --scheme QPSK --sync_threshold 15
```

Terminal 2 — TX B210 (serial 30CD424), RF A:
```bash
./sdr_system --role tx \
    --tx-args "serial=30CD424" --tx-subdev "A:A" --tx-ant "TX/RX" \
    --tx-freq 2.45e9 --tx-rate 1.6e6 --tx-gain 60 \
    --scheme QPSK --tx-reps 30
```

> Want more oversampling? Use `--U 4 --D 1 --tx-rate 3.2e6 --rx-rate 3.2e6`
> (4 samples/symbol). Any integer `rx_rate/symbol_rate` works; a **non-integer**
> ratio is now rejected at startup.

Change the modulation on **both** ends with `--scheme` (must match):
`BPSK QPSK 8-PSK 16-QAM 32-QAM 64-QAM 128-QAM 256-QAM 16APSK 32APSK`
and the differential PSK schemes `DBPSK DQPSK 8-DPSK`.

The RX prints, per burst: the ACQ correlation peak, the CFO and phase estimates,
the demodulated bits, and the decoded chunk; at the end it prints the reassembled
message. The RX **auto-terminates** `--rx-idle-timeout` seconds (default 8) after
the last burst (i.e. once the TX has finished) and prints the message; `0` keeps
the old run-until-Ctrl-C behaviour.

## Reliable delivery (error detection)

Every packet carries a **CRC-16**, so a chunk corrupted by bit errors is detected
and rejected instead of producing a wrong message. Two modes use it:

### 1. CRC-verified collection (one cable, no ACK) — the default `--role rx`

The RX accepts a chunk only when its CRC checks out, and — because the TX repeats
every chunk (`--tx-reps`) — simply waits for a clean copy of each. It auto-stops
the moment all chunks are verified. This delivers an **exact** message over the
single data cable with no reverse channel. Just run the `--role tx`/`--role rx`
commands above (hardware-verified: the full message decoded error-free).

### 2. True stop-and-wait ARQ (ACK feedback)

The receiver ACKs each CRC-verified chunk and the transmitter retransmits until
ACKed, advancing chunk-by-chunk and stopping when all are confirmed. The **DATA**
always goes over RF (RF A); the **ACK** transport is chosen with
**`--ack-transport`** (both hardware-verified, message delivered error-free, 0
unacked chunks). Start the **SINK first** in both cases.

#### 2a. TCP/IP ACK — default (one cable)

Best when both radios are on one host: data over the RF-A cable, ACK over a
localhost socket. No reverse RF cable, no full-duplex, no ACK-RX warm-up. The
sink is the ACK server; the source connects to `--ack-host:--ack-port`
(default `127.0.0.1:5599`).

Terminal 1 — **SINK** (serial 30CD3F7):
```bash
./sdr_system --role sink_arq \
    --rx-args "serial=30CD3F7" --rx-subdev "A:A" --rx-ant "RX2" --rx-freq 2.45e9 \
    --tx-rate 1.6e6 --rx-rate 1.6e6 --rx-gain 40 --scheme QPSK \
    --ack-transport tcp --ack-port 5599
```

Terminal 2 — **SOURCE** (serial 30CD424):
```bash
./sdr_system --role source_arq \
    --tx-args "serial=30CD424" --tx-subdev "A:A" --tx-ant "TX/RX" --tx-freq 2.45e9 \
    --tx-rate 1.6e6 --rx-rate 1.6e6 --tx-gain 60 --scheme QPSK --timeout 2000 \
    --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599
```
(`--ack-transport tcp` is the default; `--ack-host`/`--ack-port` let you point at
another machine or port. Use the sink's IP as `--ack-host` if on two hosts.)

#### 2b. RF ACK — `--ack-transport rf` (two cables)

ACK travels over a second RF path (RF B). Needs the reverse cable and each box
full-duplex on one B210: data on RF A, ACK on RF B, on **different frequencies**
(so a box's own data-TX doesn't blind its ACK-RX). Wiring: data
`30CD424 RF A TX/RX → 30CD3F7 RF A RX2`; ACK `30CD3F7 RF B TX/RX → 30CD424 RF B RX2`.

Terminal 1 — **SINK** (data RX + ACK TX), serial 30CD3F7, start first:
```bash
./sdr_system --role sink_arq --ack-transport rf \
    --tx-args "serial=30CD3F7" --rx-args "serial=30CD3F7" \
    --rx-subdev "A:A" --rx-ant "RX2"   --rx-freq 2.45e9 \
    --tx-subdev "A:B" --tx-ant "TX/RX" --tx-freq 2.40e9 \
    --tx-rate 1.6e6 --rx-rate 1.6e6 --tx-gain 60 --rx-gain 40 --scheme QPSK
```

Terminal 2 — **SOURCE** (data TX + ACK RX), serial 30CD424:
```bash
./sdr_system --role source_arq --ack-transport rf \
    --tx-args "serial=30CD424" --rx-args "serial=30CD424" \
    --tx-subdev "A:A" --tx-ant "TX/RX" --tx-freq 2.45e9 \
    --rx-subdev "A:B" --rx-ant "RX2"   --rx-freq 2.40e9 \
    --tx-rate 1.6e6 --rx-rate 1.6e6 --tx-gain 60 --rx-gain 40 \
    --scheme QPSK --timeout 2000
```

Both processes exit on their own (source when all chunks ACKed, sink when all
received). With the RF ACK, the first chunks show extra retransmits while the
ACK-RX front-end calibrates; the TCP ACK avoids that. (The RF ACK is currently a
full-size frame so it reuses the data sizing.)

## Things you will almost certainly need to tune

- **`--sync_threshold`** (alias **`--sync-threshold`**) — default 15. After AGC
  normalises the RX to ~unit RMS the correlation peak is ~the preamble length
  (31 for `m=5`). Watch the `[ACQ]   Peak correlation` lines: set it below the
  true peak but above the sidelobes. Too low → locks onto noise/guard; too high →
  never detects. The startup prints `[MAIN] ACQ sync_threshold: …` so you can
  confirm the value took.
- **`--det-mult`** (alias for `--IIR_threshold_multiplier`) — the **auto detector
  threshold** = measured `noise_floor × det-mult` (default 5). This is the first
  gate: it decides which bursts even reach the ACQ. **Over-the-air, raise it**
  (try 10–30) so the detector fires only on real bursts, not on ambient RF —
  otherwise the pipeline is flooded with noise "bursts". Too high and it misses
  weak signals. Startup prints `[MAIN] Energy detector: AUTO threshold = …`.
  (Use `--det-adaptive false --det-threshold <v>` for a fixed threshold instead.)
- **Antenna vs cable** — with real antennas the signal is weaker and the air is
  noisier, so you will typically **raise `--det-mult`** (reject ambient RF at the
  detector) and re-tune **`--sync_threshold`** and **`--rx-gain`** together. Bring
  the radios close, watch `[AFTER ENERGY DETECTION] … RMS` and `[ACQ]   Peak
  correlation`, and set each threshold below the real values but above the noise.
- **`--rx-gain` / `--tx-gain`** — watch the `[FEEDFORWARD AGC] ... RMS` line. If
  the RX RMS is tiny, raise gain (or reduce attenuation); if it's clipping,
  lower it. Cabled starting points: TX 50–70 dB, RX 30–45 dB. Over antennas you
  will usually need more RX gain (and less TX, to stay legal / avoid overload).
- **`--tx-freq` == `--rx-freq`** — must match. Keep it in an ISM band (e.g.
  2.45e9) if radiating.
- **Sample vs symbol rate** — defaults are `--tx-rate/--rx-rate 1.6e6` with an
  0.8e6 symbol rate and `U/D = 2/1`, i.e. an **integer 2 samples/symbol** on the
  wire. The RX matched filter is single-rate at that oversampling and the ACQ
  does symbol timing, so `rx_rate/symbol_rate` **must be a whole number** and
  `tx_rate == rx_rate == symbol_rate*U/D`. The `[CONSISTENCY]` block checks all
  three and aborts with the exact values to use on a mismatch.
- **Higher-order QAM / channel equalizer.** For 8-PSK and QAM through a
  multipath (over-the-air) channel, use a **complex Zadoff-Chu preamble** and the
  **equalizer**: add `--preamble zadoff --eq_type LMS` on BOTH ends. A real BPSK
  m-sequence preamble under-trains a complex equalizer (it can't excite the Q
  axis); the ZC preamble lets the least-squares equalizer recover the true
  channel inverse — symbol-level BER is 0 for all QAM through multipath, and 8-PSK
  over-the-air jumps from marginal to solid. The equalizer is LS-trained on the
  preamble and **frozen** by default (exact); add `--eq_dd true` to also
  decision-directed-track a slowly time-varying channel. `--eq_taps` (default 11)
  sets the span.
- **Dense QAM (16-QAM and up) over antennas is PAPR/SNR-limited**: 16-QAM+RRC has
  a high peak-to-average ratio, so the RX ADC clips (watch `[AFTER ENERGY
  DETECTION] … Peak=` — keep it under ~0.7) before you can raise gain enough for
  the SNR it needs. Bring the radios close, or use a cabled/attenuated link, for
  16-QAM+. This is a link limit, not the equalizer. Start with QPSK/8-PSK.

## Hardware result (verified on two B210s)

Verified end-to-end on the two radios (30CD424 TX/RX  →  30CD3F7 RX2, SMA cable,
QPSK @ 2.45 GHz, 1.6 Msps). With the current defaults the full message decodes
and is readable. What it took, beyond the front-end rebuild (see `CHANGES.md`):

- **Energy detector**: default `--alpha` is now **0.95** (was 0.02). The IIR is
  `filtered = (1-alpha)*inst + alpha*prev`, so 0.02 barely smoothed and the
  detector fired thousands of times on the noise floor and chopped bursts apart.
  0.95 gives one clean capture per burst (31 captures for 31 transmitted bursts).
- **Equaliser**: default `--eq_type` is now **None**. The LMS loop **diverges** on
  the real signal (decision-directed error grows and destroys the symbols) — with
  LMS the output was garbage, with None the message decodes. Fix LMS before using
  it. On a clean cabled link no equaliser is needed.
- **Reassembly** now rejects bogus headers (`tot>64` or `idx>=tot`) so false-alarm
  bursts don't pollute the decoded message.

Remaining, quality-not-connectivity: a few residual bit errors survive at moderate
SNR (no FEC yet, and the RX keeps the *last* decoded copy of each chunk rather than
majority-voting across the `--tx-reps` repetitions). Improve with more link margin
(gain tuning), wiring in `fec.hpp`, or best-of-N chunk selection.

## FPGA compatibility: when a network radio refuses to enumerate

An X310/N210 boots from its OWN flash, so its FPGA image can mismatch the
host's UHD — typically after another host with a newer UHD flashed it. The
symptom is explicit: `Expected FPGA compatibility number 38, but got 39`.
(A B210 can never hit this: it has no persistent image — the host loads its
firmware/FPGA at every USB enumeration from `/usr/share/uhd/images`.)

The platform image ships the matching flash images for all three radios, so
any session can repair this offline:

    uhd_image_loader --args="type=x300,addr=192.168.40.2"     # X310
    # N210: type=usrp2. Wait for "finished" — never interrupt a flash.

Then **power-cycle the radio** (full off/on — the FPGA loads from flash only
at power-up) and re-probe:

    uhd_usrp_probe --args addr=192.168.40.2

Coordination: after downgrading to this platform's compat (UHD 4.1 = 38), a
host running a newer UHD against the same radio hits the mirror-image error.
One radio, one UHD version — agree before flashing shared hardware.
