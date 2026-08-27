# UnionLabs SDR Platform — Beginner's Guide

A single, self-contained guide: what the platform is, how to run it, how to add your own
algorithm, and how to drive the radio directly. No prior knowledge of the codebase needed.

> **PHY = the "modem".** It turns your data (bytes / a float32 vector) into radio waves and back:
> framing → error-correction (FEC) → modulation → radio, and the reverse on receive. You bring an
> *algorithm*; the PHY moves its data over the air.

> **The promise.** You write the algorithm once. The same file runs with no radio, over a USRP
> software-defined radio, or over a LoRa module — you change a flag, not your code.

---

## 1. Quick start

**Install first** — Python 3.8 or newer, one command, no radio and no C++ build:

```bash
pip install -r requirements.txt
./run.sh selftest            # confirm the install works (every experiment x every radio-free PHY)
```

If `selftest` prints `all N checks passed`, everything below will work. If only the `usrp`
checks fail, the compiled `pyphy` extension does not match your Python — build it with
`drivers/usrp/bindings/build.sh`, or just use `--channel ideal` / `--channel lora`, which
need nothing.

```bash
./run.sh                     # run an algorithm over the PHY (defaults: echo, no radio)
./run.sh list                # list the algorithms available
./run.sh --algo marl         # run a specific algorithm
./run.sh --help              # help + every option
./radio.sh rx                # raw receive on a USRP (B210 default; --device n210|x310)
./radio.sh tx                # raw transmit
```

Anything you don't specify takes a **default** (`algo=echo role=loopback channel=ideal steps=5`).
`run.sh` prints the exact command it runs (the `>>` line) so you learn the underlying call.

**Before your first run on real radios**

```bash
uhd_find_devices                              # are the radios visible at all?
uhd_find_devices --args addr=192.168.20.2     # a specific networked one

# measure this testbed once; run.sh and radio.sh then use what it found
./prepare.sh --device x310 --addr 192.168.40.2 --band vert2450-5g
```

`prepare.sh` sweeps the band, picks a clean carrier, and derives the detector
thresholds, then publishes a profile that `run.sh` and `radio.sh` read automatically —
you must say which antenna is on the radio (`--band`), because that cannot be probed —
so you do not tune `--det-mult` and `--sync-threshold` by hand. Anything you type
still wins over it.

**Six rules that save hours**

- **Start the receiver first**, transmitter second, and leave both running.
- **N210 rates are exact:** `--rx-rate 2e6 --symbol-rate 1e6` (1.6e6 does not snap onto its 100 MHz clock).
- **X310 rates** must be `master_clock_rate / integer`; the modem now refuses a rate it cannot hit rather than transmitting a payload that will not decode.
- **Two-host ACK:** each side's `--ack-host` is the *other* host's IP.
- **Free-running LOs:** use **DQPSK** (single-carrier) or **OFDM** (coherent QPSK, pilots track the offset). Coherent single-carrier without a shared 10 MHz reference delivers poorly.
- **Receiver won't stop on Ctrl-C?** Press it again; the handler escalates.

## 2. Where things live

- **`algorithms/`** — **everything you run**, one folder per experiment, each with an `app.py`.
  Your own code and the worked examples (FL, decentralized learning, MARL, CLIP semantic comm,
  STC-AirComp, jammer) all live here — one place to look.
- **`union/`** — the middleware you code against: `run_algo.py` + `phy_link.py` (the uniform API)
  and `driver.py` (the contract every PHY implements).
- **`drivers/`** — the physical layers, one folder each:
  - `usrp/build/sdr_system` — the C++ radio engine (the modem); `radio.sh` runs raw TX/RX.
  - `usrp/python/` — `sdr.py` / `run.py` / `configs/` (direct radio), `channel_sense.py` (RF utils).
  - `usrp/bindings/pyphy` — the DSP blocks as numpy functions.
  - `lora/` — the SX1276 LoRa PHY (firmware + the Python driver).
- **`docs/`** — every guide, reference and PDF, including this one.

