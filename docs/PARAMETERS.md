# All controllable parameters

Every option below can be set three equivalent ways — all reach the same
`sdr_system` binary. Option names accept **hyphens or underscores**.

| Where | How | Example |
|---|---|---|
| JSON config (`run.py`) | `"name": value` | `"scheme": "16-QAM"` |
| Command line (overrides JSON) | `--name value` | `--scheme 16-QAM` |
| Python (`sdr.py`) | `SDR(name=value)` | `SDR(scheme="16-QAM")` |

In JSON: booleans are `true`/`false`, numbers are bare (`915e6`), strings are
quoted. **Flags** (Type = _flag_) take no value on the command line (just
`--name`); in JSON/Python set them to `true`.

The **Default** column is the value used when you omit the option: flags default to `false`; `(empty)` means an empty/unset string (e.g. auto-pick the device, or use the built-in message); `_(alias)_` marks an option that inherits its primary option's default.

> Auto-generated from `sdr_system --help` — always lists every current option (**110** total). Do not edit by hand.

## Mode, message & transmission

| Option | Type | Default | Description |
|---|---|---|---|
| `--mode` | value | `source` | Operation mode: source or sink |
| `--role` | value | `both` | tx = transmit only (one B210), rx = receive only (other B210), source_arq / sink_arq = the two ends of stop-and-wait ARQ, sense = channel-occupancy sensing (RX energy over a window, no decode), both = original single-box full-duplex/loopback |
| `--tx-reps` | value | `20` | role tx: how many times to cycle through all chunks (one-way, no ACK) |
| `--rx-idle-timeout` | value | `8` | role rx: auto-stop and print the message after this many seconds with no new bursts (TX has finished). 0 = run until Ctrl-C |
| `--num_bits` | value | `1000` | Payload bits per packet |
| `--interval` | value | `3000` | TX interval between packets (ms) |
| `--tx-mode` | value | `burst` | role tx transmission mode: burst (discrete packets/tone bursts with --interval gaps, repeated --tx-reps times then stop) or continuous (transmit until Ctrl-C — a continuous data loop or an unbroken carrier for sine/cosine) |
| `--message-type` | value | `bytes` | payload: bytes (given text, default Star Wars crawl; set with --message) \| random (num_bits random bits) \| sine \| cosine (raw baseband test tone) \| chirp (LoRa/CSS up-chirp sweep; see --chirp-bw/--chirp-sf) — raw waveforms are role tx/rx only) |
| `--message` | value | `(empty)` | text payload for --message-type bytes (overrides the default Star Wars crawl) |
| `--tone-freq` | value | `100000` | sine/cosine baseband frequency in Hz (default 100 kHz) |
| `--tone-amp` | value | `0.5` | sine/cosine amplitude, keep < 1.0 to avoid DAC clipping |

## Modulation & waveform

| Option | Type | Default | Description |
|---|---|---|---|
| `--scheme` | value | `QPSK` | Modulation: QPSK / DQPSK / DBPSK / 16-QAM / ... |
| `--fec` | value | `0` | Forward Error Correction (rate-1/2 K=7 convolutional + Viterbi). Corrects bit errors so a noisy link decodes error-free; must match on both ends. Halves the payload rate (2x the symbols). |
| `--waveform` | value | `sc` | Waveform: sc (single-carrier) or ofdm. OFDM handles multipath/CFO natively (per-subcarrier equalization) — best for dense QAM. |
| `--ofdm-fft` | value | `64` | OFDM FFT size (number of subcarriers) |
| `--ofdm-cp` | value | `16` | OFDM cyclic-prefix length (>= channel delay spread) |
| `--ofdm-tx-peak` | value | `0.5` | OFDM TX peak scaling (high PAPR — keep the DAC out of clipping) |

## Preamble & pulse shaping

| Option | Type | Default | Description |
|---|---|---|---|
| `--preamble` | value | `m-sequence` | Preamble type: m-sequence or zadoff |
| `--m` | value | `5` | m-sequence order (length = 2^m-1) |
| `--add_preamble` | value | `1` | Prepend preamble to each packet |
| `--U` | value | `2` | TX pulse-shaper upsampling factor (wire sps = U/D) |
| `--D` | value | `1` | TX pulse-shaper downsampling factor (wire sps = U/D) |
| `--filter_type` | value | `rrc` | Filter type: rrc / rc / lp |
| `--symbol_rate` | value | `800000` | Symbol rate (Hz) |
| `--num_taps` | value | `151` | Filter tap count |
| `--roll_off` | value | `0.25` | Roll-off factor |
| `--num_threads` | value | `1` | FFT threads |
| `--sps` | value | `2` | Samples per symbol (informational) |

