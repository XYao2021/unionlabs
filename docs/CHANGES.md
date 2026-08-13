# Changes

Two things were requested: (1) wire in the extra modulations that were already
half-present in the code, and (2) require the carrier **frequency** and **phase**
offset correction to run **after** time synchronisation. Both are done. A third
group of edits fixes bugs that were preventing the receiver from working.

Everything below was verified with symbol-level test harnesses (the design needs
a USRP + UHD to run for real, which isn't possible to build/run here — so the
full pipeline was **not** hardware-tested; please rebuild and test on your rig).

---

## 1. Modulations (now fully wired in and verified by zero-noise loopback)

Newly usable end-to-end:

- **16-APSK** (`--scheme 16APSK`) and **32-APSK** (`--scheme 32APSK`) — added to
  `ModulationType`, given real ring constellations in `Modulator`, and mapped in
  `string_to_mod_type()`. They flow through the normal modulate/demodulate path.
- **π/4-QPSK** (`--scheme PI4-QPSK`) — wired into `modulation_thread` /
  `demodulation_thread` as a special differential path using `PI4QPSKModulator`.
  Two mapping bugs in `PI4QPSKModulator` were fixed (the encoder used plain-binary
  dibit indexing while the decoder used a Gray table — they disagreed, so it never
  round-tripped). Now Gray-consistent.
- **128-QAM** (`--scheme 128-QAM`) — the old builder produced 96 real points then
  `resize(128)`, padding with 32 duplicate `(0,0)` points, so many symbols were
  indistinguishable. Replaced with a proper 128-point cross constellation.

Files: `include/modulator.hpp`, `src/modulator.cpp`, `include/modulator_extended.hpp`.

`modulation_thread` / `demodulation_thread` now dispatch via `string_to_mod_type()`
instead of a truncated if-else, so every scheme in the enum is reachable.

**Differential QAM (DQAM16…DQAM256)** is intentionally *rejected* at the thread
dispatch with a clear message. The existing differential coder forms symbols by
cumulative complex multiplication, which only preserves information for
constant-envelope (PSK) constellations — QAM amplitude levels are destroyed. The
enum entries still build a valid constellation (so nothing crashes) but selecting
them for TX/RX is refused rather than emitting silently-wrong bits.

## 2. Frequency & phase offset AFTER time synchronisation

Old RX order (broken):
```
timing_recovery → CFO → TimeSync → phase_offset → channel_eq → demod
```
New RX order:
```
timing_recovery → TimeSync(ACQ) → CFO → phase_offset → channel_eq(strips preamble) → demod
```

Why the old order could not work:
- CFO used **pilot-aided** estimation, which requires the block to already start
  at the preamble — impossible *before* time sync. It was correlating the local
  preamble against unaligned samples → garbage frequency estimate.
- `TimeSync` **stripped the preamble** (output data only), yet both the phase
  stage (PREAMBLE method) and the equalizer (train-on-preamble) require the
  preamble to be at the front of the block. Both were operating on data symbols
  as if they were the preamble.

What changed:
- `ACQSynchronizer` gained `ExtractAlignedPacket()` and an `AlignedStats` result
  field that returns the aligned burst **`[preamble | data]`** (preamble kept).
  `TimeSync_thread` now pushes that.
- The `StartOffset` bug in `ExtractDecisionStats` was fixed: the correlation peak
  lands at the *full-preamble* start (verified by simulating the exact packet
  layout), so the data offset is `PreambleLength`, not `PreambleLength + GuardLen`
  (the old value skipped the first 10 data symbols).
- `physical_layer.hpp` reorders the stages so CFO and phase run on the aligned
  burst (both are data-aided). CFO now runs at 1 sample/symbol (`sps=1`,
  `rate=symbol_rate`).
- The preamble is stripped just before demodulation: `channel_eq_thread` trains on
  it then outputs data only; the no-equalizer passthrough strips it too.

Files: `include/synchronization.hpp`, `src/synchronization.cpp`,
`include/channel_estimation.hpp`, `include/physical_layer.hpp`.