**Three ways to use the PHY, easiest first:**

1. **Run an algorithm** with `run.sh` (§3).
2. **Add your own algorithm** (§4).
3. **Drive the radio directly** with `sdr.py` / `radio.sh` (§5), or compose the DSP blocks (§6).

---

## 3. Running an algorithm over the PHY

Your algorithm only says **what to transmit** and **what to receive** — no radio code. Drop it in
`algorithms/<name>/app.py` (copy `algorithms/_template/`), then run it by name.

### 3.1 The three independent choices

Every run answers three separate questions. Keeping them apart is the single most useful thing
to understand about this platform:

```
./run.sh  --algo fl        --channel lora        --role loopback
          └─ WHAT ────┘    └─ WHICH PHY ──┘      └─ WHICH PART am I ─┘
```

1. **`--algo`** — what you are running. Any folder in `algorithms/` (`./run.sh list`).
2. **`--channel`** — which physical layer carries the bytes. Your algorithm never knows.
3. **`--role`** — whether this process is the *whole network* or *one node of it*.

A fourth, `--<phy>-backend`, says how the PHY is attached. **Every default backend needs no
hardware**, so you develop the whole experiment on a laptop and change one flag to go on air.

### 3.2 Choosing a PHY (`--channel`)

| `--channel` | What it is | Needs hardware? | Use it to |
|---|---|---|---|
| `ideal` *(default)* | lossless, in-process | no | check your logic first — if it fails here, it is your algorithm |
| `usrp` | the real C++ modem (OFDM/single-carrier, LDPC/turbo FEC, sync, CFO) | no, with `--usrp-backend pyphy` | see how modulation, FEC and SNR affect your algorithm |
| `lora` | the SX1276 LoRa PHY (255-byte MTU, fragmentation + ARQ) | no, with `--lora-backend sim` | see what your payload costs in airtime on a long-range, low-rate link |

```bash
./run.sh --algo fl                                    # ideal — is my logic right?
./run.sh --algo fl --channel usrp --sim-snr-db 6          # how noisy can the link get?
./run.sh --algo fl --channel lora --lora-sf 9         # what does this cost on LoRa?
```

Older spellings still work: `--channel sim` = `ideal`, `--channel pyphy` = `usrp`.

### 3.3 Choosing a role (`--role`)

**Group roles build the whole network in one process** — this is how you develop:

| `--role` | Shape | Extra flags |
|---|---|---|
| `loopback` *(default)* | two ends, one round-trip each step | — |
| `chain` | initiator → relay(s) → responder, every hop over the PHY | `--relays 1` |
| `gossip` | N peers exchanging over a graph, no server | `--agents 6` `--topology` |
| `multi` | N agents contending for one access point (collisions are real) | `--agents 4` |
| `aircomp` | N sensors transmit at once; the air sums them | `--agents 8` |

**Node roles run ONE node in this process** — this is how you deploy, one terminal or one
computer per node:

| `--role` | This process is | Extra flags |
|---|---|---|
| `tx` | the initiator (transmits, then listens) | `--radio`, peer host |
| `rx` | the responder (listens, then answers) | `--radio` |
| `relay` | a middle node: receives from upstream, re-transmits downstream | `--down-host`, `--down-port` |
| `peer` | one node of a decentralized network — both TX and RX, at different steps | `--node K` `--agents N` |

```bash
# develop: the whole 6-peer network in one process
./run.sh --algo dl --role gossip --agents 6 --topology ring

# deploy: one terminal per node (--node K implies --role peer)
./run.sh --algo dl --node 0 --agents 3
./run.sh --algo dl --node 1 --agents 3
./run.sh --algo dl --node 2 --agents 3
```

> **An algorithm can name its own roles.** `fl` calls them `client`/`server`, `dl` calls them
> `initiator`/`responder`/`peer`. `./run.sh list` prints each algorithm's roles, and you type
> the algorithm's own name: `./run.sh --algo fl --role server`. `tx`/`rx` always work too.

### 3.4 Every `run.sh` option

**Core**

