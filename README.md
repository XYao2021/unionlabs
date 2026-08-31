# UnionLabs SDR Platform

Write an algorithm once; run it over **any** physical layer. Your algorithm says only *what to
transmit* and *what to receive* — the platform carries it over a USRP software-defined radio, a
LoRa module, or no radio at all, without a single line of your code changing.

```bash
./run.sh --algo fl                                  # federated learning, no radio needed
./run.sh --algo fl --channel lora --lora-sf 9       # the SAME algorithm, over LoRa
./run.sh --algo fl --channel usrp --sim-snr-db 8        # the SAME algorithm, over the USRP modem
./run.sh list                                       # what algorithms exist, and their roles
```

New here? Start with **[`docs/BEGINNER_GUIDE.md`](docs/BEGINNER_GUIDE.md)**.

## The three questions every run answers

Every experiment is a choice of three independent things. Mixing them up is the most common
source of confusion, so the flags keep them apart:

| Question | Flag | Choices |
|---|---|---|
| **What** am I running? | `--algo` | any folder in `deploy/workspace/algorithms/` (`./run.sh list`) |
| **Which PHY** carries it? | `--channel` | `ideal` · `usrp` · `lora` |
| **Which part** am I? | `--role` | `loopback` `chain` `gossip` `multi` · `tx` `rx` `relay` `peer` |

The first four roles build the **whole network in one process** — that is how you develop. The
last four make this process **one node** of it — that is how you deploy.

A fourth flag says **how** the chosen PHY is attached — `--usrp-backend pyphy|radio`,
`--lora-backend sim|serial|spi`. Every PHY's default backend needs **no hardware**, so you can
develop the entire experiment on a laptop and then change one flag to move to radios.

```bash
# one process, whole network, no hardware — how you develop
./run.sh --algo dl --role gossip --agents 6 --topology ring --channel lora

# one terminal (or one computer) per node — how you deploy
./run.sh --algo dl --node 0 --agents 3 --radio serial=30CD424
./run.sh --algo dl --node 1 --agents 3 --radio serial=30CD3F7
```

## Quick start

Python 3.8+ and one install step. No radio, no C++ build:

```bash
pip install -r requirements.txt
./run.sh selftest              # confirm it works: every experiment × every radio-free PHY
./prepare.sh --device x310 --addr 192.168.40.2 --band vert2450-5g   # measure this testbed once
```

Then:

```bash
./run.sh                       # defaults: algo=echo, role=loopback, channel=ideal
./run.sh --algo marl           # any algorithm from algorithms/
./run.sh list                  # what exists, and the roles each accepts
./run.sh --help                # every option, grouped
./radio.sh rx                  # raw receive on a USRP  (B210 default; --device n210|x310)
```

`selftest` is the first thing to run on a fresh clone and the fastest way to tell whether a
change broke something. It exits non-zero on failure, so CI can use it directly.

## Layout (detail in [`docs/STRUCTURE.md`](docs/STRUCTURE.md))

Four files and five folders at the top level — everything you write lives in **one** of them:

```
README.md                 what this is (you are here)
docs/HOW_TO_ADD_ALGORITHM.md  the tutorial: write your own experiment
run.sh                    run an experiment over a PHY
radio.sh                  raw TX/RX on a USRP

algorithms/   ← THE folder you work in. One subfolder per experiment, each with app.py
union/           the UnionLabs bridge: one contract for every PHY and testbed
drivers/         the PHYs: usrp/ (C++ modem + pyphy), lora/ (SX1276), sim/
docs/            every guide, reference and PDF
deploy/          Docker + install
results/         generated output (gitignored)
```

| Path | What |
|---|---|
| `algorithms/` | **your** work, one folder each |
| `union/` | the **middleware** — `phy_link.py`, `run_algo.py`, `driver.py` |
| `drivers/` | the **driver layer**, one per PHY — `usrp/`, `lora/`, `sim/` |
| `docs/` | guides, references, PDFs, diagrams, slides |

Shipped experiments: `echo` `plain_echo` `fl` `dl` `marl` `marl_multi` `clip_semcom`
`stc_aircomp` `jammer` — plus `_template/` to copy, and `_shared/` for libraries that more
than one experiment uses.

**Two layers.** `union/` is the abstraction, shared by everything. `drivers/<name>/` is one
driver per (PHY × testbed). Experiments import from `union/` only, which is why the same
experiment runs on any driver — the portability POWDER and AERPAW do not expose.

## The physical layers

| `--channel` | Driver | Backends |
|---|---|---|
| `ideal` | `drivers/sim` | — |
| `usrp` | `drivers/usrp` | `pyphy` (default, no radio) · `radio` |
| `lora` | `drivers/lora` | `sim` (default) · `serial` · `spi` |