Validation: a synthetic receive chain with an injected CFO (0.03 rad/symbol) +
static phase (0.6 rad) recovered every bit (BER 0.0000 at zero noise; CFO estimate
matched the injected value exactly) versus BER ≈ 0.52 with no correction.

## 3. Phase-tracker fixes (surfaced by sweeping the RX chain over all modulations)

Running the full software receive chain across every modulation exposed two bugs
in the decision-directed phase tracker (`PhaseTracker` / `PhaseOffsetCorrector`
in `include/phase_offset.hpp`). With them, only QPSK survived the phase stage;
every higher-order absolute scheme (8-PSK, 16/32/64-QAM, 16/32-APSK) came out at
BER ≈ 0.5 — so the newly added modulations would have been broken end-to-end even
though they modulate/demodulate correctly in isolation. Both are fixed.

- **Double derotation / wrong seed.** `correct()` bulk-derotates the block by
  `-phi_est`, then seeded the tracker with `reset(-phi_est)`. The tracker applies
  `r·e^{-j·phi_}`, so with `phi_ = -phi_est` it re-rotated the block by `+phi_est`
  on entry, reintroducing the whole offset. QPSK tolerated the ~0.6 rad error
  inside its ±45° decision margin and re-locked; denser constellations decided
  wrong from the first symbol and diverged. Fixed: the block is already
  derotated, so the loop now starts from zero.

- **Preamble contaminating the loop.** The tracker processed the whole
  `[preamble | data]` block, but the preamble is BPSK (points at 0°/180°) and
  does not lie on the data constellation. Deciding a 0° preamble symbol against
  (say) a QPSK grid makes it equidistant between the 45° and 315° points → a
  spurious ±(180/M)° error on every preamble symbol → the loop drifts before the
  data starts (QPSK slipped to −90°, 16-APSK wandered ~20°). This is why it
  failed only at *zero* noise and "passed" with a little noise (the noise broke
  the exact decision ties). Fixed: the tracker now runs over the data symbols
  only; the (bulk-rotated) preamble is left untouched and is stripped before
  demod anyway.

After both fixes, all absolute schemes recover with BER 0.0000 through the full
CFO + phase chain with the tracker enabled (exactly matching tracker-disabled),
at zero noise and with light AWGN.

## Hardware-free demos

`tests/run_demo.sh` builds and runs three demos that need only g++ (no UHD / no
USRP), reproducing all of the above:
- Demo 1 — modulation round-trip for every scheme.
- Demo 2 — frequency & phase offset after time sync (QPSK), BER with/without.
- Demo 3 — the full receive chain (TimeSync → CFO → phase → demod) swept across
  QPSK, 8-PSK, 16/32/64-QAM, 16/32-APSK and the differential schemes.

## Notes / things still worth checking on hardware

- **ACQ threshold + guard sidelobe.** `PerformACQOptimized` exits the search at the
  *first* correlation above `sync_threshold`. Because the 10-symbol guard is a copy
  of the preamble's tail, it produces a strong correlation sidelobe (~27/31). If
  `sync_threshold` is set too low the search can lock onto the guard (or noise)
  instead of the true peak. Tune `--sync_threshold` so only the true peak crosses,
  or use the global-max `SamplesACQPerformance` search. (The default of `1.0` is
  almost certainly too low once AGC scaling is considered.)
- **Differential schemes + preamble strip.** Differential demod (`differential_decode`,
  and π/4) needs one reference symbol; stripping the whole preamble loses the first
  data symbol's reference (one symbol at the head). Pre-existing behaviour — run
  differential/π-4 schemes with `--eq_type None`; they're inherently robust to the
  static phase offset and don't rely on the absolute preamble-ML phase estimate.
- **Sample-rate bookkeeping** around the match filter / Gardner TED (RX match filter
  is called with `U, D = U, 1`; Gardner assumes 2 sps) looks inconsistent with the
  `sps_sync = 5` default and is worth double-checking against your actual rates.

## Two-terminal TX/RX demo (`sim/`)