| Option | Default | What it does |
|---|---|---|
| `--algo <name>` | `echo` | which folder under `algorithms/` to run |
| `--channel ideal\|usrp\|lora` | `ideal` | which PHY carries the payloads |
| `--role <role>` | `loopback` | see §3.3; or any role the algorithm declares |
| `--steps <n>` | `5` | how many rounds/round-trips to run |
| `--agents <n>` | `4` | number of nodes for `gossip` / `multi` / `aircomp` |
| `--node <k>` | — | run ONE node of a decentralized network (implies `--role peer`) |
| `--relays <n>` | `1` | relay nodes in the middle of a `chain` |
| `--topology <t>` | `ring` | `gossip` graph: `ring`, `full`, or an edge list `0-1,1-2,2-0` |
| `--sim-snr-db <dB>` | `8` | **simulation only** — the SNR the simulated channels model. On real radios SNR is *measured*, not set (see below) |

> **Simulated SNR vs a real link.** `--snr-db` is how noisy you *ask the simulator to be*:
> `--usrp-backend pyphy` adds AWGN at that Es/N0, and `--lora-backend sim` tests it against the
> spreading factor's demodulator floor. On real hardware you cannot set SNR at all — it is the
> outcome of gain, distance, antennas and interference. There you drive the link with
> `--tx-gain`/`--rx-gain` (USRP) or `--lora-power`/`--lora-sf` (LoRa), and the receiver *reports*
> the SNR it measured. Passing `--snr-db` to a real-radio run prints a NOTE saying it did nothing.

**Shared radio options** (any PHY)

| Option | Default | What it does |
|---|---|---|
| `--freq <MHz>` | `915` | centre frequency. Outside 902–928 MHz you get a US-band warning |
| `--arq <scheme>` | `stop-and-wait` | retransmission policy (only one implemented so far) |
| `--max-attempts <n>` | per PHY | give up on a chunk after this many un-ACKed sends (USRP 50, LoRa 8) |

**USRP options** (`--channel usrp`) — we assemble this PHY, so every part is yours to choose

| Option | Default | What it does |
|---|---|---|
| `--usrp-backend pyphy\|radio` | `pyphy` | in-process modem, or real radios (needs two hosts) |
| `--modulation <NAME>` | `QPSK` | BPSK, QPSK, 8-PSK, 16-QAM, DBPSK, DQPSK (`--scheme` is the same flag) |
| `--fec conv\|ldpc\|turbo\|""` | `turbo` | error-correcting code; `""` turns coding off |
| `--samp-rate` / `--symbol-rate` | `2e6` / `1e6` | sample and symbol rate in Hz |
| `--tx-gain` / `--rx-gain` | `70` / `30` | transmit and receive gain in dB |
| `--ack-transport tcp\|rf` | `tcp` | where the ACK travels: a socket, or a second RF path |
| `--ack-timeout <ms>` | `3000` | how long the sender waits for an ACK |
| `--radio serial=…\|addr=…` | — | which USRP this process owns (B210 by serial, N210/X310 by address) |
| `--tx-args` / `--rx-args` | `""` | as above, when one node has two radios |

**LoRa options** (`--channel lora`) — the chip embeds modulation and CRC, so these are its knobs

| Option | Default | What it does |
|---|---|---|
| `--lora-backend sim\|serial\|spi` | `sim` | no hardware / Arduino on USB / Pi with the radio on SPI |
| `--lora-sf <7..12>` | `9` | spreading factor: higher reaches further, costs exponentially more airtime |
| `--lora-cr <5..8>` | `5` | coding rate denominator (4/5 … 4/8) |
| `--lora-bw 125000\|250000\|500000` | `125000` | bandwidth in Hz; doubling it halves airtime and costs ~3 dB sensitivity |
| `--lora-power <dBm>` | `14` | transmit power |
| `--lora-port <dev>` | — | serial backend: e.g. `/dev/ttyUSB0` |
| `--lora-verbose` | off | print fragments, retransmissions and airtime for every message |

**Multi-process options** (node roles)

