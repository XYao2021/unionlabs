# MARL-RA ↔ SDR PHY — integration design note

How the `MARL_RA_Union` multi-agent RL random-access code hooks onto the USRP B210
PHY in this repo. The RL is the *brain* (when to transmit); the SDR is the *body*
(the real channel). Today the MARL code simulates the channel; integration means
replacing that simulated channel with our radios.

---

## 1. What maps to what

| MARL simulator (now, in software) | SDR system (integration target) |
|---|---|
| `ch_usage` flag in the observation | **`channel_sense.py`** — real energy detection (`--role sense` / `SenseStream`) |
| agent action `transmit=1` | **one real burst** on the TX path (`--payload-file` + source, single attempt) |
| `update_channel` collision test (≥2 t_flag) | the **real RF channel** — simultaneous transmitters actually collide |
| ACK after a clean slot | **one real ACK** from the receiver iff it decoded the frame (CRC OK) |
| `wireless_channel()` SNR→capacity | the real link SNR / EVM (already measured by the PHY) |
| `since_success`, queue (local state) | kept in Python, updated from real ACK/timeout outcomes |
| neighbours' AoI via consensus gossiping | needs a **side channel** (TCP) between agents, or local-only observation |

The RL loop is unchanged in spirit: `observe → actor picks {defer, transmit} → act →
reward`. Only the `act` and the observation's channel/collision parts move from
simulation to the radio.

---

## 2. The ARQ mismatch — and how to resolve it  ⭐

This is the central issue. **Our ARQ and the MARL channel model want opposite things.**

- **Our stop-and-wait ARQ** (`source_arq`/`sink_arq`, `--max-attempts 0`) is built for
  *reliable delivery*: it **retransmits every chunk until the whole message arrives**,
  0 unacked. Collisions/losses are *hidden* by retransmission — the caller only ever
  sees success.
- **MARL random access** is the opposite: each transmit decision is **one burst**, and
  whether it succeeds (ACK) or fails (collision → no ACK) **is the learning signal**. If
  we retransmit until success, every packet "succeeds," the collision signal vanishes,
  and there is nothing for the policy to learn. The whole point of the RL is to learn to
  *avoid* collisions — which requires *experiencing* them.

**Resolution — run the PHY in single-shot mode for MARL:**

1. **One burst per decision, no PHY retransmission.** Transmit the packet exactly once
   and wait for an ACK up to a timeout. This is directly available: **`--max-attempts 1`**
   on the source. Outcome:
   - ACK within timeout → **success** (`Unacked chunks=0`).
   - No ACK → **collision / loss** (`Unacked chunks=1`).
   That boolean *is* the reward input. (We do **not** use `--max-attempts 0`.)

2. **The policy replaces ARQ's retransmit logic.** A failed packet is **not** auto-resent;
   it stays in the agent's queue, and the *policy* decides whether/when to try again on a
   future slot. That learned "when to retry" is exactly the collision-avoidance / backoff
   behaviour the MARL is designed to discover — it must live in the agent, not in the PHY.

3. **The receiver becomes a persistent Access Point, not a message reassembler.** Our
   `sink_arq` reassembles one multi-chunk message and *terminates* when complete. For
   MARL the AP must **listen forever and ACK each independently-decoded frame** (one ACK
   per good frame, no reassembly, no "done"). Each transmission is a standalone packet.
   → *needs a small PHY addition* (see §5): an "AP" receive mode that keeps serving.

4. **Collisions come from the real channel — we do not simulate them.** When two agents
   transmit in the same window on the same frequency, their signals genuinely collide at
   the AP; the AP fails CRC and sends no ACK. That is the "real channel problem" you
   noted, and it's *better* than the sim: the sim declares any 2+ overlap a total loss,
   whereas real RF has the **capture effect** — the AP may still decode the stronger
   signal and ACK it. So a collision doesn't always mean *both* fail. The policy will
   learn against the true channel, capture effect included.

5. **Packet = one ARQ unit.** Size `--bytes-length` so a MARL packet is **one chunk**
   (their model = 1500 B/packet; set bytes-length accordingly, or send a few chunks but
   treat the whole packet as a single `max_attempts=1` attempt). "Success" = the packet
   got through in that single attempt.

**Summary:** keep the reliable ARQ for data transfer (the MNIST demo), but for MARL use
**single-shot ARQ (`--max-attempts 1`) + a persistent per-frame-ACK AP**, and let the
learned policy — not the PHY — own retransmission.

