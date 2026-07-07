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
