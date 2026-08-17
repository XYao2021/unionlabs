# Cross-Testbed Federated Learning — Experiment Design

**Goal.** Demonstrate the platform claim: *train a model collaboratively across several
independently-owned SDR testbeds, then deploy and evaluate it on a testbed that
contributed nothing to training.* The radio is inside the training loop, not just
alongside it.

This document is the design rationale and the build plan. It assumes familiarity with
`APPLICATIONS_INTRO.md` (§1 substrate, §2.4 Federated Learning) and
`EXPERIMENT_GUIDE.md` §1B.

---

## 0. The thesis, and the trap

The naive experiment — FedAvg on MNIST across three sites — **proves nothing**. MNIST is
site-invariant: the model trained at site A is already optimal at site C, so
"cross-testbed deployment" is vacuous, and the radio is reduced to a lossy TCP
substitute. A reviewer correctly asks why the USRPs are in the picture at all.

The claim the platform can uniquely support is:

> **The learning task is RF-native, so each testbed's data distribution is genuinely and
> irreducibly different — and the federated model still transfers to an unseen site.**

Two consequences drive every design decision below:

1. **Site heterogeneity *is* the non-IID.** Most FL papers manufacture non-IID by
   artificially partitioning labels. Here it comes for free from real physics:
   different multipath, noise floors, interference, LO quality, PA nonlinearity, and
   hardware models. This is not reproducible in simulation. It is the differentiator.
2. **The radio must carry the training, and that cost must be measured.** The headline
   metric is not accuracy-vs-round but **accuracy vs. cumulative airtime**.

MNIST FedAvg (`fl.py`) remains valuable — as the **plumbing regression test**, run before
every hardware session. It is not a scientific result.

---

## 1. Architecture — two tiers

Do not run one flat FedAvg across all radios at all sites. Use **hierarchical FL**
(HierFAVG-style), which maps exactly onto "each testbed has its own few USRPs":

```
  ┌──────────── Testbed A ────────────┐   ┌──────────── Testbed B ────────────┐
  │  USRP c0 ──┐                      │   │  USRP c0 ──┐                      │
  │  USRP c1 ──┼─RF (OTA)─> site agg  │   │  USRP c1 ──┼─RF (OTA)─> site agg  │
  │  USRP c2 ──┘            (N210 RX) │   │  USRP c2 ──┘            (N210 RX) │
  └───────────────────┬───────────────┘   └───────────────┬───────────────────┘
                      │  τ local RF rounds per WAN round  │
                      └──────────► global server ◄────────┘
                             (WAN / TCP, site-aggregated only)
                                        │
                                        ▼
                          ┌──────── Testbed C (held out) ────────┐
                          │  deploy + evaluate, never trained on │
                          └──────────────────────────────────────┘
```

| Tier | Link | Carries | Already supported by |
|---|---|---|---|
| 1 — intra-testbed | **over the air**, USRP → USRP | client top-k sparse deltas | `fl.py --uplink wireless --downlink tcp` |
| 2 — inter-testbed | WAN / TCP | site-aggregated deltas, every τ rounds | new: site aggregator relay |

**Why hierarchical, not flat:**

- The radio is in **every** local round — the "communication inside training" requirement
  is structural, not decorative.
- The WAN sees only site aggregates → the bandwidth and privacy argument writes itself.
- τ (local RF rounds per WAN round) becomes a clean experimental knob trading airtime
  against cross-site consensus.
- It survives the practical reality that sites are not time-synchronized. `slot_sync.py`
  is intra-site only, which is correct and sufficient; tier-2 rounds are logically synced
  by the global server with a deadline.

### 1.1 The airtime budget is the point

Because tier 1 is a real 1 Msym/s link, communication cost is measurable, not modelled.
The existing top-k sparsification with error feedback in `fl.py` (`--compress-ratio`,
~20 KB vs ~200 KB per delta) becomes a first-class variable:

- Sweep `--compress-ratio ∈ {0 (full), 0.2, 0.05, 0.01}`.
- Log per round: bytes over the air, airtime seconds, ARQ retransmissions, delivery rate,
  round wall-clock.
- Plot **accuracy vs. cumulative airtime seconds**, not vs. round index.

This single plot is the justification for putting the radio in the loop, and it inverts
the usual FL-compression result: aggressive sparsification can *win* in wall-clock even
when it loses per-round, because the channel — not the gradient — is the bottleneck.

---

## 2. What to learn

### 2.1 Recommended primary — a learned receiver front-end

A small neural **demapper / equalizer** mapping post-FFT (OFDM) or post-matched-filter
(SC) symbols to LLRs, inserted between `ofdm_demod` and `fec_decode_soft` on the `pyphy`
block API (`APPLICATIONS_INTRO.md` §1.2).

```
RX: capture -> sync -> ofdm_demod -> [ NEURAL DEMAPPER ] -> fec_decode_soft -> CRC
                                     ^ federated across testbeds
```

Why this task:

| Property | Why it matters here |
|---|---|
| **Labels are free** | The transmitted bits are known. Data collection = captures. No annotation, no RL variance, stable supervised training. |
| **Deploy metric is in bits** | Post-FEC BER / goodput at the held-out site vs. the existing 1-tap LS equalizer. Not an accuracy number on a borrowed vision dataset. |
| **Sites genuinely differ** | Delay spread, per-device LO phase noise, PA nonlinearity, local interference. And the difference is *dialable* (cable vs OTA, room vs corridor, N210 vs B210). |
| **Small model** | Fits the sparse uplink budget; a round is seconds, not minutes. |
| **No C++ churn** | Lives entirely on the block API. The `sdr_system` decode path is untouched. |

The comparison target is your own existing estimator, which is a fair and unarguable
baseline: does the federated neural demapper beat 1-tap LS **at a site it never saw**?

### 2.2 Recommended stretch — close the loop

Add a **link-adaptation policy** (observation → scheme / FEC / gain) that is itself
federated, and let the *current* policy govern the uplink that carries the *next* round's
model updates. The model being trained improves its own transport.

This is the result that is only obtainable on hardware: round latency and
delivered-bytes-per-round improve as training proceeds. Run in two modes so the science
stays clean:

- **Frozen transport** — fixed PHY config for the whole run. Isolates learning quality.
- **Closed loop** — the policy governs its own uplink. Isolates the self-referential gain.

Report both. The delta between them *is* the closed-loop effect.

### 2.3 Companion / fallback — interference & spectrum classification

Cheap, robust, and it doubles as the heterogeneity evidence: classify channel occupancy /
interference type from short captures (`channel_sense.py`, `freq_scan.py` already collect
the raw material). Each site's interference is authentically different. Use this if the
receiver-front-end result comes out noisy — it will still carry the cross-testbed claim,
just with a passive rather than an in-loop metric.

### 2.4 Explicitly not recommended as the headline

- **MNIST / CIFAR FedAvg** — plumbing test only (§0).
- **RF fingerprinting** — the label space is the device set, so it does not survive being
  deployed at a site with different devices.
- **MARL policy transfer** — attractive, but agent-count and topology differences across
  sites make the transfer claim hard to state cleanly. Revisit after the supervised
  result lands.

---

## 3. Evaluation protocol

### 3.1 Leave-one-testbed-out (LOTO), rotated

With sites A, B, C: train on {A,B}, evaluate on C. Rotate so every site is held out once.
Report mean ± spread over the three folds — one fold is an anecdote.

| Condition | Trained on | Evaluated on | What it isolates |
|---|---|---|---|
| Local-only | C (small local data) | C | "just collect data at every site" — the do-nothing baseline |
| Single-site transfer | A | C | does one site alone generalize? |
| **Federated, zero-shot** | **{A,B} federated** | **C** | **the platform claim** |
| Federated + k-shot | {A,B} + k batches at C | C | the practical answer (personalization) |
| Centralized pooled | A+B+C pooled | C | upper bound |
| Oracle | C (full data) | C | ceiling |