| Option | Default | What it does |
|---|---|---|
| `--peers <h1,h2,…>` | all localhost | one host per node id, when nodes are on different computers |
| `--peer-port <n>` | `5800` | base TCP port; node k listens on `peer-port + k` |
| `--peer-link tcp\|wireless\|lora` | follows `--channel` | how decentralized peers exchange |
| `--ack-host` / `--net-host` | `127.0.0.1` | the peer host's IP (USRP `tx`/`rx` roles) |

> Passing a knob for a PHY you did not select prints a NOTE rather than being silently ignored —
> `--lora-sf` with `--channel usrp` tells you so.

### 3.5 Reading the output

```
[run_algo] loaded algorithms/fl via make(role) binding
[run_algo] role 'client' -> PHY end 'tx'
  step 0: req_ber=0.0000 rep_ber=0.0000 delivered=True
[run_algo] loopback done: 12/12 round-trips delivered over channel=ideal
[run_algo] lora PHY: {'msgs': 4, 'frags': 416, 'retx': 0, 'lost': 0,
                      'airtime_s': 165.09, 'sf': 7, 'arq': 'stop-and-wait'}
```

- **`delivered`** — the round-trip completed and the CRC was clean.
- **`req_ber` / `rep_ber`** — bit error rate each way (the USRP modem reports real errors; LoRa
  reports 0 because the chip only surfaces CRC-valid packets, so a bad frame is a *loss*).
- **The PHY line** — what the run actually cost. For LoRa this is the headline: `frags` is how
  many 247-byte pieces your payload needed, `retx` the retransmissions, and `airtime_s` the real
  Semtech airtime. A 25 kB model over two rounds costs **165 s at SF7 and 3727 s at SF12** —
  the same experiment, 22× the time on air.

To write your own `app.py`, see **§4**.

---

## 4. Add your own algorithm

This has a guide of its own, so it stays in one place rather than drifting between two:

> **→ [`HOW_TO_ADD_ALGORITHM.md`](../HOW_TO_ADD_ALGORITHM.md)** (repo root, also as
> [`HOW_TO_ADD_ALGORITHM.md`](../HOW_TO_ADD_ALGORITHM.md))

It covers, in order:

1. **Where to put it** — `algorithms/<name>/app.py`, and `--algo <name>` matches the folder.
2. **One file or many** — many: `app.py` is only the bridge, and your own modules, sub-packages
   and data sit beside it.
3. **What `app.py` must provide** — `make(role)` returning an object with `transmit()` /
   `receive(msg)`, plus optional `spec` and `on_result(ack)`.
4. **Two ways to write it** — inline (copy `algorithms/_template/`), or a ~10-line binding onto
   your existing, untouched code (copy `algorithms/plain_echo/`).
5. **Roles** — the four node types (`tx`, `rx`, `relay`, `peer`), naming your own with `ROLES`,
   and learning which node you are with `make(role, index, total)`.
6. **Running it** — the same file over every PHY, and as a multi-node network.

The shortest possible version:

```python
# algorithms/my_algo/app.py
import numpy as np

class MyAlgo:
    spec = ("float32", (8,))
    def __init__(self, role): self.role = role
    def transmit(self):      return np.ones(8, np.float32)   # what to transmit (None = done)
    def receive(self, msg):  print("got", msg)               # what to receive

def make(role): return MyAlgo(role)
```

```bash
./run.sh --algo my_algo
```

## 5. Driving the radio directly (`sdr.py` / `radio.sh`)

When you want raw control of the modem (no algorithm layer), use the roles.

> **Shortcut:** `./radio.sh tx` or `./radio.sh rx` runs a raw TX/RX on a USRP with the right
> per-device defaults — **B210 is the default**; add `--device n210` or `--device x310`. E.g.
> `./radio.sh rx --device n210 --freq 915e6`. Add `--dry-run` to just print the command.

### From Python (`drivers/usrp/python/sdr.py`)

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

