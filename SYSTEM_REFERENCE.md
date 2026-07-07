# USRP B210 SDR Link — System Reference

End-to-end digital communication system for two USRP B210 radios (UHD), written in C++.
This document lists **every feature, algorithm (with the math and the common alternatives),
and command-line option** exposed by `sdr_system`.

- Binary: `build/sdr_system`
- Help: `./sdr_system --help`
- Two operating styles: **one-way** (`--role tx` / `--role rx`, repeated sends, auto-terminating RX)
  and **ARQ** (`--role source_arq` / `--role sink_arq`, stop-and-wait with ACK).

---

## 1. Signal-chain overview

```
TX:  message ─► split into chunks ─► CRC-16 ─► [FEC encode] ─► bits→symbols (modulator)
        ├── single-carrier: RRC pulse-shape ─► preamble prepend ─► USRP TX
        └── OFDM:            QAM→IFFT+CP frame [SC-sync | chan-est | data] ─► USRP TX

RX:  USRP RX ─► energy detector (burst gate) ─► AGC ─► frame/symbol sync
        ├── single-carrier: matched filter ─► timing (Gardner) ─► [equalizer] ─► demod
        └── OFDM:            Schmidl-Cox sync + CFO ─► FFT ─► 1-tap eq ─► pilot CPE ─► demod
     ─► [FEC decode] ─► CRC-16 check ─► (ARQ: ACK if OK) ─► reassemble message
```

Default physical parameters: symbol rate `0.8 MHz`, `U/D = 2/1` → sample rate `1.6 MHz`
(integer **2 samples/symbol** on the wire), RRC roll-off `0.25`, 151 taps.

---

## 2. Modulation

`--scheme <NAME>` selects the constellation. Supported (bits/symbol in parentheses):

| Family | Schemes | bits/sym |
|---|---|---|
| PSK | `BPSK` (1), `QPSK` (2), `8-PSK` (3) | 1–3 |
| QAM | `16-QAM` (4), `32-QAM` (5), `64-QAM` (6), `128-QAM` (7), `256-QAM` (8) | 4–8 |
| Differential | `DBPSK` (1), `DQPSK` (2), `8-DPSK` (3), `PI4-QPSK` (2) | 1–3 |
| APSK | `16APSK` (4), `32APSK` (5) | 4–5 |

(`16QAM` and `16-QAM` spellings both accepted.)

**Math.** A modulator maps `k = log2(M)` bits to one of `M` complex points `s = I + jQ`:
- **M-PSK**: points on the unit circle, `s = exp(j·2π·m/M)`. All symbols equal-energy;
  demod by nearest angle. Robust but spectrally inefficient at high order (points crowd on the circle).
- **M-QAM**: square/cross grid of amplitudes, `s = (2i−√M+1) + j(2q−√M+1)`. Best power efficiency
  for a given rate on an AWGN channel, but needs accurate amplitude → sensitive to gain/EVM/clipping
  (this is why 16-QAM failed OTA at ~20% EVM while QPSK survived ~37%).
- **APSK**: points on a few concentric rings. Lower PAPR than QAM, favored on non-linear
  (satellite) amplifiers — a middle ground between PSK and QAM.
- **Differential (D*)**: information is in the **phase change** between consecutive symbols,
  `s_n = s_{n-1}·exp(jΔφ)`. No absolute phase reference needed → tolerates carrier phase
  offset / no PLL, at a ~3 dB SNR penalty. `PI4-QPSK` rotates the constellation by π/4 each
  symbol to avoid zero-crossings (lower envelope variation).

**Bits↔symbols detail.** Gray-like index mapping; `bits_to_index` zero-fills a partial final
group so schemes whose bits/symbol don't divide the frame (32-QAM, 128-QAM) still align — a
bug fixed early in this project.

**Common alternatives not implemented:** GMSK/CPM (constant-envelope, used in GSM/Bluetooth),
non-square 8-QAM, probabilistic constellation shaping.

---

## 3. Waveform: single-carrier vs OFDM

`--waveform sc` (default) or `--waveform ofdm`.

### 3.1 Single-carrier + RRC pulse shaping
Symbols are upsampled `U/D` and convolved with a **root-raised-cosine** filter
(`--filter_type rrc`, `--roll_off 0.25`, `--num_taps 151`).