The story you want: *zero-shot federated ≫ single-site transfer*, and *federated + k-shot
≈ oracle with k ≪ full data*.

### 3.2 Step zero — prove the sites actually differ

**Do this before anything else.** If A and B are statistically the same room, every number
in §3.1 collapses and the experiment is unfalsifiable.

Define a standardized **site signature** and run it at each testbed with identical
commands from the identical container:

| Component | Tool | Output |
|---|---|---|
| Noise floor + occupancy | `freq_scan.py --start 902 --stop 928 --step 1` | band power profile |
| Interference over time | `channel_sense.py` / `power_monitor.py` | busy fraction, burst statistics |
| Link quality over time | `ber_monitor.py --minutes 30` | pre/post-FEC BER time series, detection + delivery rate |
| EVM vs gain envelope | `range_ber.py` sweep | per-scheme operating curve |
| CFO distribution | sync logs | LO offset spread across bursts |

Then compute a distance between site signatures and **show the transfer gap correlates
with it**. That is a second, independent scientific result, and it is the figure that
justifies the entire multi-site apparatus.

---

## 4. Three things that silently break this

### 4.1 Hardware leakage — the fake-transfer failure

The N210 gain range is 0–31.5 dB; the B210 goes to ~89 dB (`APPLICATIONS_INTRO.md` §2.5).
If raw gain index or raw RSSI is a model input, the model memorizes *hardware*, and the
"cross-testbed transfer" result is an artifact.

**Mitigation — a device-invariant feature contract.** Inputs must be relative and
calibrated: estimated SNR, EVM, power *relative to the locally calibrated noise floor*
(`calibrate_floor()` already exists in `channel_sense.py`). Never raw gain, never raw
absolute power. Write the contract down once, version it, and ship it in the container.

**Test for it:** train and evaluate within one site but across *device types*. If accuracy
drops, the features are leaking hardware identity.

### 4.2 Schema drift across sites

FedAvg averages parameter vectors; if two sites define their observation vector, label
set, carrier, or waveform set differently, it is averaging incompatible models and will
fail in a way that looks like "FL doesn't work."

**Mitigation.** One pinned config object (`sites.yaml`) covering: carrier frequency,
sample/symbol rate, waveform, scheme set, FEC set, feature vector definition and order,
label/action set, model architecture and init seed. The global server should *reject* a
site whose config hash differs.

### 4.3 The WAN rendezvous

Sites are behind different institutional firewalls. This routinely costs a week and is
never the interesting part.

**Mitigation.** Settle it in Phase 2 (below), not on travel day: a cloud VM as the global
server, or a shared VPN/ssh reverse tunnel. Also implement **deadline-based rounds** —
the global server aggregates whatever arrived by the deadline and marks the straggler,
rather than blocking. Straggler tolerance is a legitimate result to report, not a bug.

---

## 5. Metrics to log

**Learning**
- Held-out-site metric: post-FEC BER / goodput (primary task), or accuracy (classification)
- Per-fold LOTO table (§3.1), mean ± spread

**Communication (per round, per tier)**
- Bytes over the air; airtime seconds
- ARQ retransmissions; delivery rate; detection rate
- Round wall-clock; straggler count and lateness at tier 2

**Joint (the headline plots)**
- Accuracy / BER vs. **cumulative airtime** — for each `--compress-ratio`
- Transfer gap vs. site-signature distance
- Closed-loop vs. frozen-transport round time, over rounds

**Heterogeneity**
- Per-site signature (§3.2), plotted side by side

---

## 6. Staging — de-risk before travelling

