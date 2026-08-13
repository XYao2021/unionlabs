# PHY Layer — Beginner's Guide

This guide explains **what the PHY layer is, every way to run it, and the main commands,
options, and functions** — enough for a newcomer to use the codes without reading the source.

> **PHY = the "modem".** It turns your data (bytes / a float32 vector) into radio waves and
> back: framing → error‑correction (FEC) → modulation → radio, and the reverse on receive.
> You bring an *algorithm*; the PHY moves its data over the air.

---

## 0. The map — where things live

The pieces you touch most:

- **`algorithms/`** — your code (one folder per algorithm); **`run.sh`** runs it.
- **`phy/build/sdr_system`** — the C++ radio engine (the modem); **`radio.sh`** runs raw TX/RX.
- **`phy/python/`** — the Python you call: `run_algo.py`/`phy_link.py` (uniform API),
  `sdr.py`/`run.py`/`configs/` (direct radio), `channel_sense.py`/`freq_scan.py` (RF utils).
- **`phy/bindings/pyphy`** — the DSP blocks as numpy functions.
- **`applications/`** — worked apps (MARL, FL, CLIP, jammer).

**Full repo layout → [`STRUCTURE.md`](../STRUCTURE.md); file-by-file index → [`MANIFEST.md`](../MANIFEST.md).**

**Three ways to use the PHY, easiest first:**
1. **`run.sh`** — run an algorithm over the PHY with one command (§1–2).
2. **`sdr.py` / `sdr_system`** — drive the radio directly by role (§3).
3. **`pyphy` blocks** — compose the DSP yourself in Python (§4).

---

## 1. Quick start (one command)

```bash
./run.sh                          # runs the built-in "echo" test, no radio
./run.sh list                     # list the algorithms in algorithms/
./run.sh --algo marl              # run an algorithm (loopback, lossless channel)
./run.sh --help                   # this help + every option
```

Everything you don't specify takes a **default** (`algo=echo role=loopback channel=ideal steps=5`).
`run.sh` prints the exact command it runs (the `>>` line) so you can learn the underlying call.

---

## 2. Running an algorithm over the PHY (the uniform API)

Your algorithm only says **what to transmit** and **what to receive** — no radio code. Drop it in
`algorithms/<name>/app.py` (copy `algorithms/_template/`), then run it by name. **Step-by-step
authoring guide: [`HOW_TO_ADD_ALGORITHM.md`](../HOW_TO_ADD_ALGORITHM.md).**

```bash
# radio-free, lossless (check the logic):
./run.sh --algo <name> --role loopback

# radio-free THROUGH THE REAL MODEM + noise (see how SNR affects it):
./run.sh --algo <name> --role loopback --channel pyphy --snr-db 6

# over the radio (two hosts) — start the rx FIRST:
./run.sh --algo <name> --role rx --rx-args addr=192.168.20.2
./run.sh --algo <name> --role tx --tx-args serial=30CD424 --ack-host <AP_IP>
```

### `run.sh` / `run_algo.py` options

| Option | Default | What it does |
|---|---|---|
| `--algo <name>` | `echo` | which folder under `algorithms/` to run |
| `--role loopback\|tx\|rx` | `loopback` | `loopback` = both ends in one process (no radio); `tx`/`rx` = the two radio hosts |
| `--channel ideal\|pyphy` | `ideal` | loopback channel: `ideal` = lossless; `pyphy` = real modem + AWGN |
| `--steps <n>` | `5` | how many round-trips to run |
| `--scheme <NAME>` | `QPSK` | modulation for the pyphy/radio path (QPSK, BPSK, DQPSK, 16-QAM, …) |
| `--fec conv\|ldpc\|turbo\|""` | `turbo` | error-correcting code for the pyphy channel |
| `--snr-db <dB>` | `8` | signal-to-noise ratio for the pyphy channel |
| `--tx-args / --rx-args` | `""` | which radio: `serial=30CD424` (B210) or `addr=192.168.20.2` (N210) |
| `--ack-host / --net-host` | `127.0.0.1` | the peer host's IP (radio roles) |

### The algorithm contract (all you write)

```python
# algorithms/<name>/app.py — no framework import, no radio code
import numpy as np
class MyAlgo:
    spec = ("float32", (8,))                 # output type + shape (optional)
    def __init__(self, role): self.role = role
    def transmit(self):     ...              # -> array | None   WHAT TO TRANSMIT
    def receive(self, msg): ...              # WHAT TO RECEIVE (a numpy array)
    # def on_result(self, ack): ...          # optional: delivered/collision reward
def make(role): return MyAlgo(role)          # the framework builds one per node
```

