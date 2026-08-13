# Repo structure

How the project is laid out and how the layers stack. Newcomers start at
[`README.md`](README.md); this file is the map.

## Top level

```
Hardware_update/
├── README.md            overview + the Python radio API           (start here)
├── HOW_TO_ADD_ALGORITHM.md        how to add your own algorithm
├── PARAMETERS.md        every controllable option (AUTO-GENERATED)
├── STRUCTURE.md         this file
├── SYSTEM_REFERENCE.pdf  deep engine reference — the math + every algorithm  (+ .md source)
├── COMMANDS.md           ready-to-run TX/RX command pairs, tuned per scheme
├── COMMANDS_FEC_TESTS.txt  LDPC / turbo FEC test commands
├── USRP_CARRIER_MODULATION.txt  per-device (B210/N210/X310) command reference
├── HARDWARE.md           hardware setup / radio inventory
├── run.sh               run an ALGORITHM over the PHY (uniform API)
├── radio.sh             raw TX/RX on a USRP  (B210 default; --device n210|x310)
│
├── algorithms/          YOUR algorithms — one folder per algorithm, each with app.py
│   ├── _template/        copy this to start
│   ├── echo/ plain_echo/ fl/ clip_semcom/ marl/ marl_multi/ stc_aircomp/   worked examples
│   └── README.md
│
├── union/               THE ABSTRACTION / MIDDLEWARE  (one; all testbeds + all PHYs)
│   ├── phy_link.py        uniform contract: SdrApp · PayloadSpec · Codec · channels · drivers
│   ├── run_algo.py        discovers + adapts + runs any algorithm (loopback/multi/aircomp/tx/rx)
│   └── driver.py          the PhyDriver interface every backend implements
│                          (transfer / broadcast / superpose)
│
├── applications/        WORKED APPS  (curated; code to the union/ contract, not to a driver)
│   ├── MARL_RA_Union/    multi-agent random access (RL)
│   ├── FL_Union/         federated learning (MNIST)
│   ├── CLIP_SemCom_Union/ CLIP semantic communication
│   ├── STC_AirComp_Union/ STC over-the-air computation (the AJOU app; phase-1 built)
│   ├── jammer/           configurable RF interferer
│   └── APPLICATIONS_INTRO.pdf   EXPERIMENT_GUIDE.pdf   (intro + step-by-step run commands)
│
├── drivers/             THE DRIVER LAYER  (many; one per PHY × testbed)
│   ├── usrp_uhd/         the USRP/UHD PHY  (built)
│   │   ├── src/ include/     C++ engine sources + headers
│   │   ├── build/sdr_system  the compiled modem
│   │   ├── CMakeLists.txt     build; POST_BUILD regenerates sdr.py + PARAMETERS.md
│   │   ├── bindings/          pyphy — DSP blocks as numpy functions (pybind11 .so)
│   │   ├── tools/ tests/ sim/ GUIDE.md
│   │   └── python/            sdr.py (auto-gen) · run.py · configs/ · phy_flow_example.py · RF utils
│   ├── sim/             radio-free driver: ideal / pyphy  (impl. as channels in union/phy_link.py)
│   └── lora_arduino/    (planned) Arduino LoRa PHY — a different PHY, same contract
│
├── results/             figures / run outputs (regenerable)
├── deploy/              Docker + install (initialization.sh, run_sink/source.sh, docker/)
└── docs/                diagrams, slides, changelog (CHANGES.md), pandoc assets
```

**Two layers (the UnionLabs model):** `union/` is the **abstraction / middleware** — one
contract, shared across every testbed and PHY; `drivers/<name>/` is the **driver layer** — one
per (PHY × testbed). Experiments in `algorithms/` and `applications/` code to `union/` only, so
the same experiment runs on any driver. This middleware is what POWDER / AERPAW don't expose.

## The two stacks

**Uniform algorithm API** (PHY-agnostic contract → the radio):

```
algorithms/<name>/app.py        your algorithm: transmit() / receive() / on_result()
        │  make(role)
        ▼
union/run_algo.py          discovers + adapts your algorithm
union/phy_link.py          Codec (array⇄bytes) · channels (ideal|pyphy|radio) · drivers
        │  bytes
        ▼
drivers/usrp_uhd/build/sdr_system            the modem, over the air        (or pyphy, radio-free)
```

**Direct radio API** (auto-generated from the C++ CLI — the single source of truth):

```
drivers/usrp_uhd/build/sdr_system --help     Boost program_options CLI  (source of truth)
        │
        ▼  drivers/usrp_uhd/tools/gen_python_api.py   (CMake POST_BUILD)
        ├──► drivers/usrp_uhd/python/sdr.py           AUTO-GENERATED wrapper (do not edit)
        └──► PARAMETERS.md               AUTO-GENERATED option reference (repo root)
```

Because `sdr.py` and `PARAMETERS.md` are regenerated from `sdr_system --help` on every build,
a new or changed C++ option appears in Python and in the docs automatically — nothing drifts.
Regenerate by hand:

```bash
python3 drivers/usrp_uhd/tools/gen_python_api.py --binary drivers/usrp_uhd/build/sdr_system \
        --out drivers/usrp_uhd/python/sdr.py --md PARAMETERS.md
```

## Build

```bash
cd drivers/usrp_uhd && cmake -S . -B build && cmake --build build   # compiles build/sdr_system
drivers/usrp_uhd/bindings/build.sh                                  # builds pyphy (the blocks)
```

`deploy/initialization.sh --build` installs the toolchain and compiles the engine in one step.
