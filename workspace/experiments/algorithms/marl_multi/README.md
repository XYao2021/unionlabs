# MARL-RA — Multi-Agent RL Random Access

Application 1 (control / random-access archetype) on the shared SDR PHY. Multiple RL agents
contend for one channel; each transmit decision is a **single-shot burst** and the **ACK is the
reward** (delivered = success, timeout = collision/loss). The learned policy — not the PHY —
owns retransmission/backoff. The RL is the *brain* (when to transmit); the SDR is the *body*
(the real channel).

**Contents**

| File | What |
|---|---|
| `marl_ra.py`, `marl_base.py`, `marl_network.py`, `marl_learning.py`, `marl_setting.py` | the multi-agent RL code (actor/critic networks, environment, training) |
| `INTEGRATION.md` | how the RL hooks onto the PHY — the ARQ mismatch and its resolution, per-agent ACK routing, slot sync, collision realism, the phased plan |
| `results/` | saved runs |

The Python glue that drives the radio lives **here in this folder** (alongside the RL code): `marl_phy.py` /
`real_channel.py` (warm source / access point), `ap_multi.py` (per-agent ACK routing),
`agent_node.py` (one decentralized agent per process), `slot_sync.py` (the slot clock),
`marl_train.py` / `marl_multi_train.py` (online training).

## How to run it

See **`../EXPERIMENT_GUIDE.pdf`** (or `.md`) **§1A** for the full step-by-step: radio-free
validation (`ap_multi.py --sim-test`, mock training) through hardware single-agent
(`real_channel.py ap` + `marl_train.py`) and decentralized multi-agent (`ap_multi.py` +
`slot_sync.py` + `agent_node.py` per agent). Design rationale is in `INTEGRATION.md`.

## See also

- `../EXPERIMENT_GUIDE.pdf` (or `.md`) — step-by-step commands to run every application (radio-free + hardware).
- `../APPLICATIONS_INTRO.pdf` — all applications introduced together.
- `../../../../README.md` — the PHY wrapper (`sdr.py`) and role modes the adapters use.