See `algorithms/README.md` for the full contract and the worked examples (`fl`, `clip_semcom`,
`marl`, `plain_echo`).

---

## 3. Driving the radio directly (`sdr.py` / `sdr_system`)

When you want raw control of the modem (no algorithm layer), use the roles.

> **Shortcut:** `./radio.sh tx` or `./radio.sh rx` runs a raw TX/RX on a USRP with the right
> per-device defaults — **B210 is the default**; add `--device n210` or `--device x310`. E.g.
> `./radio.sh rx --device n210 --freq 915e6`. Add `--dry-run` to just print the command.

### From Python (`phy/python/sdr.py`)
```python
from sdr import tx, rx, both, source_arq, sink_arq, run_pair, options
options()                                             # print every option + default
tx(tx_args="serial=30CD424", scheme="QPSK", tx_gain=78, fec=True).run()   # transmit only
rx(rx_args="serial=30CD3F7", scheme="QPSK", fec=True).run()               # receive only
# reliable stop-and-wait ARQ across two radios (RX first):
run_pair(sink_arq(rx_args="serial=30CD3F7", scheme="QPSK", fec=True),
         source_arq(tx_args="serial=30CD424", scheme="QPSK", fec=True))
print(tx(scheme="QPSK").command())                    # just print the command (don't run)
```

### The roles
| Role | Meaning |
|---|---|
| `tx` / `rx` | transmit only / receive only (one radio each) |
| `both` | transmit **and** receive in one process (single-box loopback) |
| `source_arq` / `sink_arq` | the two ends of reliable stop-and-wait ARQ (with ACK) |
| `sense` | listen and report channel power (used by `channel_sense.py`) |

### The options you'll actually use (curated — full list in `../PARAMETERS.md`)

| Option | Example | Meaning |
|---|---|---|
| `--role` | `source_arq` | what this process does (table above) |
| `--scheme` | `QPSK` | modulation: `BPSK QPSK 8-PSK 16/64-QAM`, differential `DBPSK DQPSK 8-DPSK PI4-QPSK` |
| `--waveform` | `sc` | `sc` (single-carrier) or `ofdm` (many subcarriers, tracks frequency drift) |
| `--fec` | `true` | turn error-correction on |
| `--fec-type` | `turbo` | `conv` (fast), `ldpc`, or `turbo` (strongest) |
| `--fec_soft` | `true` | soft-decision decoding (better; pair with ldpc/turbo) |
| `--tx-args` / `--rx-args` | `serial=30CD424` / `addr=192.168.20.2` | pick the radio (B210 serial / N210 addr) |
| `--tx-freq` / `--rx-freq` | `915e6` | carrier frequency (Hz) |
| `--tx-rate` / `--rx-rate` | `2e6` | sample rate (Hz); N210 needs `2e6` |
| `--symbol-rate` | `1e6` | symbol rate; N210 pairs `2e6/1e6` |
| `--tx-gain` / `--rx-gain` | `78` / `30` | amplifier gain (dB) |
| `--tx-subdev` / `--rx-subdev` | `A:A` / `A:0` | radio front-end (B210 `A:A`, N210 `A:0`) |
| `--ack-transport` | `tcp` | send the ARQ ACK over `tcp` (so an RX-only radio never transmits) |
| `--ack-host` / `--ack-port` | `192.168.1.50` / `5599` | where the ACK goes |
| `--bytes-length` | `125` | payload size per frame (bytes) |
| `--max-attempts` | `1` | retransmit limit; `1` = single-shot (used by MARL), `0`/high = keep trying |
| `--viz` / `--viz-dir` | `true` / `phy_outputs` | capture TX/RX signals + auto-plot to `phy_outputs/<scheme>/` |
| `--config <file>` | `phy.cfg` | load all options from a file (CLI still overrides) |

Two rules that save hours: **start the receiver before the transmitter**, and on the
free-running radios use **`DQPSK`** (single-carrier) or **`OFDM`** — plain coherent `QPSK`
delivers poorly without a shared clock.

### From a JSON file (`phy/python/run.py`)
```bash
python3 phy/python/run.py --list                    # ready-made configs
python3 phy/python/run.py configs/qpsk_arq_pair.json --dry-run   # print, don't run
```

---

## 4. The building blocks (`pyphy`)