`sim/` adds a hardware-free two-terminal link: `tx_app` and `rx_app` talk over a
localhost TCP socket, the RX injects AWGN + a carrier frequency/phase offset,
then runs the real sync → CFO → phase → demod → decode chain and logs each
stage (which correlation sample is the peak, the estimated vs injected CFO and
phase, the recovered bit pattern, and the decoded message). Build with
`bash sim/build.sh`; run `rx_app` in one terminal and `tx_app` in another.
Scheme is chosen on the command line (`--scheme`). See `sim/README.md`.

## Real-hardware two-radio mode (`--role`)

`sdr_system` gained a `--role tx|rx|both` flag so one process can drive one B210.
Previously `start()` always opened both a TX and an RX device and ran both
pipelines (a single-box full-duplex/loopback design, required by the stop-and-wait
ARQ). `--role tx` opens only the TX device + TX pipeline; `--role rx` opens only
the RX device + RX pipeline; `--role both` is the original behaviour. tx/rx run a
one-way link (no ACKs): TX cycles the message `--tx-reps` times, RX decodes and
reassembles. Subdev defaults to `A:A` (RF A). See `HARDWARE.md` for the two-B210
run commands. (UHD path is compile-reviewed but hardware-untested here.)

## Bug fix: dangling scheme reference crashed TX/RX at thread startup

`launch_tx_pipeline()` and `launch_rx_pipeline()` declared `std::string scheme =
cfg_.scheme;` (and the TX path `bool add_p = ...`) as **locals**, then passed
`std::ref(scheme)` into `modulation_thread` / `demodulation_thread`, which run for
the life of the object. The moment the launch function returned, that reference
dangled; the thread then read reused stack memory, so `string_to_mod_type()`
received a corrupted string and aborted with
`std::invalid_argument: [string_to_mod_type] Unknown scheme: <garbage>`
(observed on the RX at startup). Fixed by binding the thread arguments to the
long-lived `cfg_` members (`std::ref(cfg_.scheme)`, `std::ref(cfg_.add_preamble)`)
— `cfg_` outlives the threads, which are joined in `stop()` before destruction.
This is undefined behaviour that can also silently mis-modulate on the TX side,
so it likely contributed to the original "not working" symptom.

## Detect/sync sizes now scale with the modulation (+ energy-detector burst fix)

The packet's data-symbol count depends on bits/symbol, so a fixed detect/sync
size only works for one scheme. Two coupled problems fixed:

- **Energy detector rejected/truncated real bursts.** `energy_packet_size`
  (default 3300) was both a minimum-accept length *and* a truncation target
  (`packet_size + 2*guard`). A real burst is only ~1065 samples for QPSK (fewer
  for higher-order schemes), so every burst was discarded as "too short" — the
  RX saw nothing. The detector now emits the *full* collected burst (no
  truncation); `packet_size` is only a minimum-length gate. Downstream sync
  locates the preamble and extracts exactly `recv_msg_len` data symbols, so any
  extra tail is harmless.
- **Sizes are auto-derived from `--scheme`.** `main.cpp` computes
  `data_syms = ceil((16-bit header + chunk_bytes*8) / bits_per_symbol)` and sets
  `recv_msg_len = data_syms` (ACQ extraction length) and
  `energy_packet_size = guard + preamble + data_syms` (min burst floor, in
  samples; the wire is >1 sample/symbol so real bursts always exceed it).
  Verified against the TX: QPSK → 508 data → 549-symbol packet, matching the
  observed `[MODULATION] size is 549`; 16-QAM → 254 → 295; 64-QAM → 170 → 211;
  etc. Explicit `--recv_msg_len` / `--energy_packet_size` still override.

Edge case: the final short chunk of a message (fewer data symbols than a full
chunk) can still be missed because the ACQ search range assumes the full data
length; use a chunk size that divides the message evenly, or pad, until that's
generalised.

## Fix: timing-recovery sample-rate mismatch (RX emitted ~4x too many symbols)

