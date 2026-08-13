# Two-terminal TX/RX demo (no radio)

Runs the transceiver's DSP locally across **two terminals** — one TX, one RX —
connected by a localhost TCP socket instead of USRPs. The "channel" adds AWGN
plus a configurable carrier **frequency** and **phase** offset, and the RX runs
the real receive chain and logs each stage:

```
[CHANNEL]  injected noise / CFO / phase
[SYNC]     which correlation sample is the peak   (+ peak value / m-seq length)
[CFO]      estimated frequency offset  (vs injected)
[PHASE]    estimated carrier phase      (vs injected + guard-ramp)
[DEMOD]    recovered constellation indices + bit pattern (vs expected)
[DECODE]   header (chunk index / total) + decoded payload text
```

It exercises exactly the stages that matter for an AWGN channel: packet
detection / time sync, CFO, phase, demod, and decode. The RF front-end stages
(energy detection, AGC, matched filter, timing recovery) live in the UHD path
and aren't part of a noise-only symbol-level channel, so they're not run here.
Everything is at 1 sample/symbol.

## Build (needs only g++ / C++17 — no UHD)

```bash
bash sim/build.sh          # produces sim/tx_app and sim/rx_app
```

## Run — two terminals

Start the **RX first** (it listens), then the **TX** (it connects). The scheme
and preamble `-m` must match on both ends.

Terminal 1 (receiver):
```bash
./sim/rx_app --scheme QPSK --port 5555 --snr-db 28 --cfo-hz 3820 --phase-deg 35
```

Terminal 2 (transmitter):
```bash
./sim/tx_app --scheme QPSK --port 5555
# or send your own text:
./sim/tx_app --scheme QPSK --port 5555 --message "May the force be with you."
# or from a file:
./sim/tx_app --scheme QPSK --port 5555 --msg-file mymessage.txt
```

The RX prints the per-stage logs and, at the end, the reassembled message and
the overall BER.

## Options

TX (`tx_app`):
| flag | default | meaning |
|---|---|---|
| `--scheme` | QPSK | modulation (must match RX) |
| `--m` | 5 | m-sequence preamble order (must match RX) |
| `--host` | 127.0.0.1 | RX host |
| `--port` | 5555 | TCP port |
| `--payload-bytes` | 125 | bytes per chunk |
| `--message` | built-in | message text |
| `--msg-file` | — | read message from a file |

RX (`rx_app`) — same `--scheme`/`--m`/`--port`, plus the channel:
| flag | default | meaning |
|---|---|---|
| `--snr-db` | 30 | AWGN level (Es/N0); lower = noisier |
| `--noise-sigma` | — | set noise std directly (overrides `--snr-db`) |
| `--cfo-hz` | 3820 | injected carrier frequency offset |
| `--cfo-rad` | — | CFO as rad/symbol (overrides `--cfo-hz`) |
| `--phase-deg` | 35 | injected static carrier phase |
| `--symbol-rate` | 0.8e6 | symbol rate (for Hz↔rad/sym conversion) |

## Supported schemes

Absolute (full CFO + phase chain): `BPSK QPSK 8-PSK 16-QAM 32-QAM 64-QAM
128-QAM 256-QAM 16APSK 32APSK`.
Differential (CFO + differential demod, phase-robust): `DBPSK DQPSK 8-DPSK`.

Notes:
- Dense QAM (128/256-QAM) needs higher `--snr-db` for BER 0; if you keep the
  default SNR you'll see some bit errors — lower the noise or use a smaller
  constellation.
- `PI4-QPSK` and differential-QAM aren't carried by this two-terminal app
  (π/4 is shown in `tests/mod_loopback_test`; differential-QAM is unsupported by
  the multiply-based differential coder).
- Keep the injected CFO modest (the default ~0.03 rad/sym is fine). A very large
  CFO shrinks the preamble correlation below the guard sidelobe and time sync can
  mis-lock — the same `sync_threshold` caveat noted in `CHANGES.md`.
```