---

## 3. Collision realism vs. radio count (2 × B210)

Real collisions need **≥2 simultaneous transmitters + 1 AP = ≥3 radios**. With two B210s:

- **1 agent + 1 AP** — validate the full loop end-to-end (sense → decide → one burst →
  ACK/timeout → reward). No self-collision is possible with a single transmitter, so
  create the "busy/collision" signal with a **scripted interferer** (e.g. the tone/burst
  TX we already have) or ambient traffic. This proves the mechanics and the reward wiring.
- **True N-agent collision learning** needs N+1 radios (or emulating other agents' RF).
  Interim option: keep the *multi-agent dynamics in simulation* but feed the agent-under-
  test's **real** link outcomes (real sense + real ACK/timeout) — a hardware-in-the-loop
  single agent among simulated peers.

---

## 4. Timescale reconciliation

The sim runs **9 µs slots** with a transmit decision every few slots — thousands/sec. Our
real loop is far slower per decision (radio init, burst, ACK RTT ≈ 0.2 s even tuned). So:

- **Do not** map one sim slot to one real slot. Redefine a "decision epoch" as **one
  real sense→decide→(maybe TX)→outcome cycle** (~100–300 ms with the persistent
  `SenseStream` + `--max-attempts 1`, or slower if the process re-inits each time).
- Keep the radio **persistent** across decisions (the `SenseStream` pattern, and an
  always-on AP) so each epoch is ~one burst + one ACK window, not a ~2 s re-init.
- The RL is timescale-agnostic (it optimises per-epoch reward); only the *wall-clock* and
  the AoI/throughput *units* change. Report AoI in epochs (or seconds), not 9 µs slots.

---

## 5. Reward reconciliation

The sim computes reward from simulated success/delay. On hardware, compute the same
quantities from **real** outcomes:

| objective | sim reward | real-channel reward |
|---|---|---|
| 0 fair AoI | `-since_success` | `-epochs_since_last_ACK` (age grows until a real ACK) |
| 1 max throughput | simulated bytes/s | real delivered bytes / elapsed (ACKed frames only) |
| 2 fair throughput | α-fair of sim tput | α-fair of real per-agent ACKed throughput |

Key change: **"success" is now a real ACK, "collision" is a real timeout.** `since_success`
resets on a real ACK; the queue drains on a real ACK and grows on real arrivals. Everything
the reward needs is already produced by the single-shot source (ACK vs timeout) — no PHY
change beyond §2.

---

## 6. Concrete PHY changes needed

Bridge built: **`python/marl_phy.py`** (`sense()`, `transmit_once() -> bool`, `AccessPoint`,
`MarlRadio`). Hardware findings that reshaped the plan:

1. **Single-shot transmit — DONE and validated.** `--max-attempts 1` sends one burst and
   reports `Unacked chunks`; `transmit_once()` returns `acked = (Unacked == 0)`. Confirmed
   on the radios: a warm AP ACKs a single 32-byte burst on the first attempt
   (`Sent=1 Retransmissions=0 Unacked=0`). ACK = success, timeout = collision/loss. ✓
2. **Persistent warm Access Point — BUILT & validated (`--serve-forever`).** `sink_arq
   --serve-forever` starts the radio ONCE (stays warm/settled) and re-accepts a source per
   fire, ACKing each decoded frame; a per-session timeout re-accepts if a source's burst
   never decodes and it disconnects (else the sink would deadlock waiting on a source that
   left). Only the lightweight TCP accept recycles — the RX pipeline never re-inits.
   `marl_phy.AccessPoint` drives it; `transmit_once` fires against it.
   - Validated on the radios: warm AP + fire-on-demand single-shot works — 3/3 and 2/3 ACKed
     in good windows, and the AP stays up across fires. Only the sink's *radio* must be warm;
     the source can stay cold/per-attempt (its cold-start is absorbed by the ~2 s ACK window).
   - **Caveat (the real limit):** single-shot success tracks the link's CFO window — 3/3, then
     2/3, then 0/6 across windows this session, while multi-attempt ARQ always got through.
     On a free-running link a lost single burst is *ambiguous* (collision vs. link loss), which
     muddies the RL reward. A **shared 10 MHz clock** makes single-shot reliable-when-idle so
     "no ACK ≈ collision" holds. Until then, treat the reward as noisy or gate on link quality.
   - **Warm source — BUILT (`source_arq --on-demand`).** The transmitter's dual: the TX radio
     starts once and stays warm; each line on stdin fires ONE packet and prints
     `RESULT acked=0|1` (a fresh ACK connection per fire lets the AP re-accept). No ~2 s
     re-init per packet. `marl_phy.WarmSource` drives it: `tx.fire() -> bool`. Validated —
     8 fires from one warm process, radio never re-initialised. So both ends stay warm and
     packets fly only on command, exactly the target shape.

Already available and reused as-is: `channel_sense.py` (sense / `SenseStream` /
`should_transmit`), `--payload-file`/`--out-file` byte-pipe, `--timeout`, `--bytes-length`,
the actor networks in `MARL_learning_Union.py`.

The neighbour-AoI observation (shared via gossiping in the sim) needs a **TCP side channel**
between agents on real hardware — analogous to the ARQ ACK socket — or restrict the agent
to **local-only observation** (own queue + own AoI + sensed channel) for a first cut.

---

## 7. Phased plan

1. **Bridge layer (Python) — DONE.** `marl_phy.py` (`sense`, `transmit_once`, warm
   `AccessPoint`, warm `WarmSource`) and **`real_channel.py`** — the reusable channel:
   `RealChannel(tx_args, sense_rx_args)` with `sense()`, `transmit()`, `step(action)`.
   Drop it in place of any simulated random-access channel (MARL or future studies);
   both radios stay warm and packets fly only on `transmit()`/`step`. The AP runs
   separately (`AccessPoint`). All validated on hardware; delivery rate tracks the CFO
   window (a shared clock makes it reliable).
2. **Env adapter — DONE.** `marl_env.py` `RealChannelEnv`: a gym-style env that keeps the
   MARL device model (Poisson arrivals, finite queue, Age-of-Information, throughput) but
   drives the channel through an injected `RealChannel` — `reset()` / `step(action)` returning
   `(obs, reward, done, info)` in the sim's convention (obs `[AoI/60, queue/Q_max, ch_usage]`,
   objective-0 reward `-AoI/(15·num_D)`). Channel is dependency-injected, so `MockChannel`
   validates the logic offline and `RealChannel` runs it on the radios. Mapping:
   `ch_usage → sense()['busy']`, transmit → `channel.transmit()` (ACK=delivered → dequeue +
   AoI reset; no-ACK=collision/loss → keep + retry is the policy's job). Validated offline
   (queue/AoI/reward correct) AND end-to-end on hardware (env fired a real burst via the warm
   source, updated state from the real ACK/timeout). Single real agent (2 B210s); real
   multi-agent needs more radios or simulated peers.
3. **Online training on the real link — DONE.** `marl_train.py`: single-agent A2C reusing
   `MARL_learning_Union.Actor_A2C` + `Critic` over `RealChannelEnv`. Per step: obs → actor
   softmax → sample {defer, transmit} → `env.step` (a real burst if transmit) → reward → one-
   step TD update of critic + actor. `--mock` validates the loop offline; the real run trains
   from live ACK/timeout. Validated: offline the agent learns to transmit (P(transmit|queued)
   0.51→0.93 on a clean mock link); on the radio it trained end-to-end from real rewards and
   the learned policy *reflects the real link* — in a poor CFO window (delivery ~2/27) it does
   not learn to transmit (transmitting rarely cuts AoI), which is correct. A good window /
   shared 10 MHz clock makes delivery reliable so the policy converges to transmit.
   - **DQPSK unlocks clean learning without a shared clock.** Coherent QPSK delivered only
     ~17% (CFO spins the constellation), so `P(transmit|queued)` stayed stuck ~0.5 and the
     agent couldn't learn. Switching to **differential DQPSK** (data on the phase *difference*,
     so a drifting CFO cancels between consecutive symbols; PLL bypassed) raised single-shot
     delivery to ~64–83% — enough signal for the RL: on the radio `P(transmit|queued)` climbed
     **0.51 → 0.76**, cumulative-delivery slope steepened, reward dips shallowed. The stack now
     defaults to DQPSK + 125 B packets. (Differential+FEC mis-frames at very small chunks like
     32 B → CRC always fails; 125 B is the validated size — a small-chunk framing bug to fix.)