On hardware the RX produced garbage even after the detector fix. Cause: the RX
matched filter upsamples by U (=5) and the Gardner loop strobes once every `sps`
input samples (emitting input/sps symbols), so with the old fixed `sps_sync=5`
the two cancel — timing recovery emitted ONE symbol per received RF sample
(e.g. out=2106) instead of one per actual symbol (~549 for QPSK). The ACQ then
correlated a 1-sample/symbol preamble against ~4x oversampled data, so the peak
smeared (15/31, barely over threshold), it locked marginally, and demod/SNR were
garbage (SNR ~ -1 dB).

`sps_sync` is now auto-derived: at the matched-filter output there are
`(rx_rate/symbol_rate)*U` samples per symbol, so timing recovery uses that as its
`sps` and strobes once per symbol. Verified: rx_rate/symbol_rate = 1 -> sps 5,
= 4 -> sps 20, etc., all giving ~549 output symbols for a QPSK packet. For an
exact ratio use `rx_rate = k * symbol_rate`. Explicit `--sps_sync` overrides.

## RX matched-filter U corrected to cfg_.D (=4), decoupled from the TX RRC U (=5)

Per the intended design, the RX matched filter is designed at the RX oversampling
rate (rx_rate/symbol_rate = 4 samples/symbol), i.e. U=4, D=1 — NOT the TX RRC's
cfg_.U=5. The shipped code wrongly passed cfg_.U(5) to the RX match filter, so the
front-end produced the wrong samples/symbol. The match filter now uses cfg_.D(=4)
as its upsampling factor (TX RRC still cfg_.U/cfg_.D = 5/4), and the auto-derived
sps_sync = (rx_rate/symbol_rate) * cfg_.D and energy_packet_size (~0.75 *
(rx_rate/symbol_rate) * total_syms samples) follow from the same value. With
rx_rate = 4*symbol_rate this gives sps_sync = 16 and timing recovery emits ~one
sample per symbol. (For a true matched filter, keep cfg_.D = rx_rate/symbol_rate.)

## Startup rate-chain consistency check

`main.cpp` now walks samples/symbol through the pipeline
(receive -> energy -> AGC -> match filter -> timing -> sync) and verifies:
  1. tx_rate == symbol_rate * U/D   (pulse shaper output rate)
  2. rx_rate == tx_rate             (RX samples exactly what the TX sent)
  3. (rx_rate/symbol_rate) * cfg_.D  is a whole number (clean sps_sync)
It prints the full chain (`[CONSISTENCY] …`) with det_sps, match-filter Urx=cfg_.D,
after-MF sps, and the required sps_sync, then aborts with the exact `--tx-rate` /
`--rx-rate` to use on a mismatch. `--skip-rate-check` bypasses it. The energy
detector is kept BEFORE the match filter (on raw rx_rate samples), so its sizes
are in rx_rate samples.

## Front-end rebuild — the real reason the demodulated message was wrong

Every hardware-free test above validated the *core* chain (ACQ → CFO → phase →
demod) at 1 sample/symbol and got BER 0. But none of them exercised the RF
front-end — the **matched filter + timing recovery** — which only runs in the
full `sdr_system`. That front-end was broken three ways, so on real B210s the
symbols reaching the demodulator were garbage even though every earlier stage was
"verified". A faithful, thread-level reproduction (`tests/integration_test.cpp`,
`tests/frontend_repro.cpp`, `tests/pipeline2_verify.cpp` — g++ + fftw + volk, no
UHD) reproduced the failure and now confirms the fix at **BER 0.0000** across
CFO 0–8 kHz, phase 0–120°, and 2 & 4 samples/symbol.

1. **Matched filter was not matched (`filters.cpp`).** `match_filter_thread` was
   called with `U=cfg_.D=4`, so `rrc_pulse` designed a pulse with a **4-sample**
   symbol period while the polyphase upsampled the 1.25-sps wire to **5** sps —
   pulse period ≠ symbol spacing, so it was not a matched filter and the preamble
   correlation collapsed to ~3.5/31 (no detection, *even with perfect timing*).
   Fixed: the RX matched filter is now **single-rate** and designs its RRC at the
   integer RX oversampling `os = round(sample_rate/symbol_rate)`. It filters at
   the incoming rate and does not resample.