## TX radio

| Option | Type | Default | Description |
|---|---|---|---|
| `--tx-args` | value | `(empty)` | UHD TX device args (e.g. serial=XXXXXXX) |
| `--tx-rate` | value | `1600000` | TX sample rate (Hz) = symbol_rate * U/D |
| `--tx-freq` | value | `2412000000` | TX centre frequency (Hz) |
| `--tx-gain` | value | `20` | TX gain (dB) |
| `--tx-bw` | value | `1000000` | TX bandwidth (Hz) |
| `--tx-ant` | value | `TX/RX` | TX antenna port |
| `--tx-channel` | value | `0` | TX channel index |
| `--tx-subdev` | value | `A:A` | TX subdev spec |
| `--tx-dc-i` | value | `0` | Manual TX LO-leakage null, I component (normalized [-1,1]). Tune with --tx-dc-q to minimize the RX DC spike on a direct cable (dense QAM). |
| `--tx-dc-q` | value | `0` | Manual TX LO-leakage null, Q component (normalized [-1,1]). |
| `--tx-scale` | value | `1` | TX digital back-off for the single-carrier waveform, multiplied into every sample before the DAC (1.0 = unchanged). fc32 full scale is 1.0, so if [TX VALIDATE] reports a peak above that the DAC is clipping — which distorts the payload while the preamble still correlates. Try 0.7, then lower. |

## RX radio

| Option | Type | Default | Description |
|---|---|---|---|
| `--rx-args` | value | `(empty)` | UHD RX device args |
| `--rx-rate` | value | `1600000` | RX sample rate (Hz) = tx_rate (integer samples/symbol) |
| `--rx-freq` | value | `2412000000` | RX centre frequency (Hz) |
| `--rx-gain` | value | `30` | RX gain (dB) |
| `--rx-bw` | value | `1000000` | RX bandwidth (Hz) |
| `--rx-ant` | value | `RX2` | RX antenna port |
| `--rx-channel` | value | `0` | RX channel index |
| `--rx-subdev` | value | `A:A` | RX subdev spec |

## ARQ / ACK

| Option | Type | Default | Description |
|---|---|---|---|
| `--ack-transport` | value | `tcp` | ARQ ACK channel: tcp (default, ACK over a socket — no reverse RF needed) or rf (ACK over the second RF path, RF B) |
| `--ack-host` | value | `127.0.0.1` | TCP ACK: host/IP of the sink that the source connects to (default localhost) |
| `--ack-port` | value | `5599` | TCP ACK: socket port |
| `--timeout` | value | `3000` | ACK timeout in ms (source) |
| `--timer_interval` | value | `20` | sink FIFO poll interval in ms — this SETS the ACK round-trip latency, so keep it small (was 1000, which made ARQ ~5x slower) |

## Energy detection

| Option | Type | Default | Description |
|---|---|---|---|
| `--alpha` | value | `0.949999988` | Energy-detector IIR smoothing: filtered = (1-alpha)*inst + alpha*prev, so LARGER alpha = MORE smoothing. The old 0.02 barely smoothed, so the detector fired on every noise spike (thousands of false bursts) and cut real bursts apart on the RRC envelope. 0.95 (~20-sample time constant) gives one clean capture per burst. |
| `--energy_threshold` | value | `1.00000001e-07` | Fixed energy threshold (used only if the adaptive detector is off). Alias: --det-threshold |
| `--det-threshold` | value | `1.00000001e-07` _(alias)_ | alias for --energy_threshold (fixed detector threshold) |
| `--energy_packet_size` | value | `3300` | Samples to collect after energy detection |
| `--IIR_window_size` | value | `20` | IIR window size |
| `--IIR_threshold_adaptive` | value | `1` | Use the auto (adaptive) detection threshold = noise_floor × multiplier. Alias: --det-adaptive |
| `--det-adaptive` | value | `1` _(alias)_ | alias for --IIR_threshold_adaptive (use the auto detector threshold) |
| `--IIR_threshold_multiplier` | value | `5` | Auto detector threshold = measured noise_floor × this. RAISE it over-the-air (e.g. 10-30) so the detector fires only on real bursts, not ambient RF; too high and it misses weak bursts. Alias: --det-mult |
| `--det-mult` | value | `5` _(alias)_ | alias for --IIR_threshold_multiplier (auto-threshold noise multiplier) |
| `--det-continuous` | value | `1` | Continuously re-track the noise floor during idle (default true), vs a one-shot startup calibration. Robust to drifting ambient noise. |

## Synchronization

