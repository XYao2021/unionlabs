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
`--bytes-length` (large chunks). Tune with `--hidden`, `--batch`, `--lr`, `--topk`,
`--scheme`, `--chunk`.
