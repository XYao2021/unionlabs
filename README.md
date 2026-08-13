# USRP SDR Platform

A software-defined-radio **PHY** (USRP / UHD, C++ `sdr_system`) with a **uniform API** for
running algorithms over the air, a thin Python wrapper for driving the radio directly, and a
set of worked **applications**. You bring an algorithm; the PHY moves its data over the radio.

## Quick start

```bash
./run.sh                       # run an algorithm over the PHY (defaults: echo, no radio)
./run.sh --algo marl           # pick any algorithm from algorithms/   (./run.sh list)
./radio.sh rx                  # raw receive on a USRP  (B210 default; --device n210|x310)
./radio.sh tx                  # raw transmit
```

- **Add your own algorithm:** [`HOW_TO_ADD_ALGORITHM.md`](HOW_TO_ADD_ALGORITHM.md) — where to put it,
  how to write `app.py`, how to link your existing code, how to run it by name.
- **Learn the PHY:** [`drivers/usrp_uhd/GUIDE.md`](drivers/usrp_uhd/GUIDE.md) — beginner's guide to every way to run it.
- **Every option:** [`PARAMETERS.md`](PARAMETERS.md) — auto-generated, always current.
- **Full layout:** [`STRUCTURE.md`](STRUCTURE.md) · **file index:** [`MANIFEST.md`](MANIFEST.md).

## Layout (see `STRUCTURE.md` for detail)

| Path | What |
|---|---|
| `run.sh` / `radio.sh` | run an **algorithm** over the PHY / raw **TX·RX** on a USRP |
| `algorithms/` | **your** algorithms (one folder each) |
| `union/` | the **abstraction / middleware** — one contract for all testbeds + PHYs (`phy_link.py`, `run_algo.py`, `driver.py`) |
| `drivers/` | the **driver layer** — one per PHY × testbed: `usrp_uhd/` (C++ engine + `pyphy`), `sim/`, `lora_arduino/` (planned) |
| `applications/` | worked apps: MARL random access, federated learning, CLIP semantic comm, STC-AirComp (AJOU), jammer |
| `docs/` `results/` `deploy/` | diagrams & slides / run outputs / Docker + install |

## Three ways to use the PHY

1. **Uniform algorithm API** — the easiest path. Your algorithm only says *what to transmit /
   what to receive*; `./run.sh --algo <name>` runs it. See `HOW_TO_ADD_ALGORITHM.md` and `drivers/usrp_uhd/GUIDE.md §2`.
2. **Direct radio** — drive the modem by role via `sdr.py` or `./radio.sh` (see below + `drivers/usrp_uhd/GUIDE.md §3`).
3. **`pyphy` blocks** — compose the DSP yourself (modulate/FEC/sync/OFDM as numpy functions).
   See `drivers/usrp_uhd/GUIDE.md §4`.

---

## Driving the radio directly

For raw TX/RX and the **`sdr.py` Python API** — roles, options, JSON configs, channel sensing —
see **[`drivers/usrp_uhd/GUIDE.md §3`](drivers/usrp_uhd/GUIDE.md)**. The one-line shortcut is `./radio.sh tx` / `./radio.sh rx`
(B210 default; `--device n210|x310`).

**Where to find commands & options (each doc has one job):**

| Doc | Contains |
|---|---|
| [`PARAMETERS.md`](PARAMETERS.md) | every controllable option (auto-generated) |
| `COMMANDS.md` | ready-to-run TX/RX command pairs, **per scheme** (tuned gains) |
| `USRP_CARRIER_MODULATION.txt` | commands **per device** (B210 / N210 / X310) across carriers |
| `COMMANDS_FEC_TESTS.txt` | LDPC / turbo FEC test commands |
| [`MANIFEST.md`](MANIFEST.md) | the **CLI ↔ Python (`sdr.py`)** command mapping (+ file index) |
| `SYSTEM_REFERENCE.pdf` | the deep engine reference (the math + every algorithm) |
| `HARDWARE.md` | hardware setup & radio inventory |

## Applications

Worked examples that build on the PHY live in `applications/` (MARL random access, federated
learning, CLIP semantic communication, a jammer). Each has its own `README.md` / `INTEGRATION.md`,
and step-by-step run commands are in [`applications/EXPERIMENT_GUIDE.pdf`](applications/EXPERIMENT_GUIDE.pdf).
The same apps are also available as uploadable algorithms under `algorithms/`.

## Install

`deploy/initialization.sh` installs the toolchain (`--build` also compiles `drivers/usrp_uhd/build/sdr_system`).