4. **Next:** a q-ALOHA / trained-policy comparison, more radios for real multi-agent
   collisions, and a shared clock for clean reward. Model saved to `--out` for reuse/eval.
4. **(Optional) online learning on hardware** and/or **more radios** for true multi-agent
   collisions; add the AoI side channel if using neighbour observations.

**Bottom line on your question:** don't retransmit for MARL. Send **one burst**, treat
**ACK = success / timeout = collision** as the reward, and let the **learned policy** decide
when to retry. Keep reliable ARQ only for bulk data (the MNIST path); add a single-shot mode
(`--max-attempts 1`, already there) and a persistent per-frame-ACK AP (small addition) for
the random-access path.

## 8. Multi-agent contention (the regime where MARL beats q-ALOHA)  ⭐

Single agent has no collision partner, so "always transmit" is optimal and a learned
policy can only *match* a fixed-p baseline (validated: MARL ≈ q-ALOHA p=1.0 on the warm
QPSK link; on throughput it can even hurt itself by over-transmitting). The MARL payoff
needs **≥2 agents contending for one AP**. Code (ready; mock-validated, hardware-ready):

- `python/marl_multi_env.py` — `MultiAgentRAEnv` (N agents, one shared medium). A slot
  with **1** transmitter fires a **real** burst (real ACK/loss); **≥2** transmitters ⇒
  **collision** (no ACK to anyone). Channels: `MockMultiChannel` (offline) and
  `MultiRealChannel` (N `WarmSource`s → 1 AP; logical collision by default, `physical=True`
  fires overlapping bursts). Each agent observes **all** agents' AoI + own queue + channel-busy.