### The options you'll actually use (curated — the full list is `PARAMETERS.md`, or `sdr_system --help`)

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
| `--max-attempts` | `1` | retransmit limit; `1` = single-shot, `0`/high = keep trying |
| `--viz` / `--viz-dir` | `true` / `phy_outputs` | capture TX/RX signals + auto-plot to `phy_outputs/<scheme>/` |
| `--config <file>` | `phy.cfg` | load all options from a file (CLI still overrides) |

Two rules that save hours: **start the receiver before the transmitter**, and on free-running
radios use **`DQPSK`** (single-carrier) or **`OFDM`** — plain coherent `QPSK` delivers poorly
without a shared clock.

### From a JSON file (`drivers/usrp/python/run.py`)

```bash
python3 drivers/usrp/python/run.py --list                                  # ready-made configs
python3 drivers/usrp/python/run.py configs/qpsk_arq_pair.json --dry-run    # print, don't run
```

---

## 6. The building blocks (`pyphy`)

Every DSP stage is also a plain Python function, so you can compose the chain yourself
(GNU-Radio style). Build it with `drivers/usrp/bindings/build.sh`; import with
`PYTHONPATH=drivers/usrp/bindings python3` (add `arch -x86_64` on macOS).

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

Worked flowgraph: `drivers/usrp/python/phy_flow_example.py`.

---

## 7. Function cheat-sheet (the Python API)

**Uniform API — `union/phy_link.py`**

- `adapt(obj, role)` — wrap ANY object with `transmit()`/`receive()` into the internal app.
- `PayloadSpec(dtype, shape)` — declare an output's type + shape.
- `Codec.pack(arr)` / `Codec.unpack(buf)` — numpy array ⇄ self-describing bytes.
- `IdealChannel` / `PyphyChannel(scheme, fec, snr_db)` — radio-free channels.
- `run_loopback(tx, rx, channel, steps)` — radio-free round-trip driver.
- `RadioRoundTrip(role, …).step(app)` — the two-host radio round-trip.

**Radio wrapper — `drivers/usrp/python/sdr.py`**

- `SDR(**opts)` — one invocation; `.command()` (print), `.run()` (blocking), `.popen()` (background), `.set(**opts)`.
- helpers `tx / rx / both / source_arq / sink_arq / run_pair / options`.

**Spectrum — `drivers/usrp/python/channel_sense.py`, `freq_scan.py`**

- `sense_channel(...)` — measure power at one frequency.
- `python3 drivers/usrp/python/freq_scan.py --start 902 --stop 928 --rx-args addr=192.168.20.2` — find a quiet carrier.

---

## 8. Troubleshooting (beginner gotchas)

| Symptom | Fix |
|---|---|
| `pyphy` import / `Python.h` / arch error (macOS) | run via `./run.sh` (it sets `PYTHONPATH` + `arch -x86_64`), or build with `drivers/usrp/bindings/build.sh` |
| `Exec format error` on `sdr_system` | rebuild on that machine: `cd drivers/usrp && cmake -S . -B build && cmake --build build` |
| receiver gets nothing / no ACK | start the **receiver first**; keep both radios on; use `DQPSK` or `OFDM` |
| startup "rate check" fails | N210 needs `--rx-rate 2e6 --symbol-rate 1e6`; set `--tx-rate = --rx-rate` |
| a stuck receiver won't stop | press Ctrl-C again; last resort `Ctrl-\` or `kill -9` |
| coherent `QPSK` ~17% delivered | free-running clocks — use `DQPSK` (single-carrier) or `OFDM` |

---

### 8.1 Gotchas that bite on real hardware

