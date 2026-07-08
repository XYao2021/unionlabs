# Python layer — structure

A thin, **auto-generated** Python stack over the C++ `sdr_system` PHY binary. The
C++ CLI is the single source of truth: the wrapper is generated from
`sdr_system --help` on every build, so Python never drifts from the radio code.

## Layering

```
        C++ PHY  ──────────────────────────────────────────────
        build/sdr_system            (Boost program_options CLI = source of truth)
              │  --help
              ▼
        tools/gen_python_api.py      (generator; run by CMake POST_BUILD)
              │  emits
              ▼
   ┌──► python/sdr.py  ◄── AUTO-GENERATED — do not edit
   │        • OPTIONS registry (all 83 options)
   │        • class SDR(**opts)  →  .command() / .run() / .popen() / .set()
   │        • tx() rx() both() sink_arq() source_arq()   (role helpers)
   │        • run_pair(rx, tx)   (drive BOTH ends together)
   │        • options()          (print every option)
   │
   ├──► python/run.py            (JSON config runner — clearest interface)
   │        run(cfg) / main()  →  builds SDR(s) from a config, launches them
   │             │
   │             └─ python/configs/*.json   (ready-made single & paired configs)
   │
   └──► python/example.py        (hand-written usage: the 4 role modes, sweeps, tone)
```

Everything below `sdr.py` is written **against** it, so a new/changed C++ option
appears everywhere automatically after a rebuild.

## Files

```
python/
├── sdr.py            AUTO-GENERATED wrapper (from sdr_system --help). Do not edit.
├── run.py            Run the PHY from a JSON config file (single or paired).
├── example.py        Runnable examples — prints commands without hardware.
├── configs/          Ready-made JSON configs:
│   ├── qpsk_arq_pair.json     two-box QPSK stop-and-wait ARQ (both ends)
│   ├── ofdm_qpsk_pair.json    same, OFDM waveform
│   ├── random_pair.json       random-bit throughput test over ARQ
│   ├── tx_only.json           transmit only (8-PSK, one-way)
│   ├── rx_only.json           receive only
│   ├── tx_tone.json           continuous cosine test tone (signal generator)
│   ├── tx_tone_burst.json     same tone, transmitted in bursts
│   └── rx_tone_monitor.json   raw tone monitor (prints freq + power, no decode)
├── README.md         Usage guide (API + JSON config).
└── STRUCTURE.md      This file.

tools/gen_python_api.py   Generator (parses --help → writes python/sdr.py).
CMakeLists.txt            POST_BUILD hook that reruns the generator every build.
```

## Three ways to use it

| Interface | Best for | Example |
|---|---|---|
| **JSON config** (`run.py`) | day-to-day runs, sharing setups | `python3 run.py configs/qpsk_arq_pair.json` |
| **Keyword API** (`sdr.py`) | scripting, sweeps, automation | `tx(scheme="QPSK", tx_gain=78, fec=True).run()` |
| **Raw command** | inspection / logging | `sink_arq(scheme="QPSK").command()` |

All three build the same `sdr_system` argv underneath.

## The four role modes

| Helper | `--role` | Meaning |
|---|---|---|
| `tx()` | `tx` | transmit only (one radio) |
| `rx()` | `rx` | receive only (one radio) |
| `both()` | `both` | one process: transmit **and** receive (single-box full-duplex / loopback) |
| `sink_arq()` / `source_arq()` | `sink_arq` / `source_arq` | the two ends of stop-and-wait ARQ (two radios) |

`run_pair(rx, tx)` launches two ends together (RX first, head start, then TX; waits
for TX, then lets RX self-terminate) — the two-box "ARQ over the air" case.

## Auto-generation (why `sdr.py` is never edited by hand)

1. `make` builds `build/sdr_system`.
2. CMake's `POST_BUILD` step runs `tools/gen_python_api.py --binary build/sdr_system --out python/sdr.py`.
3. The generator parses `sdr_system --help` and rewrites `python/sdr.py` with every
   current option as a keyword argument (Python `True/False` → `true/false`;
   bool-switch flags added only when `True`).

The generator always exits 0, so a hiccup there can never break the C++ build.
To regenerate by hand:

```bash
python3 tools/gen_python_api.py --binary build/sdr_system --out python/sdr.py
```

## Binary path

`sdr.py` defaults to `../build/sdr_system`. Override per call with `SDR(binary=...)`,
per config with a top-level `"binary"` key, or globally with the `SDR_SYSTEM_BIN`
environment variable.
