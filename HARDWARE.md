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
cd Hardware_update
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
message.

## Things you will almost certainly need to tune

- **`--sync_threshold`** — default is now 15 (was 1.0, far too low). After AGC
  normalises the RX to ~unit RMS the correlation peak is ~the preamble length
  (31 for `m=5`). Watch the `[ACQ]   Peak correlation` lines: set it below the
  true peak but above the sidelobes. Too low → locks onto noise/guard; too high →
  never detects.
- **`--rx-gain` / `--tx-gain`** — watch the `[FEEDFORWARD AGC] ... RMS` line. If
  the RX RMS is tiny, raise gain (or reduce attenuation); if it's clipping,
  lower it. Reasonable cabled starting points: TX 50–70 dB, RX 30–45 dB.
- **`--tx-freq` == `--rx-freq`** — must match. Keep it in an ISM band (e.g.
  2.45e9) if radiating.
- **Sample vs symbol rate** — defaults are `--tx-rate/--rx-rate 1.6e6` with an
  0.8e6 symbol rate and `U/D = 2/1`, i.e. an **integer 2 samples/symbol** on the
  wire. The RX matched filter is single-rate at that oversampling and the ACQ
  does symbol timing, so `rx_rate/symbol_rate` **must be a whole number** and
  `tx_rate == rx_rate == symbol_rate*U/D`. The `[CONSISTENCY]` block checks all
  three and aborts with the exact values to use on a mismatch.
- **Dense QAM** (128/256) needs a clean, well-tuned link (good SNR, gains, and
  threshold) before it will decode; start with QPSK to bring the link up, then
  step up.

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
