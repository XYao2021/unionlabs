# `algorithms/` — drop your algorithm here

The **upload folder**: put your algorithm in `algorithms/<name>/app.py` as a plain object that only
says **what to transmit** and **what to receive**; the framework runs it over the SDR PHY. You never
touch the radio.

> **How to add one → [`HOW_TO_ADD_ALGORITHM.md`](../HOW_TO_ADD_ALGORITHM.md)** — where to put it, how
> to write `app.py`, how to link your existing code, and how to run it by name (`./run.sh --algo <name>`).
> The framework internals it relies on (`PayloadSpec`, `Codec`, `PhyLink`, `run_loopback`,
> `RadioRoundTrip`, the `adapt`/loader) live in `../union/phy_link.py` + `run_algo.py`.

## Worked examples in this folder (the applications, ported as pure algorithms)

Each is the real algorithm with **all PHY plumbing removed** — only `transmit`/`receive`/`on_result`
— proving the same uniform API carries every archetype:

| Algorithm | What `transmit`/`receive` do | Validated (radio-free) |
|---|---|---|
| **`echo`** | round-trip smoke test (reply = 2×request), as an `SdrApp` subclass | 3/3 ideal, 3/3 pyphy@8 dB, 0/3 @2 dB |
| **`plain_echo`** | the same test as a **pure algorithm + 2-line `make()` binding** (no framework import) | 4/4 ideal |
| **`fl`** | client `transmit()` = trained model vector; server `transmit()` = FedAvg aggregate | model round-trips, test-acc **0.15→0.31** over 5 rounds |
| **`clip_semcom`** | BS `transmit()` = CLIP embedding; user `receive()` classifies, replies the label | **40/40** classified; @6 dB **40/40 correct** on **38/40** CRC-clean frames — *semantic robustness* |
| **`marl_multi`** | **real multi-agent** random access: N independent A2C agents contend for 1 AP; learn to avoid collisions | 4 agents → **throughput 0.42/slot = the slotted-ALOHA optimum**, collision-rate 0.35→0.23 |
| **`marl`** | **online single-agent A2C** — observes [AoI, queue], learns transmit/defer from the ACK reward (the real `Actor`/`Critic`) | `P(transmit\|queued)` climbs **0.51→0.75** over 400 steps; also learns over the pyphy modem |
| **`stc_aircomp`** | **compute archetype** — N sensors `produce()` a scalar; they transmit *at once*, the air sums them, the AP recovers Σvᵢ (STLC 2-antenna CSI-free combine); `--role aircomp` | 8 sensors, NMSE(Σvᵢ) **0.1→1.3e-4** vs SNR; STLC removes the single-antenna error floor |

```bash
./run.sh --algo fl          --steps 6
./run.sh --algo clip_semcom --steps 45
./run.sh --algo marl --steps 400        # single-agent A2C learns transmit/defer
```

They reuse each app's own library from `../applications/<App>_Union/` (a real upload brings its own
too). The curated full apps still live under `../applications/`; user uploads live here.