**Math.** A raised-cosine (RC) spectrum has zero inter-symbol interference (ISI) at symbol
instants (Nyquist criterion). Splitting it as **RRC at TX and RRC at RX** makes the cascade
a full RC (matched-filter optimal in AWGN) *and* maximizes SNR. Roll-off β trades bandwidth
`(1+β)·Rsym` against time-domain sidelobe decay. Alternatives: `rc` (raised cosine, all
shaping at TX), `lp` (plain low-pass). Gaussian shaping (GMSK) is the common constant-envelope
alternative.

### 3.2 OFDM
Frame layout per burst: `[ Schmidl-Cox sync symbol | channel-estimation symbol | data symbols… ]`,
each symbol = `N`-point IFFT + cyclic prefix.

- `--ofdm-fft 64` — number of subcarriers `N` (FFT size).
- `--ofdm-cp 16` — cyclic-prefix length (must exceed the channel's delay spread).
- `--ofdm-tx-peak 0.5` — scales the frame so its peak ≈ this value (OFDM has high PAPR;
  keep the DAC out of clipping).

**Math.** OFDM splits the band into `N` narrow orthogonal subcarriers via the IDFT:
`x[n] = (1/N) Σ_k X[k]·exp(j2πkn/N)`. A cyclic prefix (copy of the tail prepended) turns the
channel's *linear* convolution into *circular* convolution, so after the FFT each subcarrier
sees a single complex gain `H[k]`: `Y[k] = H[k]·X[k] + noise`. Equalization is then **one
complex divide per subcarrier** (`X̂[k] = Y[k]/H[k]`) — no FIR equalizer needed even under
heavy multipath, provided the delay spread < CP. This is why OFDM is preferred for dense QAM
on frequency-selective channels. Cost: high peak-to-average power ratio (PAPR) and sensitivity
to carrier frequency offset (loss of subcarrier orthogonality → inter-carrier interference).

**Channel estimation & pilots.** One known **channel-estimation symbol** (known value on every
active subcarrier) gives `H[k] = Y_chest[k]/ref[k]`. **Scattered pilots** (every 8th active
subcarrier carries a known value) then track the residual **common phase error (CPE)** that
grows symbol-by-symbol from residual CFO.

**CPE estimation — the fix that made OFDM work OTA.** Per data symbol, estimate the phase φ
from the pilots and derotate:

```
φ = arg( Σ_pilots  Y[k]·conj(H[k])·conj(PILOT) )      (maximum-ratio combining)
```

The earlier version used the *equalized* pilot `Y[k]/H[k]`, which **blows up on a deep-fade
subcarrier** (tiny |H[k]|) — one garbage pilot then hijacked φ and smeared the whole symbol
into a blob (EVM ~67%, 0 CRC over the air; invisible in a mild simulation channel). Weighting
by channel power `|H[k]|²` via `Y·conj(H)` suppresses faded pilots instead of amplifying them
→ four clean clusters (EVM ~37%), message decodes. Disable with env `OFDM_NO_CPE=1` to compare.

**Common alternatives:** decision-feedback / per-subcarrier phase-locked tracking, MMSE channel
estimation (vs the least-squares one used here), pilot-aided residual-CFO (SFO) slope correction,
windowed-OFDM / filtered-OFDM / OTFS for spectral containment.

---

## 4. Energy detection (burst gating)

The RX runs continuously; the energy detector decides *when a burst is present* so downstream
sync isn't run on noise.

**Math.** An IIR (EMA) smoother tracks instantaneous power:
`filtered = (1−α)·inst + α·prev` (`--alpha 0.95`, larger α = more smoothing, ~20-sample time
constant). A burst is declared when `filtered > threshold`.

- **Adaptive threshold** (`--det-adaptive true`, default): `threshold = noise_floor × multiplier`,
  with `--det-mult 5` (raise to 10–30 over the air so ambient RF doesn't trigger it; too high
  misses weak bursts).
- **Continuous noise tracking** (`--det-continuous true`, default): the noise floor is re-estimated
  by an EMA during idle periods (`noise_floor ← (1−β)·noise_floor + β·inst`), so it follows
  drifting ambient noise. Alternative: one-shot startup calibration (`--det-continuous false`) —
  simpler but brittle if the environment changes.
- **Fixed threshold** (`--det-adaptive false` + `--det-threshold 1e-7`): absolute cutoff, only for
  a known clean cabled link.
- `--energy_packet_size` — how many samples to grab once a burst is detected (auto-sized per
  modulation).

**Common alternatives:** matched-filter / correlation detection (detect on the *known preamble*
rather than energy — more sensitive but needs the template), constant-false-alarm-rate (CFAR)
detectors, cyclostationary feature detection (works below the noise floor).

---

## 5. Synchronization

Three things must be recovered: **frame start** (where the packet begins), **symbol timing**
(the optimal sampling instant), and **carrier frequency/phase**.

### 5.1 Preamble
`--preamble m-sequence` (default) or `--preamble zadoff`; `--m 5` sets m-sequence order
(length `2^m − 1 = 31`).

- **m-sequence** — a maximal-length binary (BPSK) shift-register sequence with an almost-ideal
  two-valued autocorrelation (sharp peak, −1 floor). Real-valued.
- **Zadoff-Chu** — a complex **constant-envelope** CAZAC sequence (constant amplitude, zero
  autocorrelation). Better for driving a complex channel estimate / equalizer training and for
  clean correlation under CFO. Preferred when training the equalizer or for dense QAM
  (used with LS equalizer training in this project).

### 5.2 Frame sync (single-carrier) — ACQ correlation
`SamplesACQPerformance` cross-correlates the incoming samples against the known preamble; the
correlation **peak location** is the frame start and the peak **magnitude** gates false alarms.
`--sync_threshold 15` — set below the true preamble peak (~preamble length ≈ 31 after AGC) but
above the noise-correlation floor. Watch the `[ACQ] Peak correlation` log lines to tune.

**Math.** For received `r` and preamble `p`, the correlator computes
`R[d] = Σ r[d+n]·conj(p[n])`; a true preamble gives `|R|≈Σ|p|²` at the right offset, noise gives
much less → threshold in between. Joint frame + coarse symbol timing come from the peak.

### 5.3 Symbol-timing recovery — Gardner TED
A **Gardner timing-error detector** in a second-order loop tracks the sampling phase:
`--timing_loop_bw 0.015` (loop bandwidth BnT), `--timing_damping 0.707`, `--sps_sync 5`
(samples/symbol at matched-filter output).

**Math.** Gardner error `e = Re{ (y[k] − y[k−1])·conj(y[k−½]) }` uses the sample *between*
symbols; it is **decision- and carrier-phase-independent** (works before the constellation is
resolved), which is why it's popular. The error drives an NCO/interpolator via a
proportional-integral loop (bandwidth vs jitter trade-off set by `timing_loop_bw`).
Alternatives: Mueller & Müller (decision-directed, 1 sample/sym), early-late gate, Zero-crossing TED.

### 5.4 Carrier recovery
- **Phase PLL** (`--phase_loop_bw 0.02`, `--phase_damping 0.707`) — a second-order
  phase-locked loop derotates residual carrier phase for single-carrier.
- **OFDM CFO** — Schmidl-Cox estimates fractional carrier frequency offset from the repeated
  half-symbol preamble: `ε = angle(P)/π`, where `P = Σ r[n]·conj(r[n+N/2])` over the identical
  halves. The whole burst is derotated by `exp(−j·2π·ε·n/N)`. Range ±½ subcarrier spacing;
  residual is mopped up per-symbol by the pilot CPE (§3.2).

**Math (Schmidl-Cox).** The sync symbol is two identical halves `[A A]`. The timing metric
`M[d] = |P[d]|² / R[d]²` (P = correlation of the two halves, R = energy) plateaus where the
halves align; the plateau left-edge (plus a small guard into the CP) is the frame start, and
`arg(P)` gives the CFO. Alternatives: Minn/Park variants (sharper timing metric), CP-based
(blind, pilotless) sync.

---

## 6. Channel equalization (single-carrier)

`--eq_type None` (default) | `LMS` | `RLS` | `DFE`. `--eq_taps 11`, `--eq_mu 0.3` (NLMS step),
`--eq_dd false` (decision-directed tracking after training).

**Math.** A linear FIR equalizer `w` inverts channel ISI: `ŝ = wᴴ·r`. Trained on the known
preamble by **least-squares (LS/MMSE)**: `w = (RᴴR + λI)⁻¹ Rᴴs`, which is exact for a complex
(Zadoff-Chu) preamble. **NLMS** updates `w ← w + μ·e·r*/‖r‖²` sample-by-sample (used for a real
preamble or DD tracking). A center-tap delay must be compensated (fixed in this project).

**Status / why default None.** On a clean cabled link no equalizer is needed. The LMS
decision-directed loop currently **diverges on the real hardware signal** (DD error grows and
destroys the symbols) — verified OTA where `None` decodes and `LMS` produces garbage. For
frequency-selective channels, prefer **OFDM** (§3.2), which equalizes per-subcarrier without an
adaptive FIR. Alternatives: DFE (feedback of past decisions, better on deep nulls),
RLS (faster convergence than LMS, O(n²) cost), MLSE/Viterbi equalization (optimal, expensive).

---

## 7. Forward error correction (FEC)

`--fec true` (default off) — rate-1/2, constraint-length-7 **convolutional code** with
**Viterbi** hard-decision decoding. Must match on both ends; halves the payload rate (2× symbols).

**Math.** Encoder: two generator polynomials `G1 = 0171₈`, `G2 = 0133₈` (the standard
NASA / 802.11a code) produce 2 output bits per input bit from a 6-stage shift register, spreading
each bit's influence over `K = 7` bits (memory). The **Viterbi** decoder finds the
maximum-likelihood path through the 64-state trellis (survivor path per state, traceback of the
predecessor), correcting scattered bit errors. Validated to ~100% CRC-OK up to ~2% raw BER.
Two original bugs (encoder output taken from the *next* state; traceback storing the bit not the
predecessor) were fixed.

**Common alternatives:** LDPC and Turbo codes (near-Shannon, used in Wi-Fi/5G), Reed-Solomon
(burst errors), Polar codes (5G control), soft-decision Viterbi (~2 dB better than the
hard-decision decoder here).

---

## 8. Error detection + ARQ

- **CRC-16-CCITT** appended to every chunk; the RX drops any chunk that fails the check
  (guarantees the delivered message is error-free).
- **Stop-and-wait ARQ** (`--role source_arq` / `sink_arq`): the source sends a chunk and waits
  for an ACK; on `--timeout 3000` ms it retransmits; it advances when ACKed and exits when all
  chunks are ACKed. The sink CRC-verifies each chunk, ACKs only verified ones, re-ACKs duplicates,
  and auto-stops once every chunk is in.
- **ACK transport** (`--ack-transport`):
  - `tcp` (default) — ACK over a socket; **no reverse RF path needed**. Source connects to
    `--ack-host 127.0.0.1 : --ack-port 5599` (the sink listens there). Ideal when both radios
    share one host.
  - `rf` — ACK sent back over the air on the second RF path (needs a clean reverse link).

**Math/theory.** CRC = remainder of the message polynomial divided (mod-2) by the CRC-16-CCITT
generator `x¹⁶+x¹²+x⁵+1`; catches all 1–2 bit errors, all odd-count errors, and all bursts ≤16
bits. Stop-and-wait is the simplest ARQ (low throughput on long links because it idles waiting
for each ACK). Alternatives: Go-Back-N and Selective-Repeat ARQ (pipelined, higher throughput),
Hybrid ARQ (FEC + ARQ combined, as used here when `--fec` is on).

**One-way mode** (`--role tx` / `rx`, no ACK): the TX cycles all chunks `--tx-reps` times; the RX
reassembles from CRC-verified chunks and auto-stops `--rx-idle-timeout 8` s after the last burst.

---

## 9. Automatic gain control (AGC)

`--AGC_type Feed` (feed-forward, default) or `Closed` (closed-loop). Feed-forward measures the
detected burst's RMS and scales it to unit power in one shot (no loop lag); closed-loop adapts a
gain over time. The energy detector's captured burst feeds the AGC before sync.

---

## 10. Visualization / EVM

`--viz true` (default) captures one TX and one RX burst and **auto-saves a figure on exit** to
`<viz-dir>/<scheme>/figure.png` (per-modulation folder, `--viz-dir viz`). The 2×3 figure shows
TX/RX **time domain**, **spectrum** (FFT), and **constellation** with the ideal points overlaid
and an **EVM %** readout. Rough EVM ceilings for reliable decode: QPSK tolerates ~30–35% (more
with FEC), 16-QAM needs <~12%, 64-QAM <~6%. Render manually with
`python3 tools/plot_viz.py <dir> --fs 1.6e6 --save out.png`.

---

## 11. Complete command-line reference

### Mode / roles
| Option | Default | Controls |
|---|---|---|
| `--role` | `both` | `tx`, `rx`, `both`, `source_arq`, `sink_arq` |
| `--mode` | `source` | legacy source/sink selector |
| `--tx-reps` | `20` | one-way: times to cycle all chunks (no ACK) |
| `--rx-idle-timeout` | `8.0` | one-way RX: auto-stop after N s of no bursts (0 = until Ctrl-C) |
| `--num_bits` | `1000` | payload bits per packet |
| `--interval` | `3000` | TX gap between packets (ms) |

### ARQ / ACK
| Option | Default | Controls |
|---|---|---|
| `--ack-transport` | `tcp` | ACK channel: `tcp` or `rf` |
| `--ack-host` | `127.0.0.1` | TCP: sink IP the source connects to |
| `--ack-port` | `5599` | TCP: ACK socket port |
| `--timeout` | `3000` | source ACK timeout (ms) before retransmit |
| `--timer_interval` | `1000` | sink ACK timer interval (ms) |

### Modulation / waveform / FEC
| Option | Default | Controls |
|---|---|---|
| `--scheme` | `QPSK` | constellation (§2) |
| `--fec` | `false` | rate-1/2 K=7 conv + Viterbi (must match both ends) |
| `--waveform` | `sc` | `sc` (single-carrier) or `ofdm` |
| `--ofdm-fft` | `64` | OFDM subcarrier count (FFT size) |
| `--ofdm-cp` | `16` | OFDM cyclic-prefix length |
| `--ofdm-tx-peak` | `0.5` | OFDM peak scaling (PAPR / clipping guard) |
| `--sps` | `2` | samples/symbol (informational) |

### Preamble / pulse shaping
| Option | Default | Controls |
|---|---|---|
| `--preamble` | `m-sequence` | `m-sequence` or `zadoff` |
| `--m` | `5` | m-sequence order (length 2^m−1) |
| `--add_preamble` | `true` | prepend preamble to each packet |
| `--filter_type` | `rrc` | `rrc` / `rc` / `lp` |
| `--roll_off` | `0.25` | RRC/RC roll-off β |
| `--num_taps` | `151` | filter tap count |
| `--U` / `--D` | `2` / `1` | pulse-shaper up/down sampling (wire sps = U/D) |
| `--symbol_rate` | `0.8e6` | symbol rate (Hz) |
| `--num_threads` | `1` | FFT threads |

### Energy detection
| Option | Default | Controls |
|---|---|---|
| `--alpha` | `0.95` | IIR power-smoothing (larger = smoother) |
| `--det-adaptive` (`--IIR_threshold_adaptive`) | `true` | use noise_floor×mult vs fixed threshold |
| `--det-mult` (`--IIR_threshold_multiplier`) | `5.0` | adaptive threshold multiplier (raise OTA to 10–30) |
| `--det-threshold` (`--energy_threshold`) | `1e-7` | fixed threshold (adaptive off only) |
| `--det-continuous` | `true` | re-track noise floor during idle vs one-shot calib |
| `--energy_packet_size` | `3300` | samples to collect after detection (auto-sized) |
| `--IIR_window_size` | `20` | IIR window size |

### Synchronization / timing / phase
| Option | Default | Controls |
|---|---|---|
| `--sync-threshold` (`--sync_threshold`) | `15.0` | ACQ correlation gate (set below true peak, above noise) |
| `--sps_sync` | `5` | samples/symbol at matched-filter output |
| `--recv_msg_len` | auto | data symbols to extract per burst (auto from scheme) |
| `--timing_loop_bw` | `0.015` | Gardner TED loop bandwidth BnT |
| `--timing_damping` | `0.707` | Gardner TED damping |
| `--phase_loop_bw` | `0.02` | carrier phase PLL bandwidth |
| `--phase_damping` | `0.707` | carrier phase PLL damping |

### Equalizer
| Option | Default | Controls |
|---|---|---|
| `--eq_type` | `None` | `None` / `LMS` / `RLS` / `DFE` (see §6 — LMS diverges OTA) |
| `--eq_taps` | `11` | equalizer tap count |
| `--eq_mu` | `0.3` | NLMS step (DD / real-preamble training) |
| `--eq_dd` | `false` | decision-directed tracking after LS training |

### RF hardware (TX)
| Option | Default | Controls |
|---|---|---|
| `--tx-args` | `""` | UHD TX device (e.g. `serial=30CD424`) |
| `--tx-rate` | `1.6e6` | TX sample rate (Hz) = symbol_rate·U/D |
| `--tx-freq` | `2.412e9` | TX centre frequency (Hz) |
| `--tx-gain` | `20` | TX gain (dB) |
| `--tx-bw` | `1.0e6` | TX analog bandwidth (Hz) |
| `--tx-ant` | `TX/RX` | TX antenna port |
| `--tx-subdev` | `A:A` | TX subdev spec |
| `--tx-channel` | `0` | TX channel index |

### RF hardware (RX)
| Option | Default | Controls |
|---|---|---|
| `--rx-args` | `""` | UHD RX device (e.g. `serial=30CD3F7`) |
| `--rx-rate` | `1.6e6` | RX sample rate (Hz), must equal TX rate |
| `--rx-freq` | `2.412e9` | RX centre frequency (Hz) |
| `--rx-gain` | `30` | RX gain (dB) — key for EVM/clipping (§ recipes) |
| `--rx-bw` | `1.0e6` | RX analog bandwidth (Hz) |
| `--rx-ant` | `RX2` | RX antenna port |
| `--rx-subdev` | `A:A` | RX subdev spec |
| `--rx-channel` | `0` | RX channel index |

### Common RF / buffering / misc
| Option | Default | Controls |
|---|---|---|
| `--ref` | `internal` | clock reference: `internal` / `external` / `mimo` |
| `--settling` | `0.2` | RF settling time (s) |
| `--uhd_timeout` | `1000` | UHD TX timeout (ms) |
| `--samps_per_buff` | `10000` | samples per UHD receive buffer |
| `--num_recv_request` | `0` | total samples to receive (0 = continuous) |
| `--AGC_type` | `Feed` | `Feed` (feed-forward) or `Closed` (closed-loop) |
| `--skip-rate-check` | off | bypass startup rate-consistency check |
| `--viz` | `true` | capture + auto-save TX/RX figure on exit |
| `--viz-dir` | `viz` | base dir for figures (per-scheme subfolder made) |

---

## 12. Verified command recipes (915 MHz, VERT900 antennas)

**QPSK OFDM + ARQ (reliable OTA demo).** Start the sink first.
```bash
# Sink / RX
./sdr_system --role sink_arq --rx-args serial=30CD3F7 --tx-args serial=30CD3F7 \
  --rx-subdev A:A --rx-ant RX2 --rx-freq 915e6 --rx-rate 1.6e6 --rx-gain 25 \
  --waveform ofdm --ofdm-fft 64 --ofdm-cp 16 --scheme QPSK --fec true \
  --ack-transport tcp --ack-port 5599 --det-mult 3 --viz-dir viz
# Source / TX
./sdr_system --role source_arq --tx-args serial=30CD424 --rx-args serial=30CD424 \
  --tx-subdev A:A --tx-ant TX/RX --tx-freq 915e6 --tx-rate 1.6e6 --tx-gain 82 \
  --waveform ofdm --ofdm-fft 64 --ofdm-cp 16 --ofdm-tx-peak 0.5 --scheme QPSK --fec true \
  --ack-transport tcp --ack-host 127.0.0.1 --ack-port 5599 --timeout 3000 --viz-dir viz
```

**Single-carrier QPSK one-way** (swap `--waveform ofdm …` for `--waveform sc`, roles `rx`/`tx`,
add `--tx-reps 20`).

**16-QAM / 64-QAM**: same as above with `--scheme 16QAM` (or `64QAM`) on both ends — but needs
much higher SNR (EVM < ~12% / 6%). Use a cabled link (SMA + 30–40 dB attenuator) or very close
antennas; tune `--rx-gain` so the RX `Peak=` stays under ~0.8 (below 1.0 = ADC clipping) while
`RMS` is as high as possible. Best OTA point found so far: `--rx-gain 21 --tx-gain 80 --ofdm-tx-peak 0.45`.

### Level-tuning cheatsheet (the #1 cause of "nothing decodes")
- RX log `Peak > 1.0` → **clipping**, lower `--rx-gain` (fatal for QAM).
- No detections (`bursts=0`) → signal too weak, raise `--tx-gain` or `--rx-gain`, or lower `--det-mult`.
- Detections but CRC fails → check the constellation EVM in the figure; if the order is too high
  for the link SNR, drop to QPSK or improve the link.
- Keep `--fec true` on marginal links; it closes the last few % of bit errors.
