# Repo structure

How the project is laid out and how the layers stack. Newcomers start at
[`README.md`](../README.md); this file is the map.

## Top level

```
unionlabs/
├── README.md                overview + the three questions every run answers  (start here)
├── HOW_TO_ADD_ALGORITHM.md  the tutorial: write your own experiment
├── run.sh                   run an EXPERIMENT over any PHY (the uniform API)
├── radio.sh                 raw TX/RX on a USRP  (B210 default; --device n210|x310)
│
├── algorithms/         EVERYTHING YOU RUN — one folder per experiment, each with app.py.
│   │                    Algorithms and worked applications live together here, so there is
│   │                    exactly ONE place to look.
│   ├── _template/         copy this to start
│   ├── _shared/           libraries used by MORE THAN ONE experiment:
│   │                        fl_core.py · mnist_sgd_over_sdr.py   (fl + dl)
│   │                        marl_learning.py · marl_setting.py · marl_base.py (marl + marl_multi)
│   ├── echo/ plain_echo/  round-trip smoke tests
│   ├── fl/                federated learning on MNIST   (client/server/relay) + fl.py
│   ├── dl/                decentralized learning on MNIST (peers over a graph)
│   ├── marl/              single-agent A2C random access + its env/training
│   ├── marl_multi/        multi-agent random access: the full application (agent_node.py,
│   │                      ap_multi.py, slot_sync.py, training, INTEGRATION.md)
│   ├── clip_semcom/       CLIP semantic communication + semcom_core.py
│   ├── stc_aircomp/       STC over-the-air computation (the AJOU app) + stc_core.py
│   ├── jammer/            configurable RF interferer
│   └── README.md
│
├── union/               THE ABSTRACTION / MIDDLEWARE  (one; all testbeds + all PHYs)
│   ├── phy_link.py        uniform contract: SdrApp · PayloadSpec · Codec · ARQ registry
│   │                      · channels (ideal/pyphy/lora) · runners (loopback, chain, gossip,
│   │                        slotted) · links (RadioRoundTrip, PeerLink)
│   ├── run_algo.py        discovers + adapts + runs any experiment; picks the PHY (--channel),
│   │                      the node type (--role) and how the PHY is attached (--*-backend)
│   └── driver.py          the PhyDriver interface every backend implements
│                          (transfer / broadcast / superpose) + what is uniform vs PHY-specific
│
├── drivers/             THE DRIVER LAYER  (many; one per PHY × testbed)
│   ├── usrp/             the USRP/UHD PHY  (built)          --channel usrp
│   │   ├── src/ include/     C++ engine sources + headers
│   │   ├── build/sdr_system  the compiled modem
│   │   ├── CMakeLists.txt    build; POST_BUILD regenerates sdr.py + docs/PARAMETERS.md
│   │   ├── bindings/         pyphy — DSP blocks as numpy functions (pybind11 .so)
│   │   ├── tools/ tests/ sim/ GUIDE.md
│   │   └── python/           sdr.py (auto-gen) · run.py · configs/ · RF utils
│   ├── lora/             the SX1276 LoRa PHY                 --channel lora
│   │   ├── arduino/lora_phy/  PHY firmware (Arduino/Teensy + SX1276)
│   │   ├── python/            lora_radio.py (sim|serial|spi) · framing.py (255 B MTU + ARQ)
│   │   │                      · lora_driver.py (LoRaChannel · LoRaLink · LoRaDriver)
│   │   └── tools/             role_selftest.py · spi_selftest.py
│   └── sim/              radio-free driver: ideal  (impl. as a channel in union/phy_link.py)
│
├── docs/                ALL documentation: BEGINNER_GUIDE · STRUCTURE (this file) · COMMANDS
│                        · PARAMETERS (auto-generated) · HARDWARE · MANIFEST · SYSTEM_REFERENCE
│                        · APPLICATIONS_INTRO · EXPERIMENT_GUIDE · every .pdf · diagrams/slides
├── deploy/              Docker + install (initialization.sh, run_sink/source.sh, docker/)
└── results/             figures / run outputs, regenerable (gitignored)
```

**Why this shape.** Four files and five folders. A newcomer opens `algorithms/` and finds
everything runnable in one place; `union/` is the bridge; `drivers/` is every PHY; `docs/` is
every word of documentation. Nothing else competes for attention at the top level.

**Two layers (the UnionLabs model):** `union/` is the **abstraction / middleware** — one
contract, shared across every testbed and PHY; `drivers/<name>/` is the **driver layer** — one
per (PHY × testbed). Experiments in `algorithms/` and `algorithms/` code to `union/` only, so
the same experiment runs on any driver. This middleware is what POWDER / AERPAW don't expose.

## The two stacks

**Uniform algorithm API** (PHY-agnostic contract → the radio):

```
algorithms/<name>/app.py     your algorithm: transmit() / receive() / on_result()
        │  make(role[, index, total])   +  optional ROLES = {"client": "tx", ...}
        ▼
union/run_algo.py            discovers + adapts your algorithm; resolves --algo / --channel /
        │                    --role into (an app, a PHY, a node type)
union/phy_link.py            Codec (array⇄bytes) · the runners · the links
        │  bytes  ─────────────── transfer(buf) -> (bytes_at_the_peer, info) ───────────────┐
        ▼                                                                                   │
   ┌────────────────────────┬─────────────────────────────┬──────────────────────────────┐  │
   │ --channel ideal        │ --channel usrp              │ --channel lora               │  │
   │ drivers/sim            │ drivers/usrp            │ drivers/lora         │  │
   │ lossless, in-process   │ sdr_system (C++) / pyphy    │ SX1276: sim | serial | spi   │  │
   └────────────────────────┴─────────────────────────────┴──────────────────────────────┘  │
                                                                                            │
   info always carries crc_ok; each PHY adds its own accounting  ──────────────────────────┘
   (ber/snr_db for the modem; frags/retx/airtime_ms for LoRa)
```

**Roles are the middleware's, not a driver's.** `loopback` / `chain` / `gossip` / `multi` /
`aircomp` build the whole network in one process; `tx` / `rx` / `relay` / `peer` run ONE node in
this process, so an experiment scales from a laptop to one terminal per node without an edit.
Every role works on every PHY.

**Direct radio API** (auto-generated from the C++ CLI — the single source of truth):

```
drivers/usrp/build/sdr_system --help     Boost program_options CLI  (source of truth)
        │
        ▼  drivers/usrp/tools/gen_python_api.py   (CMake POST_BUILD)
        ├──► drivers/usrp/python/sdr.py           AUTO-GENERATED wrapper (do not edit)
        └──► PARAMETERS.md               AUTO-GENERATED option reference (repo root)
```

Because `sdr.py` and `PARAMETERS.md` are regenerated from `sdr_system --help` on every build,
a new or changed C++ option appears in Python and in the docs automatically — nothing drifts.
Regenerate by hand:

```bash
python3 drivers/usrp/tools/gen_python_api.py --binary drivers/usrp/build/sdr_system \
        --out drivers/usrp/python/sdr.py --md PARAMETERS.md
```

## Build

```bash
cd drivers/usrp && cmake -S . -B build && cmake --build build   # compiles build/sdr_system
drivers/usrp/bindings/build.sh                                  # builds pyphy (the blocks)
```

`deploy/initialization.sh --build` installs the toolchain and compiles the engine in one step.