| Symptom | Cause → Fix |
|---|---|
| `Exec format error` running `sdr_system` | Wrong-arch binary → rebuild natively on that box (see §1). |
| `<frozen getpath>` Python crash | Stale shell CWD → `cd` out and back in. |
| Sink fails the **rate check** at startup | The receive-only sink still checks the full chain → set `tx_rate = rx_rate` on it; N210 needs `--rx-rate 2e6 --symbol-rate 1e6`. |
| `ACQ ERROR / Filtered vector too short` (works at small `k`, fails at large) | Long frame's energy dips below the detector → raise TX power/gain, add `--det-mult`, `--energy_packet_size`, or use smaller `--ldpc-k`. |
| Single-shot burst gets **no ACK** (MARL) | Cold LO / free-running CFO → keep the AP **warm**, use **DQPSK**, ideally a shared 10 MHz clock; "no ACK" is then ≈ collision. |
| Coherent **QPSK ~17% delivery** | Free-running LO → use **DQPSK** (SC) or **OFDM** (coherent, pilots track CFO). |
| CLIP accuracy ~0 even at high SNR (uncoded) | Any bit error wrecks the 512-float payload → add FEC (`--fec turbo`), use soft decode. |
| RX won't stop on Ctrl-C | Press Ctrl-C again (escalating handler); emergency = `Ctrl-\` or `kill -9`. |
| Two processes on one host clash | Give distinct ports: ARQ `--ack-port`, FL downlink `--net-port`, slot clock `--port 5600`. |
| pyphy `Python.h not found` / arch mismatch (macOS) | Use `drivers/usrp/bindings/build.sh` (adds `-isystem`, `python3-config`); run with `arch -x86_64 python3`. |

---

---

## 9. The built-in experiments, end to end

Concrete two-host recipes for the applications that ship with the platform. Each
starts radio-free so you can check the logic before any hardware is involved.

### 9.1 MARL random access

RL agents share one channel; the **ACK is the reward** (delivered = success, timeout =
collision/loss). Single-shot bursts (`--max-attempts 1`); the *policy* owns retransmission.

#### Step 0 — radio-free validation (no hardware)
```bash
cd algorithms/marl_ra
python3 ap_multi.py --self-test      # per-agent ACK routing (synthetic id stream)  -> PASS
python3 ap_multi.py --sim-test       # parser + end-to-end with a fake C++ sink       -> PASS
python3 slot_sync.py --self-test     # slot clock aligns 3 clients                     -> PASS

# offline decentralized run (one process per agent, mock medium):
python3 mock_medium.py --agents 2 --slots 600 &
python3 agent_node.py --mock --id 0 --slots 600
python3 agent_node.py --mock --id 1 --slots 600

# offline training (learns to transmit; prints P(transmit|queued) climbing):
python3 marl_train.py --mock --steps 400                       # single agent (A2C)
python3 marl_multi_train.py --mock --agents 4 --steps 800 \
        --coll-penalty 0.5 --compare-aloha "0.15,0.25,0.5"     # multi-agent vs ALOHA
```
**Success:** self/sim-tests PASS; in training `P(transmit|queued)` rises on a clean mock link;
multi-agent collision rate drops and per-agent `P(transmit)` settles near `1/N`.

#### Step 1 — hardware, single agent (2 USRPs: 1 AP + 1 agent)
```bash
# AP host (warm receiver + ACK router):
python3 real_channel.py ap --rx-args serial=30CD3F7

# Agent host (trains online from real ACK/timeout):
python3 marl_train.py --tx-args serial=30CD424 --steps 150 --scheme DQPSK
```
**Success:** the agent fires real bursts; reward tracks real delivery; the model + reward plot
save next to `--out`. (One agent has no collision partner — use Step 2 or the jammer for contention.)

#### Step 2 — hardware, decentralized multi-agent (3 USRPs: 1 AP + 2 agents)
```bash
# AP host — start BOTH the decoder/ACK router and the slot clock:
python3 ap_multi.py --agents 2 --scheme QPSK --rx-args serial=30CD3F7    # B210 AP
#   (N210 AP instead:  --rx-args addr=192.168.20.2 --rx-subdev A:0 --rx-rate 2e6 --symbol-rate 1e6 --scheme DQPSK)
python3 slot_sync.py --agents 2 --slot-ms 150                            # slot clock (port 5600)

# Each agent host (local-only obs; learned CSMA), pointing at the AP host:
python3 agent_node.py --id 0 --tx-args serial=30CD424 --scheme QPSK --ap-host $AP_IP --slot-host $AP_IP
python3 agent_node.py --id 1 --tx-args serial=<3RD>   --scheme QPSK --ap-host $AP_IP --slot-host $AP_IP
```
**Success:** AP prints `[BURST] id=… hex=…` per decoded frame and routes an ACK to that agent;
≥2 intents in a slot ⇒ COLLISION (no ACK); agents learn to back off (collision rate falls).