- `python/marl_multi_train.py` — **independent A2C**, one actor+critic per agent.

**Key finding (mock):** naive independent learners under the AoI reward *fail* to
coordinate — a transmit-into-collision looks identical to a defer (no delivery either
way), so there's no gradient to back off and both agents spam (collision rate *climbs*
0.68→0.88). Adding an explicit **collision penalty** (`--coll-penalty`, distinguishing
"collide" from "defer") fixes it: collision rate **drops 0.68→0.08**, per-agent
`P(transmit)` settles toward **1/N** (the optimal symmetric rate), and with 4 agents MARL
gets the **best throughput of any policy** (0.347/slot) *without being told the right rate*
— while a mis-tuned fixed-p ALOHA starves (p too low) or melts down (p=0.5 → 70% collisions).

**Run.** Offline today: `marl_multi_train.py --mock --agents 4 --steps 800 --coll-penalty 0.5
--compare-aloha "0.15,0.25,0.5"`. On hardware (2 agents + 1 AP, when the 3rd USRP is in):
start `marl_phy.py ap --scheme QPSK ...`, then `marl_multi_train.py --tx-args serial=A
--tx-args serial=B --scheme QPSK --steps 300`. Commands in `../../python/README.md`.

### 8b. Decentralized (each TX on its own node) — the deployment-faithful version

`marl_multi_train.py` is a single-process orchestrator (all agents in one loop) — a
one-host convenience, NOT how RA deploys. Real agents are **independent nodes** sharing
only the medium. The decentralized stack:

- `agent_node.py` — one process per agent; **local-only** obs `[own AoI, own queue,
  sensed busy]` (fixed 3-dim, agent needn't know N); own A2C + online learning.
- `mock_medium.py` — offline shared-channel+AP server (validate with no radio).
- `ap_multi.py` — multi-agent AP: the C++ ACK server is **single-client**
  (`net.hpp: listen(srv,1)`), so ACK routing is done in Python — the C++ sink decodes,
  and a Python multi-client server ACKs the agent whose **id is payload byte 0**. A
  collision decodes nothing → no ACK → the agent times out (its only collision signal).

**Design choices (validated with the user):** local-only observation (no AP beacon /
global state) and one-host / 3-USRP test topology. A node **cannot observe collisions**,
only its own no-ACK, so coordination is learned from **carrier-sense (busy) + a penalty
on its own wasted transmits** — i.e. learned CSMA. Mock-validated: 2 agents converge to
the ALOHA-optimal rate. Caveat: a *simultaneous-slot* mock can't model listen-before-talk,
so the busy bit is uninformative offline — real RF sensing on hardware is what makes CSMA
work. **Pending hardware:** `ap_multi._decode_stream` (parse the sink's per-burst output
for the agent-id); the ACK routing around it passes a radio-free self-test.
