# Python wrapper for the SDR PHY

`sdr.py` is a thin Python wrapper around the C++ `sdr_system` binary. It exposes
**every** command-line option as a keyword argument, builds the command line, and
launches it as a subprocess.

**`sdr.py` is auto-generated** from `sdr_system --help` by `tools/gen_python_api.py`,
which runs as a CMake post-build step — so whenever you rebuild the C++, the Python
wrapper picks up any new/changed options automatically. **Don't edit `sdr.py` by hand.**

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
python3 run.py configs/qpsk_arq_pair.json              # run it
python3 run.py configs/qpsk_arq_pair.json --dry-run    # just print the command(s)
```

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
overrides the `sdr_system` path. Provided configs: `qpsk_arq_pair`, `ofdm_qpsk_pair`,
`random_pair`, `tx_only`, `rx_only`, `tx_tone`.