### Optional — add real contention with the jammer
```bash
python3 ../jammer/jammer.py --tx-args serial=<JAMMER> --freq 915e6 \
        --mode burst --interval 150 --duration 300 --tx-gain 80
```

---

### 9.2 Federated learning (MNIST)

Clients train locally and send **compressed model deltas** (float32 vectors) up to a server that
FedAvg-aggregates and broadcasts the aggregate down.

#### Step 0 — radio-free
```bash
cd algorithms/_shared
python3 fl.py --mock --clients 2 --rounds 20                    # compressed (default) -> ~0.71→0.93
python3 fl.py --mock --clients 2 --rounds 20 --compress-ratio 0 # full model

# whole protocol over TCP (no radios), real server/client processes:
python3 fl.py server --clients 1 --rounds 20 --uplink tcp --downlink tcp &
python3 fl.py client --client-id 0 --clients 1 --rounds 20 --uplink tcp --downlink tcp \
        --net-host 127.0.0.1 --net-port 5700 --ack-host 127.0.0.1
```
**Success:** mock accuracy climbs ~0.71→0.93 in 20 rounds; TCP run reaches ~0.92 in 12 rounds.

#### Step 1 — hardware (server = N210 RX-only, client = B210 TX). Start the server first.
```bash
# Server host (N210): uplink over RF, model broadcast back over TCP
python3 fl.py server --clients 1 --rounds 20 --uplink wireless --downlink tcp \
        --rx-args addr=192.168.20.2 --rx-subdev A:0 --net-port 5700

# Client host (B210):  ack-host + net-host = the server's IP
python3 fl.py client --client-id 0 --clients 1 --rounds 20 --uplink wireless --downlink tcp \
        --tx-args serial=30CD424 --ack-host $SERVER_IP --net-host $SERVER_IP --net-port 5700
```
**Success:** each round the client's delta is delivered (ARQ), the server aggregates and broadcasts;
accuracy rises round over round. Use `--waveform ofdm` for coherent QPSK if the SC link is marginal.

---

### 9.3 STC-AirComp (design only — not yet runnable)

This app is specified but **not implemented** (blocked on the paper's exact STLC precoding/combiner
equations and on a synchronized 2-channel RX capture). See `stc_aircomp/INTEGRATION.md`.
When built, the operating sequence will be:

1. **DSP loopback (radio-free):** validate a 2-sensor → 1-AP sum through a simulated 2-antenna AWGN
   channel (0 error at high SNR; NMSE-vs-SNR curve).
2. **`capture2` bring-up:** verify a synchronized 2-channel RX on a B210/X310 (2×2) captures one known TX.
3. **Single-sensor RF:** one B210 sensor → 2×2 AP; STLC-encode, transmit, `capture2`, combine, recover
   the single value. Measure the timing/phase coherence budget.
4. **Two-sensor RF sum:** add a second B210, `slot_sync` them into one slot, recover `v1+v2`; report NMSE.
   Try **DBPSK** first (no shared clock), then BPSK + `--ref external` if coherence demands it.

To start building it, provide the paper's STLC matrices + estimator (see the INTEGRATION note's §7).

---

### 9.4 CLIP semantic communication

The base station encodes an image with **CLIP** into a float32 embedding and transmits **only the
embedding**; the user classifies it (zero-shot) with no return link. Data-transfer archetype.

```bash
cd algorithms/clip_semcom
```

