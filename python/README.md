# Python wrapper for the SDR PHY

`sdr.py` is a thin Python wrapper around the C++ `sdr_system` binary. It exposes
**every** command-line option as a keyword argument, builds the command line, and
launches it as a subprocess.

**`sdr.py` is auto-generated** from `sdr_system --help` by `tools/gen_python_api.py`,
which runs as a CMake post-build step — so whenever you rebuild the C++, the Python
wrapper picks up any new/changed options automatically. **Don't edit `sdr.py` by hand.**

> **[`OPTIONS.md`](OPTIONS.md) lists every controllable option** (grouped, with type,
> default, and description) and how to set it via JSON / command line / Python. It is
> auto-generated too, so it is always complete and current. Start there to see what you
> can change.

## Quick start

```python
from sdr import SDR, tx, rx, sink_arq, source_arq, both, run_pair, options

options()                                   # list every option + default + help

# Pick a role — hyphens or underscores both work as keywords:
tx(tx_args="serial=30CD424", scheme="QPSK", tx_gain=78, fec=True).run()   # TX only
rx(rx_args="serial=30CD3F7", scheme="QPSK", fec=True).run()               # RX only
both(tx_args="serial=30CD424", rx_args="serial=30CD3F7").run()            # TX+RX, one box

# Two boxes, ARQ over the air — RX and TX at the same time (RX starts first):
run_pair(sink_arq(rx_args="serial=30CD3F7", scheme="QPSK", fec=True),
         source_arq(tx_args="serial=30CD424", scheme="QPSK", fec=True))
```

## The role modes

| Helper | `--role` | What it does |
|---|---|---|
| `tx(...)` | `tx` | transmit only (one radio) |
| `rx(...)` | `rx` | receive only (other radio) |
| `both(...)` | `both` | one process transmitting **and** receiving (single-box full-duplex / loopback ARQ) |
| `sink_arq(...)` / `source_arq(...)` | `sink_arq` / `source_arq` | the two ends of stop-and-wait ARQ (two radios) |

`run_pair(rx_side, tx_side)` launches two ends together: it starts the receiver,
gives it a head start, starts the transmitter, waits for TX to finish, then lets
the RX self-terminate (or stops it).

## API

- `SDR(**opts)` — one invocation. Set any option (`tx_gain=78`, `waveform="ofdm"`,
  `message_type="random"`, `fec=True`, `skip_rate_check=True`, …). Python `True/False`
  become `true/false`; bool-switch flags (`skip-rate-check`) are added only when `True`.
- `.command()` → the shell command string (inspect without running).
- `.run(**overrides)` → blocking `subprocess.run`.
- `.popen(**overrides)` → background `subprocess.Popen`.
- `.set(**opts)` → change options, chainable.
- `SDR(..., extra=["--foo","bar"])` → pass raw extra args.
- Binary path: defaults to `../build/sdr_system`; override with `binary=...` or the
  `SDR_SYSTEM_BIN` environment variable.

See `example.py` for the four role modes, scheme sweeps, a random-bit test, and a tone.
Run it to print the commands without touching hardware.

## JSON config files (recommended — clearer than a long command line)

`run.py` runs the PHY from a JSON file. Every key is an option name from `sdr.py`
(hyphens or underscores). Ready-made configs are in `configs/`.

```bash
python3 run.py --list                                  # list every config + scheme/gains
python3 run.py configs/qpsk_arq_pair.json              # run it
python3 run.py configs/qpsk_arq_pair.json --dry-run    # just print the command(s)
```

### Per-scheme configs — pick your modulation (recommended)

One file per validated scheme, each a **complete ARQ pair with the tuned gains
baked in** (mirrors the table in [`../COMMANDS.md`](../COMMANDS.md)) — so you
never have to remember the operating point. Run one side per terminal, **RX first**:

```bash
python3 run.py configs/8psk.json --only rx      # terminal 1 (sink)  — start first
python3 run.py configs/8psk.json --only tx      # terminal 2 (source)
```