2. **Gardner TED stepped the NCO wrong (`timing_recovery.hpp`).** `omega = 1/sps`
   produced **one** strobe per symbol, but the error detector's odd/even logic
   needs **two** strobes per symbol (symbol-centre + half-symbol). The "mid"
   sample it fed the Gardner error was therefore a whole symbol away, so the loop
   tracked on garbage at every sps. Fixed to `omega = 2/sps` (emit only the
   symbol-centre strobes). *(The loop is no longer wired into the pipeline — see
   3 — but the fix stays for anyone who uses it.)*

3. **Timing is now done by the ACQ correlator, not a Gardner loop
   (`physical_layer.hpp`, `synchronization.cpp`).** The matched-filter output goes
   straight into `TimeSync_thread` at `samples_per_symbol = os`; the ACQ
   correlates the preamble across **every** sample offset (`SamplesACQPerformance`,
   not `PerformACQOptimized` — the latter strides by `os` and tests only one of
   the `os` sub-symbol phases, locking onto a zero-crossing → BER ≈ 0.5) and
   extracts the aligned `[preamble|data]` burst at the best sub-symbol instant,
   one sample/symbol. For packet-mode bursts the radios' ppm drift over one packet
   is negligible, so a per-burst correlation-chosen phase is simpler and more
   robust than a decision-directed loop.

4. **Integer samples/symbol by default (`main.cpp`, `physical_layer.hpp`).** New
   defaults `U/D = 2/1`, `symbol_rate 0.8e6`, `tx_rate = rx_rate = 1.6e6` → an
   exact 2 samples/symbol wire. The consistency check now requires
   `rx_rate/symbol_rate` to be a whole number (the old fractional 1.25-sps wire
   could not be matched-filtered cleanly). Analog `tx_bw/rx_bw` default raised to
   1.0 MHz (the 500 kHz default clipped the ~1 MHz occupied signal), and
   `sync_threshold` default raised 1.0 → 15 (the AGC-normalised preamble peak is
   ~31).

5. **Final short chunk padded (`main.cpp`).** Chunks are padded to the full
   `bytes_length` so every packet is full size; a short final chunk is no longer
   dropped by the detect/sync length gate, so the whole message reassembles.

---

## CFO estimator: least-squares phase-slope (lower variance) + cross-burst prior

**Motivation.** The single-carrier CFO stage was a *one-shot* estimate per burst
with no tracking loop, and the estimator itself was adjacent-symbol differential
(lag-1 only) → high variance (the ±1200 Hz jitter of §13). Differential schemes
(DBPSK/DQPSK, the two-host cold-LO range test) have **no** mid-burst CFO tracking,
so they live or die on that single estimate — making its variance the thing to fix
first, and one that needs no shared clock.

**What changed (`include/frequency_offset.hpp`, `include/physical_layer.hpp`):**
1. New `CFOEstimator::estimate_ls_slope()` — strips the modulation off every
   preamble symbol, unwraps, and does a **magnitude-weighted least-squares
   straight-line fit** of the phase progression. Uses all preamble symbols jointly
   (the data-aided ML / CRLB estimate) instead of lag-1 differencing → same
   unambiguous range, ~$L^2$ lower variance. See SYSTEM_REFERENCE §5.4-C.
2. New `CFOCorrector::Method::PILOT_LS`, now the **pipeline default** (was
   `PILOT_AIDED`). The old differential estimator is kept for comparison.
3. **Cross-burst EMA prior** (`CFOCorrector::set_prior_smoothing(α)`, CLI
   `--cfo_prior_alpha`, default `1.0`). `1.0` = pure per-burst, the correct/safe
   choice for a **cold per-fire LO** (two-host DQPSK). `<1.0` blends history and is
   for a **warm resident LO** only. Exposed through Python `_phy()` so
   `AccessPoint(cfo_prior_alpha=0.5)` reaches the warm sink; omitted by default so
   the cold-LO path is unaffected.