#### Step 0 — radio-free (no torch weights needed; uses the mock CLIP)
```bash
python3 semcom.py demo --mock                       # encode -> lossless channel -> classify (~0.99)

# reproduce the paper's accuracy-vs-noise (Fig. 3) through OUR real modem + FEC:
PYTHONPATH=../../drivers/usrp/bindings arch -x86_64 python3 semcom.py demo --mock \
        --channel pyphy --scheme QPSK --fec turbo --snr-sweep 0,2,4,6,10

python3 semcom.py demo --mock --model-sweep         # 3-CLIP-model accuracy / payload / delay / energy
```
**Success:** clean accuracy ≈ 1.0; in the `pyphy` sweep accuracy climbs with SNR tracking BER
(≈0 @0 dB → ~0.7 @4 dB → ~0.99 @6 dB); `int8` quant (`--quant int8`) is 4× smaller payload.

#### Step 1 — real CLIP weights (optional; torch already installed)
```bash
pip install -r requirements.txt        # open_clip_torch, pillow, ftfy, regex
python3 semcom.py demo --model vit-l14 # drops --mock automatically -> real ViT-L/14
```

#### Step 2 — over the radio (RX host = N210, TX host = B210). Start RX first.
```bash
# RX host (N210): receive embeddings and classify locally
python3 semcom.py rx --rx-args addr=192.168.20.2 --rx-subdev A:0 --num 8

# TX host (B210): encode images, send each embedding (ack-host = RX host IP)
python3 semcom.py tx --tx-args serial=30CD424 --ack-host $RX_IP
```
**Success:** the RX prints `predicted=<class> (truth=<class>)` per embedding. The radio path uses
reliable ARQ (lossless delivery). For the on-air noise→accuracy curve, use the `--channel pyphy`
sweep in Step 0 (radio-free), or the phase-2 single-shot mode in the INTEGRATION note.

---

---

## 10. Quick-reference card

```bash
# ---- radio-free (no hardware) ----
(cd algorithms/marl_ra && python3 ap_multi.py --sim-test)      # MARL routing
(cd algorithms/marl_ra && python3 marl_multi_train.py --mock --agents 4 --steps 800 --coll-penalty 0.5)
(cd algorithms/_shared && python3 fl.py --mock --clients 2 --rounds 20)  # FL
cd algorithms/clip_semcom && python3 semcom.py demo --mock   # CLIP
PYTHONPATH=../../drivers/usrp/bindings arch -x86_64 python3 semcom.py demo --mock --channel pyphy --fec turbo --snr-sweep 0,2,4,6,10

# ---- hardware (RX/AP host first, TX host second) ----
# MARL single:   real_channel.py ap --rx-args serial=30CD3F7   |   marl_train.py --tx-args serial=30CD424 --steps 150
# MARL multi:    ap_multi.py --agents 2 --rx-args serial=30CD3F7 + slot_sync.py --agents 2 | agent_node.py --id N --ap-host $AP_IP --slot-host $AP_IP
# FL:            fl.py server --uplink wireless --downlink tcp --rx-args addr=192.168.20.2 --rx-subdev A:0 | fl.py client --tx-args serial=30CD424 --ack-host $SERVER_IP --net-host $SERVER_IP
# CLIP:          semcom.py rx --rx-args addr=192.168.20.2 --rx-subdev A:0 | semcom.py tx --tx-args serial=30CD424 --ack-host $RX_IP
```

For engine options: `PARAMETERS.md` (auto-generated, complete). For the full PHY reference:
[`SYSTEM_REFERENCE.md`](SYSTEM_REFERENCE.md). For per-application design: each folder's `README.md` + `INTEGRATION.md`,
and [`APPLICATIONS_INTRO.md`](APPLICATIONS_INTRO.md).

---

---

## Beyond this guide

For deeper reference (in the repo root / `algorithms/`): **`PARAMETERS.md`** (every option — or run
`sdr_system --help`), **`SYSTEM_REFERENCE.md`** (the engine math + every algorithm), **`COMMANDS.md`**
and **`USRP_CARRIER_MODULATION.txt`** (ready-to-run command recipes per scheme / per device),
**`HARDWARE.md`** (hardware setup), and **§9 above** (running the example apps).