| Config | Scheme | Waveform | RX / TX gain |
|---|---|---|---|
| `bpsk.json`      | BPSK   | sc   | 20 / 78 |
| `qpsk.json`      | QPSK   | sc   | 20 / 78 |
| `8psk.json`      | 8-PSK  | sc   | 16 / 86 |
| `dbpsk.json`     | DBPSK  | sc   | 20 / 78 |
| `dqpsk.json`     | DQPSK  | sc   | 20 / 78 |
| `8dpsk.json`     | 8-DPSK | sc   | 16 / 86 |
| `bpsk_ofdm.json` | BPSK   | ofdm | 22 / 80 |
| `qpsk_ofdm.json` | QPSK   | ofdm | 22 / 85 |

Being ARQ, **both** ends stop the instant every chunk is ACKed, and the TX prints
a per-chunk transmission summary. Override anything on the CLI (it wins) — e.g.
`--tx-gain 84`, `--rx-gain 18`, `--message-type random --num-bits 4096`. The gains
mirror `../COMMANDS.md`; treat that table as the source of truth if they ever drift.

### Override config values on the command line (CLI wins)

Keep a base config and change just what you need — any `--option value` overrides
the file (and applies to both ends of a pair). Great for sweeping modulation or
tweaking gains without editing JSON:

```bash
python3 run.py configs/qpsk_arq_pair.json --scheme 16-QAM --tx-gain 82 --rx-gain 18
python3 run.py configs/qpsk_arq_pair.json --scheme=8-PSK --waveform ofdm
```

`--dry-run` shows the final command(s) after overrides; unknown options are
rejected with a clear message; bool-switch flags (e.g. `--skip-rate-check`) take
no value.

### Separate terminals (watch each process)

`run.py config.json` launches both ends from one command. To run TX and RX in
their **own terminals** — one paired config, one side each:

```bash
# terminal 1  (receiver)
python3 run.py configs/qpsk_arq_pair.json --only rx
# terminal 2  (transmitter)
python3 run.py configs/qpsk_arq_pair.json --only tx
```

(Start the RX first.) You can also just use the single-role configs
(`rx_only.json` / `tx_only.json`), or the keyword API directly in each terminal:
`python3 -c "import sdr; sdr.sink_arq(rx_args='serial=...', scheme='QPSK').run()"`.

**Single run** — one object with a `role` (plus any options):

```json
{ "role": "tx", "tx_args": "serial=30CD424", "scheme": "8-PSK",
  "tx_gain": 86, "fec": true, "tx_reps": 20 }
```

**Paired run (both ends at once)** — `rx` and `tx` objects; `common` is merged into
both; optional `pair` tunes `run_pair()`:

```json
{
  "common": { "rx_freq": 915e6, "tx_freq": 915e6, "scheme": "QPSK", "fec": true },
  "rx": { "role": "sink_arq",   "rx_args": "serial=30CD3F7", "rx_gain": 20 },
  "tx": { "role": "source_arq", "tx_args": "serial=30CD424", "tx_gain": 78 },
  "pair": { "rx_head_start": 4, "rx_grace": 30 }
}
```

Keys starting with `_` (e.g. `"_comment"`) are ignored. `binary` at the top level
overrides the `sdr_system` path. Provided configs: per-scheme ARQ pairs
`bpsk` / `qpsk` / `8psk` / `dbpsk` / `dqpsk` / `8dpsk` / `bpsk_ofdm` / `qpsk_ofdm`
(tuned gains baked in — see the table above); plus `qpsk_arq_pair`, `ofdm_qpsk_pair`,
`random_pair`, `tx_only`, `rx_only`, `tx_tone` / `tx_tone_burst`, `rx_tone_monitor`.

### Test tone + monitor

`tx_tone.json` transmits a raw cosine carrier; `rx_tone_monitor.json` streams the
received samples and prints the dominant tone's **frequency and power once a
second** (a small spectrum analyzer — no decode pipeline). Run each in its own
terminal:

```bash
python3 run.py configs/rx_tone_monitor.json --rx-gain 30     # → [MONITOR] tone f = +199.6 kHz ...
python3 run.py configs/tx_tone.json --tx-gain 75             # 200 kHz cosine (change with --tone-freq)
```

The monitor works with a continuous **or** burst tone (`tx_tone_burst.json`), and
skips a small band around DC so the (cable) carrier leakage isn't mistaken for the tone.

## Channel sensing — `channel_sense.py`

Callable channel-occupancy detection (energy over a window), importable from any
script. Drives `sdr_system --role sense`, which integrates received power over a
`--sense-window` (ms) and prints `[SENSE] busy=.. power_db=..`.

```python
from channel_sense import sense_channel, calibrate_floor, should_transmit

thr = calibrate_floor(rx_args="serial=30CD3F7")     # idle floor + 6 dB margin
r = sense_channel(rx_args="serial=30CD3F7", threshold_db=thr)
print(r["busy"], r["power_db"])                     # True/False, e.g. -4.0

# NEXT STEP — p-persistent access: if idle, transmit with probability p
if should_transmit(p=0.5, rx_args="serial=30CD3F7", threshold_db=thr):
    ...  # transmit
```

**Persistent feed for a tight sense→decide loop** — `SenseStream` runs
`--sense-count 0` (stream forever) with **one** radio init and keeps the latest
reading fresh via a background thread, so each decision is instant:

```python
from channel_sense import SenseStream
with SenseStream(rx_args="serial=30CD3F7") as s:
    thr = s.calibrate()                       # idle floor + margin, from the feed
    while ...:
        go, r = s.should_transmit(p=0.5, threshold_db=thr)   # no re-init
        if go: ...transmit...
```

CLI: `python3 channel_sense.py --calibrate` (measure floor) /
`--count 10` (auto-calibrate then sense) /
`--p 0.5 --count 8` (p-persistent sense→decide loop over the persistent feed).
Validated on hardware: idle ~−12 dB → not busy → ~50% transmit at p=0.5; an active
tone ~−3 dB → busy → defer every time. One-shot calls re-init the radio (~1–2 s);
`SenseStream` / `--sense-count`/ `calibrate_floor` pay that once.

## Learning over the radio — `mnist_sgd_over_sdr.py`

Trains a tiny MLP on MNIST where **each iteration's top-k (5%) compressed gradient
is transmitted over the SDR link** — distributed SGD with the B210s as the network.
It calls `sdr.source_arq()` / `sdr.sink_arq()` **inside the training loop** (exactly
how `run.py` launches them), handing the gradient to the PHY through one reused
scratch file per side (overwritten each round — no per-iteration files). Transport
is QPSK single-carrier, large chunks (`--chunk 512`), FEC + ARQ (error-free).

```bash
# measure the real link throughput first (no training)
python3 mnist_sgd_over_sdr.py server --probe 32768 --rx-args serial=30CD3F7
python3 mnist_sgd_over_sdr.py worker --probe 32768 --tx-args serial=30CD424

# then train (start the SERVER first) — accuracy should climb, matched on both ends
python3 mnist_sgd_over_sdr.py server --rounds 30 --rx-args serial=30CD3F7
python3 mnist_sgd_over_sdr.py worker --rounds 30 --tx-args serial=30CD424
```

Both ends start from the same seed and apply the identical sparse update, so their
models track exactly — matching accuracy proves the gradient crossed the link
error-free. Relies on the PHY's `--payload-file` / `--out-file` byte-pipe and
`--bytes-length` (chunk size). Tune with `--hidden`, `--batch`, `--lr`, `--topk`,
`--scheme`, `--chunk`, `--timeout`, `--timer-interval`.

## Link BER diagnostic — `marl_phy.py ber`