- **`ideal`** — lossless and in-process. Check your logic here first.
- **`usrp`** — the real C++ modem: OFDM or single-carrier, LDPC/turbo/Viterbi FEC, sync, CFO.
- **`lora`** — SX1276. The 255-byte MTU means the driver fragments and retransmits, and it
  reports the real airtime that cost.

Each PHY keeps its own knobs, because the PHYs genuinely differ — we assemble the USRP
waveform, while a LoRa chip embeds its modulation and CRC:

```
shared    --freq (MHz)  --max-attempts  --arq
simulated --sim-snr-db      (the noise a SIMULATED channel adds; on real radios SNR is
                         measured, not set — use the gains below)
usrp      --modulation --fec --samp-rate --symbol-rate --tx-gain --rx-gain
          --ack-transport tcp|rf  --ack-timeout  --radio serial=…|addr=…
lora      --lora-sf 7..12  --lora-cr 5..8  --lora-bw 125000|250000|500000
          --lora-power  --lora-port /dev/ttyUSB0
```

Passing one PHY's knob while another is selected prints a NOTE rather than being silently
ignored.

## Documentation

Read in this order. Everything else is reference you look things up in, not
material you read front to back.

**Start here**

| Doc | Its one job |
|---|---|
| [`docs/BEGINNER_GUIDE.md`](docs/BEGINNER_GUIDE.md) | **the front door** — install, first run, every `run.sh` option, driving the radio, and the built-in experiments end to end |
| [`docs/HOW_TO_ADD_ALGORITHM.md`](docs/HOW_TO_ADD_ALGORITHM.md) | write your own `app.py`: the contract, roles, linking existing code |
| [`docs/STRUCTURE.md`](docs/STRUCTURE.md) | the repo map and how the two layers stack |

**Look it up**

| Doc | Its one job |
|---|---|
| [`docs/PARAMETERS.md`](docs/PARAMETERS.md) | every **C++ modem** option — auto-generated on build, so it cannot drift |
| [`docs/PARAMETERS_ALGO.md`](docs/PARAMETERS_ALGO.md) | every **`run.sh`** and LoRa option — also auto-generated |
| [`docs/COMMANDS.md`](docs/COMMANDS.md) | ready-to-run commands, and tuned settings per modulation scheme |
| [`docs/HARDWARE.md`](docs/HARDWARE.md) | radio inventory, wiring, FPGA images, the per-device gotchas |
| [`docs/USRP_CARRIER_MODULATION.txt`](docs/USRP_CARRIER_MODULATION.txt) | raw `sdr_system` commands per device (B210 / N210 / X310) |
| [`docs/SYSTEM_REFERENCE.md`](docs/SYSTEM_REFERENCE.md) | the engine: the math and every DSP stage, one section each |
| [`docs/MANIFEST.md`](docs/MANIFEST.md) | file index and the CLI ↔ `sdr.py` mapping |
| [`drivers/usrp/GUIDE.md`](drivers/usrp/GUIDE.md) | the USRP PHY: every way to drive it directly |
| [`drivers/lora/README.md`](drivers/lora/README.md) | the LoRa PHY: firmware, wiring, the three attachments |

**Design notes — where things are going, not how to run them**

| Doc | Its one job |
|---|---|
| [`docs/APPLICATIONS_INTRO.md`](docs/APPLICATIONS_INTRO.md) | what each application is, what is built, what is still open |
| [`docs/CROSS_TESTBED_FL.md`](docs/CROSS_TESTBED_FL.md) | federated learning across two testbeds: the plan and the wiring |

PDFs are not kept in the repo — the committed ones had all gone stale against
their markdown. Render one when you need to send it: `./docs/make-pdf.sh BEGINNER_GUIDE`.

## Install

```bash
deploy/initialization.sh --build     # toolchain + compile drivers/usrp/build/sdr_system
```

`--minimal` skips torch/networkx/opencv (PHY only); `--docs` adds the PDF toolchain.

**Or run it in a container instead** — one image that downloads the platform, installs
everything, and serves a desktop in your browser with no password:

```bash
deploy/docker/build-unionlabs.sh     # clones the repo inside the image
deploy/docker/run-unionlabs.sh       # prints a http://<host>:6080/vnc.html link
# ^ these run on a BUILD HOST, from a git checkout. They are pruned from the
#   session image along with deploy/testbed/, which is node administration:
#   inside a session there is no host to run them against.
```

See [`deploy/DOCKER.md`](deploy/DOCKER.md) for the options and the other two images.