| Phase | Setup | Proves | Status |
|---|---|---|---|
| **0** | All-TCP, one host, MNIST | protocol correctness | **done** (`fl.py --uplink tcp --downlink tcp`, 0.92 in 12 rounds) |
| **1** | One testbed, RF uplink, MNIST | radio in the loop | command-ready (`EXPERIMENT_GUIDE.md` §1B Step 1) |
| **2** | **Two *virtual* sites at one location** — different rooms / carriers / antennas, separate subnets, separate site aggregators, real tier-2 TCP hop | the entire hierarchical protocol, the site-signature machinery, the WAN rendezvous, the LOTO harness | **build this next** |
| **3** | Real cross-site, ≥2 remote testbeds + 1 held out | the actual claim | after Phase 2 is green |

Phase 2 is the highest-value step: it exercises 100% of the software and ~80% of the
methodology with zero travel and zero coordination cost. Only tier-2 latency and true
RF-environment diversity are missing.

**If only two physical testbeds are available**, the held-out site can be manufactured:
split one testbed into two conditions (different room / carrier / antenna / device type),
train on two conditions and hold out the third. State this honestly; it is still a real
domain gap.

**Ship the same container everywhere.** You already have `Dockerfile` / `DOCKER.md`. If
sites run different Python or UHD versions, "the sites differ" stops meaning RF.

---

## 7. Build list

Ordered by dependency. Everything reuses existing pieces; nothing touches the C++ decode
path.

### 7.1 Site characterization
- `python/site_signature.py` — runs the §3.2 sweep and emits one JSON per site in a fixed
  schema. Wraps `freq_scan`, `channel_sense`, `ber_monitor`, `range_ber`.
- `python/site_distance.py` — pairwise signature distance + the heterogeneity figure.

### 7.2 Data collection
- `python/collect_phy_dataset.py` — the standardized capture harness: fire known payloads,
  record (post-demod symbols, known bits, features, site id, device id) to a common
  on-disk schema. This is the artifact that actually makes cross-testbed possible; write
  the schema before the model.
- `sites.yaml` — the pinned config contract of §4.2, with a hash the server validates.

### 7.3 Hierarchical FL
- `python/fl_site.py` — the **site aggregator**: tier-1 server (existing `fl.py server`
  role, `--uplink wireless`) + tier-2 client to the global server over TCP. τ local rounds
  per WAN round.
- `python/fl_global.py` — the global server: deadline-based rounds, config-hash validation,
  per-site straggler accounting, per-round comms logging.
- Extend `fl_core.py` with weighted multi-tier aggregation (site weights by client count /
  sample count).

### 7.4 The task
- `python/phy_model.py` — the neural demapper: numpy forward/backward in the `TinyMLP`
  style so it stays torch-free and runs in the container, or a torch variant behind a
  flag. Feature extraction per the §4.1 contract.
- `pyphy` insertion point: a callable slotted between `ofdm_demod` and `fec_decode_soft`,
  falling back to the existing 1-tap LS estimator when disabled (that fallback *is* the
  baseline).

### 7.5 Evaluation
- `python/loto_eval.py` — runs the §3.1 condition matrix over collected datasets and emits
  the results table and the accuracy-vs-airtime plots.

---

## 8. Open questions to settle before Phase 3

1. **How many testbeds, and are they genuinely geographically separate?** Three is the
   minimum for a rotated LOTO; two forces the manufactured-third-condition variant (§6).
2. **USRP inventory per site.** The N210-is-RX-only / B210-is-TX-only constraint shapes
   which tier-1 topologies are possible at each site.
3. **Is the carrier band clean and legal at every site?** Run `freq_scan.py` at each before
   committing to 915 MHz across the board — the pinned config must work everywhere.
4. **Who hosts the global server**, and is inbound TCP reachable from every site?
5. **Frozen vs. closed-loop as the headline** — recommend leading with frozen (clean) and
   presenting closed-loop as the platform demonstration.

---

## See also

- `APPLICATIONS_INTRO.md` — §1 substrate, §2.4 Federated Learning, §4 the archetype seam
- `EXPERIMENT_GUIDE.md` §1B — the existing single-site FL runbook
- `python/fl.py`, `python/fl_core.py` — the FedAvg + compression implementation this builds on
- `DOCKER.md` — the container to ship to every site