Measure the **real per-burst bit-error-rate** end-to-end from Python. It runs a
warm Access Point (RX+ACK) and fires N copies of a known payload; the sink knows
the ground truth, so for **every** decoded burst (CRC pass or fail) it reports the
**pre-FEC** (raw channel) and **post-FEC** (payload) BER. This tells you whether a
CRC-failed frame is *nearly right* (a few residual bits) or *garbage* (FEC
overwhelmed) — a CRC failure is not automatically a garbage packet.

```bash
# one-box probe: spins up its own warm AP + fires N known packets, reports min/median/max
python3 marl_phy.py ber --attempts 20 --scheme DQPSK
#   [ber_probe] 20/20 fired bursts decoded  |  CRC pass 18/20
#   [ber_probe] pre-FEC  (channel) BER  min/median/max = 0.00 / 0.00 / 0.67 %
#   [ber_probe] post-FEC (payload) BER  min/median/max = 0.00 / 0.00 / 0.60 %
```

```python
from marl_phy import ber_probe
rows = ber_probe(n=20, scheme="DQPSK")     # [{pre_fec, post_fec, crc}, ...] per burst
```

Extra knobs: `--tx-args` / `--rx-args` (serials), `--tx-gain` (85) / `--rx-gain`
(40), `--scheme` (must match both ends). Under the hood this is the C++
`--ber-expected <known_payload.bin>` sink option; to run the two ends in separate
terminals (real two-box link) see [`../COMMANDS.md`](../COMMANDS.md) →
*Link BER diagnostic*.

**Reading the numbers** (both regimes measured on this rig):

| Window | pre-FEC | post-FEC | CRC-fail means |
|---|---|---|---|
| Good link | ≤0.7 % | ≤0.6 % | *nearly right* — a handful of bits off, message ~intact |
| Broken link | ~24 % | ~42 % | *garbage* — raw BER passed the rate-½ code's ~11 % limit, Viterbi then **amplifies** errors |

So **post-FEC > pre-FEC is the catastrophic-failure signature**: the channel BER
exceeded what the FEC can correct and decoding made things worse. See
`../SYSTEM_REFERENCE.md` §8.1.

### Long-term channel monitor — `ber_monitor.py`

The same probe **extended into time**: fires a known burst at a fixed cadence for
minutes/hours and records a *timestamped* per-burst trajectory (pre/post-FEC BER,
CRC, and whether the AP detected the burst at all), so you can watch the channel
drift — good/bad windows, detection rate, delivery rate — with no training running.

```bash
python3 ber_monitor.py --minutes 20                      # 20-min run, DQPSK, default gains
python3 ber_monitor.py --bursts 200 --scheme QPSK --tag qpsk_run
```

Writes `<tag>.csv` (full time-series) and `<tag>.png` (BER-over-time + rolling
delivery rate) into `../applications/MARL_RA_Union/results/`, and prints a link
summary: **detection rate** (burst seen at all — a link-budget/SNR gauge) and
**delivery rate** (CRC pass — usable payload). A low detection rate means the link
is below the acquisition threshold (radios too far / gain too low), which is a
*different* failure from a detected-but-corrupt burst (that one is a decode/SNR
margin issue). `Ctrl-C` saves the partial run.

**Throughput (measured, 32 KB over the air, marginal free-running link).** The two
big levers are the **sink poll interval** (`--timer-interval`, was 1000 ms → the ACK
latency) and the **ACK timeout** (`--timeout`):

| `timer-interval` / `timeout` | rate | note |
|---|---|---|
| 1000 / 3000 ms (old PHY default) | 0.21 KB/s | ~1.3 s ACK latency |
| 20 / 800 ms | 0.88 KB/s | fast poll → ~0.17 s ACK |
| **20 / 400 ms (default here)** | **1.38 KB/s** | safe above the ~170 ms ACK |

So a top-k MLP gradient (~31.8 KB) is ~22 s/round. The remaining cost is ~30%
retransmits from the free-running-clock CFO — a **shared 10 MHz reference** removes
them (~3 KB/s), after which **bigger `--chunk`** finally helps (under CFO, long bursts
drift past the decision boundary and fail, so 512 B is kept here).