**Verification (no radio was connected):** full `sdr_system` builds clean against
UHD; `sdr.py`/`OPTIONS.md` regenerated with the new option; a hardware-free
Monte-Carlo (`scratchpad/cfo_ls_test.cpp`) shows the LS estimator ~halves CFO RMSE
at operating SNR (≈450 Hz vs ≈1050 Hz at 12 dB), matching/beating the §13 figure,
and the Python command-builder passes/omits the flag correctly. **Still to do on
the rig:** the end-to-end on-hardware Python check (per the project's
verify-Python-after-C++ rule) — the ~2× gain is *sim-validated, not yet
hardware-confirmed*. This widens the QPSK/8-PSK/DQPSK margin but does **not** lift
the dense-QAM wall (§13); a shared reference clock is still required for 16-QAM+.

**Residual-CFO FLL investigated and rejected (negative result).** A blind
M-th-power frequency-locked loop (`FreqTracker`, `include/phase_offset.hpp`) was
built and A/B-tested to add *mid-burst* CFO tracking. It does **not** help this
link and was **not** wired in: (1) on the differential path it *degrades* BER —
differential detection is already immune to a constant/slow residual CFO up to
±45°, so the FLL only adds M-th-power noise (sim: raw DQPSK = 0 BER up to 0.6
rad/sym residual even at 30 dB; +FLL is worse); (2) on the coherent path the
existing 2nd-order phase PLL (§5.5) already pulls CFO in to its decision limit, so
an FLL front-end adds nothing (sim: FLL+PLL ≈ PLL alone). With the LS estimator
above leaving ~milliradian residual, no tracker is needed. The `FreqTracker` class
is retained as a documented building block for a possible future large-CFO
coherent-acquisition experiment; sims live in `scratchpad/fll_test.cpp` and
`fll_coh_test.cpp`.

---

## Soft-decision FEC wired in (opt-in `--fec_soft`)

The convolutional code already had a **soft-decision Viterbi decoder** (`decode_soft`)
and a Max-Log LLR generator (`soft_demodulate_llr`), but nothing called them — the RX
always hard-decided. This wires the soft path end-to-end for **~2-3 dB of coding gain**
at the same SNR.

**How it flows:** `demodulation_thread` now optionally emits one **LLR per coded bit**
(`soft_demodulate_llr`) into a new `PHYSICAL_LAYER::rx_llr_fifo`, block-for-block
aligned with `rx_bits_fifo`. The ARQ `SINK` (`ACQ_stop_and_wait.hpp`) pops the matching
LLR block and calls the new `fec_soft_decode_block()` instead of the hard
`fec_decode_block()`.

**Scope / safety:**
- **Opt-in, default OFF** (`--fec_soft`, needs `--fec true`). The hard path is byte-for-byte
  unchanged when off — the demod passes `nullptr` for the LLR FIFO and the SINK never
  touches it.
- **Coherent single-carrier only.** Differential (DBPSK/DQPSK/8-DPSK), π/4-DQPSK and OFDM
  push an **empty** LLR vector (soft LLRs are undefined for differential detection; the
  OFDM demod has its own path), so the SINK transparently **falls back to hard** — no
  crash, just no soft gain there.
- RX-side only; the transmitter is unaffected (it still FEC-encodes the same way).

**Files:** `include/fec.hpp` (`fec_soft_decode_block`), `include/modulator.{hpp}` /
`src/modulator.cpp` (LLR emit), `include/physical_layer.hpp` (`rx_llr_fifo`, `fec_soft`
config, demod launch), `include/ACQ_stop_and_wait.hpp` (SINK soft path), `src/main.cpp`
(`--fec_soft`).

**Verification:** hardware-free Monte-Carlo (`scratchpad/soft_fec_test.cpp`) confirms the
primitives give the expected gain — e.g. QPSK CRC-OK 4%→78% @4 dB, 16-QAM 16%→95% @10 dB,
and the LLR sign convention is correct (negated LLRs decode to 0%). Full `sdr_system`
builds clean; the hard-path fake-channel regression is unchanged. **Still to verify on
the rig:** the live `rx_llr_fifo`↔SINK lockstep inside the ARQ loop can't be exercised
hardware-free — run a QPSK A/B (`--fec_soft` on the sink) at a *marginal* gain where hard
FEC drops chunks; soft should recover them.