| Option | Type | Default | Description |
|---|---|---|---|
| `--sps_sync` | value | `5` | Samples per symbol at match-filter output |
| `--sync_threshold` | value | `15` | ACQ correlation threshold — raise it over-the-air so ambient-noise bursts are rejected (a real preamble peaks near the preamble length ~31 after AGC; noise correlates far lower). Watch the '[ACQ] Peak correlation' lines and set it below the true peak but above the noise. Alias: --sync-threshold |
| `--sync-threshold` | value | `15` _(alias)_ | alias for --sync_threshold (hyphenated spelling) |
| `--recv_msg_len` | value | `508` | Data symbols to extract (QPSK: 1016bits/2bps=508) |
| `--samps_per_buff` | value | `10000` | Samples per UHD receive buffer |
| `--num_recv_request` | value | `0` | Total samples to receive (0=continuous) |

## Timing & phase recovery

| Option | Type | Default | Description |
|---|---|---|---|
| `--timing_loop_bw` | value | `0.0149999997` | Gardner TED loop bandwidth BnT |
| `--timing_damping` | value | `0.707000017` | Gardner TED damping factor |
| `--phase_loop_bw` | value | `0.0199999996` | Phase PLL loop bandwidth |
| `--phase_damping` | value | `0.707000017` | Phase PLL damping factor |

## Equalizer

| Option | Type | Default | Description |
|---|---|---|---|
| `--eq_taps` | value | `11` | Number of equaliser taps |
| `--eq_mu` | value | `0.300000012` | Equalizer NLMS step (used for DD tracking / real-preamble training) |
| `--eq_dd` | value | `0` | Equalizer decision-directed tracking after training (default off: the LS-trained eq is exact frozen; DD can diverge on noisy dense QAM) |
| `--eq_type` | value | `None` | Equaliser type: LMS / RLS / DFE / None. Default None: on a clean cabled link no equaliser is needed, and the LMS loop currently DIVERGES on the real signal (decision-directed error grows and destroys the symbols) — verified on hardware, where None decodes the message and LMS produces garbage. Leave None until the LMS/RLS/DFE update is debugged. |

## Visualization

| Option | Type | Default | Description |
|---|---|---|---|
| `--viz` | value | `1` | capture TX/RX signals and auto-save the plot to <viz-dir>/<scheme>/figure.png (default true; --viz false disables) |
| `--viz-dir` | value | `results/phy_outputs` | base directory for --viz output, relative to the working directory (a per-modulation subfolder is made). Defaults under results/ so a run does not scatter output across the repo root. |

## RF, clock & misc

