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
│   ├── echo/ plain_echo/ fl/ clip_semcom/ marl/ marl_multi/   worked examples
│   └── README.md
│
├── phy/                 THE PHY LAYER  (UHD-based software-defined radio)
│   ├── src/ include/     C++ engine sources + headers
│   ├── build/sdr_system  the compiled radio engine (the modem)
│   ├── CMakeLists.txt     build; POST_BUILD regenerates sdr.py + PARAMETERS.md
│   ├── bindings/          pyphy — the DSP blocks as numpy functions (pybind11 .so)
│   ├── tests/ sim/        C++ tests / simulations
│   ├── tools/             gen_python_api.py (codegen), md/pdf + plotting helpers
│   ├── GUIDE.md           beginner's guide to the PHY-layer codes
│   └── python/            the Python you call:
│       ├── run_algo.py      run an uploaded algorithm over the PHY (uniform API loader)
│       ├── phy_link.py      the uniform API framework (contract, Codec, channels, drivers)
│       ├── sdr.py           AUTO-GENERATED wrapper over sdr_system (do not edit)
│       ├── run.py           run the PHY from a JSON config
│       ├── configs/         ready-made JSON configs (per-scheme ARQ pairs, tones)
│       ├── example.py       hand-written usage examples
│       ├── phy_flow_example.py   a worked pyphy block flowgraph
│       └── channel_sense.py / freq_scan.py / power_monitor.py / ber_*.py   RF utilities
│
├── applications/        WORKED APPS built on the PHY
│   ├── MARL_RA_Union/    multi-agent random access (RL)
│   ├── FL_Union/         federated learning (MNIST)
│   ├── CLIP_SemCom_Union/ CLIP semantic communication
│   ├── STC_AirComp_Union/ space-time over-the-air computation (design)
│   ├── jammer/           configurable RF interferer
│   ├── APPLICATIONS_INTRO.pdf   EXPERIMENT_GUIDE.pdf   (intro + step-by-step run commands)
│
├── results/             figures / run outputs (gitignored, regenerable)
├── deploy/              Docker + install (initialization.sh, run_sink/source.sh, docker/)
└── docs/                diagrams, slides, changelog (CHANGES.md), pandoc assets
```

## The two stacks

**Uniform algorithm API** (PHY-agnostic contract → the radio):

```
algorithms/<name>/app.py        your algorithm: transmit() / receive() / on_result()
        │  make(role)
        ▼
phy/python/run_algo.py          discovers + adapts your algorithm
phy/python/phy_link.py          Codec (array⇄bytes) · channels (ideal|pyphy|radio) · drivers
        │  bytes
        ▼
phy/build/sdr_system            the modem, over the air        (or pyphy, radio-free)
```

**Direct radio API** (auto-generated from the C++ CLI — the single source of truth):

```
phy/build/sdr_system --help     Boost program_options CLI  (source of truth)
        │
        ▼  phy/tools/gen_python_api.py   (CMake POST_BUILD)
        ├──► phy/python/sdr.py           AUTO-GENERATED wrapper (do not edit)
        └──► PARAMETERS.md               AUTO-GENERATED option reference (repo root)
```

Because `sdr.py` and `PARAMETERS.md` are regenerated from `sdr_system --help` on every build,
a new or changed C++ option appears in Python and in the docs automatically — nothing drifts.
Regenerate by hand:

```bash
python3 phy/tools/gen_python_api.py --binary phy/build/sdr_system \
        --out phy/python/sdr.py --md PARAMETERS.md
```

## Build

```bash
cd phy && cmake -S . -B build && cmake --build build        # compiles build/sdr_system
phy/bindings/build.sh                                        # builds pyphy (the blocks)
```

`deploy/initialization.sh --build` installs the toolchain and compiles the engine in one step.
