# `algorithms/` — everything you run lives here

**One folder to open.** Algorithms and worked applications are not separate any more: each
experiment is a folder with an `app.py` (the bridge to the platform) plus whatever code, data
and docs belong to it. Put yours in `algorithms/<name>/app.py` as a plain object that says
**what to transmit** and **what to receive**; the framework runs it over any PHY. You never
touch the radio.

```bash
./run.sh list          # every experiment here, with the roles it accepts
./run.sh --algo fl     # run one
```

> **How to add one → [`HOW_TO_ADD_ALGORITHM.md`](../HOW_TO_ADD_ALGORITHM.md)** — where to put it,
> how to write `app.py`, how to name your own roles, how to link your existing code. The framework
> internals it relies on live in `../union/phy_link.py` + `run_algo.py`.

## What is in here

| Folder | What it is |
|---|---|
| `_template/` | copy this to start a new experiment |
| `_shared/` | libraries used by **more than one** experiment — `fl_core.py` + `mnist_sgd_over_sdr.py` (fl, dl) and `marl_learning.py` + `marl_setting.py` + `marl_base.py` (marl, marl_multi) |
| everything else | one experiment each — see the table below |

An experiment folder holds its own code: `fl/` has `fl.py` beside `app.py`, `marl_multi/` has the
whole multi-agent application (`agent_node.py`, `ap_multi.py`, `slot_sync.py`, training scripts,
`INTEGRATION.md`), `clip_semcom/` has `semcom_core.py`. Only `app.py` knows the framework exists.

## Worked examples in this folder

Each is the real algorithm with **all PHY plumbing removed** — only `transmit`/`receive`/`on_result`
— which is what proves the same uniform API carries every archetype:

| Algorithm | What `transmit`/`receive` do | Roles | Runs with |
|---|---|---|---|
| **`echo`** | round-trip smoke test (reply = 2×request), as an `SdrApp` subclass | tx/rx | `--role loopback` |
| **`plain_echo`** | the same test as a **pure algorithm + 2-line `make()` binding** (no framework import) | tx/rx | `--role loopback` |
| **`fl`** | **federated learning on MNIST**: client `transmit()` = its locally-trained model; server `transmit()` = the FedAvg aggregate | `client` `server` `relay` | `loopback` `chain` |
| **`dl`** | **decentralized learning on MNIST**: no server — every peer trains locally, exchanges with its graph neighbours only, and averages (consensus) | `peer` `initiator` `responder` | `gossip` `loopback` `--node K` |
| **`clip_semcom`** | BS `transmit()` = CLIP embedding; user `receive()` classifies and replies the label | tx/rx | `--role loopback` |
| **`marl_multi`** | **real multi-agent** random access: N independent A2C agents contend for one AP and learn to avoid collisions | agent | `--role multi` |
| **`marl`** | **online single-agent A2C** — observes [AoI, queue, busy], learns transmit/defer from the ACK reward | tx/rx | `--role loopback` |
| **`stc_aircomp`** | **compute archetype** — N sensors transmit *at once*, the air sums them, the AP recovers Σvᵢ (STLC 2-antenna CSI-free combine) | sensor | `--role aircomp` |

## The two learning algorithms, side by side

`fl` and `dl` are the same task on the same data, differing only in **where the averaging happens** —
which is exactly the comparison the platform exists to make easy:

|  | `fl` (federated) | `dl` (decentralized) |
|---|---|---|
| Topology | every client ↔ one server | peers ↔ their graph neighbours |
| Averaging | FedAvg, at the server | consensus, at every node |
| Fails if | the server is unreachable | nothing — there is no single point |
| Run it | `./run.sh --algo fl --steps 20` | `./run.sh --algo dl --role gossip --agents 6` |

```bash
# federated: 12 rounds on real MNIST, global test-acc 0.759 (round 1) -> 0.906 (round 12)
./run.sh --algo fl --steps 12

# decentralized: 6 peers on a ring; every node ends within a point of the others
./run.sh --algo dl --role gossip --agents 6 --steps 10

# the topology is yours to choose: ring (default), full, or an explicit edge list
./run.sh --algo dl --role gossip --agents 6 --steps 15 --topology full
./run.sh --algo dl --role gossip --agents 6 --steps 15 --topology 0-1,1-2,2-3,3-4,4-5
```

> **Make the topology matter.** With IID shards every node learns nearly the same model and the
> graph barely registers. `DL_NONIID=1` switches to a label-skew split (each node sees ~2 digits),
> and the effect becomes the textbook one — 6 peers, 15 rounds: a line reaches mean accuracy 0.411
> with model disagreement 0.176, a ring 0.442 / 0.108, fully connected 0.506 / 0.094.

## Running the same algorithm on a different PHY

Nothing in these files knows which radio it is on:

```bash
./run.sh --algo fl --channel ideal        # lossless
./run.sh --algo fl --channel usrp --sim-snr-db 8    # the real C++ modem + noise
./run.sh --algo fl --channel lora --lora-sf 9   # the SX1276 LoRa PHY
```

Environment knobs for the learning algorithms (defaults mirror `algorithms/_shared/fl.py`):
`FL_HIDDEN` `FL_ROUNDS` `FL_LOCAL_STEPS` `FL_LR` `FL_BATCH` `FL_CLIENTS` `FL_CLIENT_ID`
`FL_SYNTHETIC`, and `DL_*` equivalents plus `DL_NONIID` / `DL_NODES` / `DL_NODE_ID`. Over a real
radio the whole model is the payload, so shrink it for a quick on-air demo: `FL_HIDDEN=16`.

They reuse each app's own library from its own folder (a real upload brings its own
too). The curated full apps still live under `../experiments/`; user uploads live here.