| Option | Type | Default | Description |
|---|---|---|---|
| `--config` | value | `(empty)` | read options from a config file (one 'name = value' per line, '#' comments; use the long option name without '--'). Any option given on the COMMAND LINE overrides the file. Generate a fully defaulted template with: sdr_system --help (or see phy.cfg). |
| `--stop-on-complete` | value | `1` | role rx: stop as soon as every chunk of a finite message (bytes / fixed-length random) is CRC-verified (default true). false = keep receiving (collect duplicates / measure the link) until the idle timeout or Ctrl-C. Ignored for continuous TX. |
| `--marl-report` | value | `0` | role rx: emit one machine-readable line per CRC-OK burst — '[BURST] id=<payload byte0> idx=<i> tot=<t> nbytes=<n> hex=<HEX>' — for the MARL multi-agent AP to route an ACK to the transmitting agent (payload byte 0 = agent id). No effect on the decoded message. |
| `--skip-rate-check` | value | `0` | bypass the startup rate-chain consistency check (run even if rates mismatch) |
| `--max-attempts` | value | `50` | source_arq: give up on a chunk after this many un-ACKed sends. 0 = never give up (keeps TX/RX in lockstep on a marginal link, since a given-up chunk desyncs a paired sender/receiver loop). |
| `--serve-forever` | value | `0` | sink_arq: act as a persistent access point — keep the radio warm and re-accept a new source per session instead of exiting after one message (for fire-on-demand random access, e.g. the MARL bridge). |
| `--on-demand` | value | `0` | source_arq: warm transmitter — keep the radio warm and send ONE packet each time a line is read on stdin, printing 'RESULT acked=0\|1' per fire. No per-fire radio re-init (pairs with a --serve-forever AP). |
| `--bytes-length` | value | `125` | payload bytes per chunk (default 125). Larger chunks amortise the per-burst detect/sync/ACK overhead — higher throughput. MUST match on TX and RX. Total chunks <= 255. |
| `--payload-file` | value | `(empty)` | TX: send the raw bytes of this file as the payload (binary, e.g. a serialized gradient). Overrides --message / --message-type. |
| `--out-file` | value | `(empty)` | RX (rx / sink_arq): write the decoded payload as raw bytes to this file (pairs with --payload-file for a binary byte-pipe). |
| `--ber-expected` | value | `(empty)` | rx / sink_arq: file with the KNOWN transmitted message; every rejected burst is then scored against it, printing pre-FEC BER vs the closest chunk. ~50% = the bits are not framed where sync thinks they are; a few % = link margin; ~0% = only the header/CRC disagrees. |
| `--sense-window` | value | `10` | role sense: energy-integration window in ms (default 10) |
| `--sense-threshold-db` | value | `-30` | role sense: channel is 'busy' when the window's avg power (dB) exceeds this. Calibrate to your gain/noise floor (channel_sense.py can auto-calibrate). |
| `--sense-count` | value | `1` | role sense: number of consecutive windows to measure/report (0 = stream forever until Ctrl-C — for a persistent sensing feed) |
| `--chirp-bw` | value | `0` | LoRa/CSS chirp sweep bandwidth in Hz (--message-type chirp); 0 = full sampled band (tx-rate) |
| `--chirp-sf` | value | `7` | LoRa spreading factor 7-12; chirp symbol duration = 2^SF / bandwidth |
| `--chirp-down` | value | `0` | generate a down-chirp instead of an up-chirp (default up) |
| `--ref` | value | `internal` | Clock reference: internal / external / mimo |
| `--settling` | value | `0.20000000000000001` | Settling time (s) |
| `--uhd_timeout` | value | `1000` | UHD TX timeout (ms) |
| `--AGC_type` | value | `Feed` | AGC type: Feed or Closed |
| `--dc-block` | value | `0` | Experimental per-burst DC-block high-pass on the RX (default false). A gentle cutoff barely dents the cable leakage; an aggressive one distorts the preamble and breaks sync — prefer --tx-dc-i/--tx-dc-q. |
| `--fec_soft` | value | `0` | Soft-decision decode (needs --fec true). RX passes per-bit LLRs to the decoder for ~2-3 dB coding gain vs hard-decision. Coherent schemes only (differential fall back to hard). RX-side only; TX unaffected. For LDPC this is the native decode path — recommended whenever --fec-type ldpc. |
| `--fec-type` | value | `conv` | FEC code family (needs --fec true; must MATCH on both ends): conv = rate-1/2 K=7 convolutional + Viterbi (default), ldpc = rate-1/2 systematic IRA/staircase LDPC + min-sum belief propagation, turbo = rate-1/2 punctured PCCC (two (7,5) RSC + interleaver) + iterative max-log-MAP BCJR. ldpc/turbo are soft-native — pair with --fec_soft. |
| `--ldpc-k` | value | `256` | LDPC/turbo info-block size in bits. Payload is segmented into k-bit blocks (last zero-padded); both ends MUST use the same k so the code matches. Larger k codes better but adds latency. |
| `--fec-iters` | value | `0` | Tuning: max decoder iterations (LDPC belief-prop / turbo BCJR). 0 = default (LDPC 50, turbo 6). Raise (e.g. turbo 8-12) if a marginal link won't converge. Decoder-only — need not match the other end. |
| `--fec-scale` | value | `0` | Tuning: min-sum (LDPC) / extrinsic (turbo) normalization scale, 0.7-0.9 typical. 0 = default 0.75. Decoder-only — need not match the other end. |
| `--ldpc-col-weight` | value | `3` | Tuning: LDPC variable-node degree (default 3). Higher = denser code, sometimes stronger. CHANGES the parity-check matrix, so BOTH ends must match. Ignored by conv/turbo. |
| `--allow-rate-coercion` | value | `0` | Transmit/receive even when UHD could not give the exact --tx-rate / --rx-rate you asked for. Off by default: a coerced rate leaves the preamble correlating but drifts the payload's symbol timing, so the link syncs and then decodes garbage. Prefer a rate the device can hit exactly (master_clock_rate / integer). |
| `--lora-sf` | value | `8` | LoRa/CSS spreading factor 7-12 for --waveform lora (2^SF chips/symbol; higher = more processing gain / range, slower) |
| `--lora-sync-word` | value | `18` | LoRa network id (2 sync symbols after the preamble); RX rejects frames with a different word. 18=0x12 private, 52=0x34 public. Must match TX & RX |
| `--cfo_prior_alpha` | value | `1` | Cross-burst CFO estimate smoothing (EMA alpha). 1.0=per-burst (cold LO, default); <1.0 blends history (warm resident LO only, e.g. 0.5) |