Every DSP stage is also a plain Python function, so you can compose the chain yourself
(GNU-Radio style). Build it with `phy/bindings/build.sh`; import with
`PYTHONPATH=phy/bindings python3` (add `arch -x86_64` on macOS).

| Group | Functions |
|---|---|
| Framing | `frame`, `unframe` |
| FEC | `fec_encode`, `fec_decode`, `fec_decode_soft`, `fec_encoded_len` (`conv\|ldpc\|turbo`, `k`) |
| Modulation | `modulate`, `demodulate`, `soft_llr` |
| Single-carrier | `rrc_tx`, `rrc_rx` (pulse shape / matched filter) |
| Sync | `preamble`, `acq`, `cfo_correct`, `phase_correct` |
| OFDM | `ofdm_mod`, `ofdm_demod`, `ofdm_data_per_sym` |
| Radio (UHD build) | `Radio(role, args, freq, rate, symbol_rate, gain, subdev, ant)` · `.transmit()` · `.capture()` |

```python
import numpy as np, pyphy
bits = (np.random.rand(2000) > 0.5).astype(np.uint8)
syms = pyphy.modulate(bits, "QPSK")            # bits -> symbols
syms = syms * 0.6                              # <- your own op between stages
wave = pyphy.rrc_tx(syms, sps=2, beta=0.25)    # pulse-shape to a waveform
# ... channel/radio ...
back = pyphy.demodulate(pyphy.rrc_rx(wave, sps=2, beta=0.25)[::2], "QPSK")
```
Worked flowgraph: `phy/python/phy_flow_example.py`.

---

## 5. Function cheat-sheet (the Python API)

**Uniform API — `phy/python/phy_link.py`**
- `SdrApp` — base app: `produce()` / `consume(msg)` / `on_result(ack)` (advanced path)
- `adapt(obj, role)` — wrap ANY object with `transmit()`/`receive()` into an `SdrApp`
- `PayloadSpec(dtype, shape)` — declare an output's type + shape
- `Codec.pack(arr)` / `Codec.unpack(buf)` — numpy array ⇄ self-describing bytes
- `IdealChannel` / `PyphyChannel(scheme, fec, snr_db)` — radio-free channels
- `run_loopback(tx, rx, channel, steps)` — radio-free round-trip driver
- `RadioRoundTrip(role, …).step(app)` — the two-host radio round-trip

**Runner — `phy/python/run_algo.py`**
- `load_app_factory(name)` — find/adapt the algorithm in `algorithms/<name>/app.py`

**Radio wrapper — `phy/python/sdr.py`**
- `SDR(**opts)` — one invocation; `.command()` (print), `.run()` (blocking), `.popen()` (background), `.set(**opts)`
- helpers `tx / rx / both / source_arq / sink_arq / run_pair / options`

**Spectrum — `phy/python/channel_sense.py`, `freq_scan.py`**
- `sense_channel(...)` — measure power at one frequency
- `python3 phy/python/freq_scan.py --start 902 --stop 928 --rx-args addr=192.168.20.2` — find a quiet carrier

---

## 6. Go deeper

- **`../PARAMETERS.md`** — every option (auto-generated, always current).
- **`../SYSTEM_REFERENCE.pdf`** — the full engine reference (the math + every algorithm).
- **`applications/EXPERIMENT_GUIDE.pdf`** — step-by-step commands to run the example apps.
- **`algorithms/README.md`** — how to write/upload an algorithm.
- **`docs/framework_marl.pdf`** — one-page picture of the whole framework.

---

## 7. Troubleshooting (beginner gotchas)

| Symptom | Fix |
|---|---|
| `pyphy` import / `Python.h` / arch error (macOS) | run via `./run.sh` (it sets `PYTHONPATH` + `arch -x86_64`), or build with `phy/bindings/build.sh` |
| `Exec format error` on `sdr_system` | rebuild on that machine: `cd phy && cmake -S . -B build && cmake --build build` |
| receiver gets nothing / no ACK | start the **receiver first**; keep both radios on; use `DQPSK` or `OFDM` |
| startup "rate check" fails | N210 needs `--rx-rate 2e6 --symbol-rate 1e6`; set `--tx-rate = --rx-rate` |
| a stuck receiver won't stop | press Ctrl-C again; last resort `Ctrl-\` or `kill -9` |
| coherent `QPSK` ~17% delivered | free-running clocks — use `DQPSK` (single-carrier) or `OFDM` |
