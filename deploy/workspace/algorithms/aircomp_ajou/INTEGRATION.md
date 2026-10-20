# STC-AirComp ↔ SDR PHY — integration design note

How the STLC over-the-air-computation scheme (Lee–Lee–Jung, IEEE WCL vol. 15, 2026) hooks
onto the USRP PHY in this repo. Unlike `marl_ra`, this application does **not**
decode one device's packet — it reads a **function (sum) of many devices transmitting at
once**. The radio is not a bit-pipe here; the **wireless medium itself is the adder**, and
our job is to shape the transmit side so the access point's simple combine returns the
aggregate.

**Status: forward-looking design.** Nothing here is built yet (contrast the "DONE" markers
throughout the MARL note). This document is the plan; §7 lists what blocks the paper's exact
equations, and §8 the phased build.

---

## 1. What maps to what

| STLC-AirComp (the paper) | SDR system (integration target) |
|---|---|
| sensor measurement `v_i` (a scalar) | the app's `next_value()` — float from the algorithm |
| quantize `v_i` → bits | pure-Python `quantize()` (app logic) |
| bit-slice → low-order symbols | `bit_slice()` + **`pyphy.modulate(..., "BPSK"/"DBPSK")`** |
| local CSI `h_i` at the sensor | **`estimate_channel()`** — expose the existing pilot estimator (sounding) |
| STLC precode over 2 time slots | **`stlc_encode(sym, h_i)`** — new numpy block |
| simultaneous, time-aligned TX from N sensors | **`slot_sync.py`** (reused) + **`Radio.transmit()`** |
| superposition in the air | the **real RF channel** — signals add at the AP (this *is* the compute) |
| AP with ≥2 RX antennas (receive diversity) | **`Radio.capture2()`** — new synchronized 2-channel RX |
| CSI-free linear combine | **`stlc_combine()`** — new numpy block |
| de-map / de-bit-slice / de-quantize → sum | `dequantize()` + `aggregate()` (app logic) |

The loop in spirit: `each sensor: quantize → STLC-encode with its own CSI → fire in the
shared slot`; `AP: capture 2 antennas → combine → dequantize → the aggregate`. Only the
2-channel capture is a genuine engine addition; everything else is composed on the `pyphy`
blocks (§1.2 of `../APPLICATIONS_INTRO.pdf`).

---

## 2. The paradigm mismatch — and why it is *easier* here, not harder  ⭐

This is the central point. **The reliable link (App 1) spends its whole PHY avoiding what
this application depends on.**

- **App 1 (MARL/FL):** two simultaneous transmitters are a **collision** — a failure the
  CRC rejects and the policy learns to avoid. The receiver runs the full pipeline (sync, CFO,
  phase, demod, FEC, CRC) to pull *one* device's bytes out cleanly.
- **STC-AirComp:** N simultaneous transmitters are the **entire point** — their superposition
  *is* the sum being computed. The receiver does **almost nothing**: estimate CSI, apply a
  fixed CSI-free linear combine, de-quantize. No FEC, no ARQ, no per-device equalization.

So most of App 1's heavy machinery is **off this application's critical path**. What replaces
it is a *transmit-side* burden (each sensor pre-shapes its signal) and two new requirements:
**multi-antenna reception** and **coherent-enough superposition**. STLC is precisely the tool
that discharges both cheaply:

- **STLC = the transmit-side dual of Alamouti.** In a space-time *block* code the transmitter
  is blind and the *receiver* does the channel-aware combining. In a space-time *line* code
  the **transmitter** uses its **local** CSI `h_i` to precode across 2 time slots so the
  receiver gets full diversity from a **trivial, CSI-free combine** — the channel terms cancel
  in the sum by construction. Diversity order is `2M` for `M` receive antennas.
- **Only local CSI per sensor** (each knows its own channel, not the others') → low overhead,
  which is why it suits massive IoT. And because each sensor pre-compensates its *own* channel,
  the AP's combiner sees the N contributions **aligned**, so the combine output ≈ the **sum**
  of the sensors' symbols — the aggregate falls out directly.

### The one hard requirement: coherent-enough superposition on free-running LOs

For the superposition to compute a *clean* sum, the sensors' symbols must (a) **arrive
time-aligned** and (b) not have their relative carrier phases drift unmodeled within the
2-slot codeword. Our radios have **independent LOs** (see the free-running-LO note in the
intro), so this is the genuine risk. Two things de-risk it:

1. **DBPSK is differential** — data rides on the phase *difference* between symbols, so a
   slowly drifting CFO cancels and **no absolute phase reference is needed**. This likely
   removes the shared-10 MHz-clock requirement that coherent AirComp would impose. (Exactly
   the trick that unlocked MARL learning — see MARL `INTEGRATION.md` §7.3.)
2. **Time alignment** still must be enforced: `slot_sync.py` aligns the *slot*, but AirComp
   wants **sub-symbol** alignment. A shared 10 MHz / PPS reference (OctoClock/GPSDO) across
   sensors + AP is the clean fix; without it, keep the frame short and the symbol rate modest
   so residual timing skew stays within the cyclic tolerance. **This is the first thing to
   measure on hardware.**

**Summary:** the receiver is nearly free; the work moves to (i) a synchronized 2-antenna
capture and (ii) getting the sensors coherent-enough — for which DBPSK buys us a lot.

---

## 3. The scheme in signal terms (schematic — exact matrices TBD, §7)

Canonical STLC is 1 TX antenna, `M = 2` RX antennas, 2 time slots, carrying 2 symbols
`s1, s2`. The transmitter, knowing its channels `h1, h2` to the two AP antennas, sends a
precoded pair over slots `t = 1, 2`:

```
sensor i:
  s1, s2   = its two bit-sliced BPSK/DBPSK symbols
  x_i[1], x_i[2] = STLC_precode(s1, s2 ; h_i1, h_i2)   # conjugated-CSI precoding, unit power
  transmit x_i[1] in slot 1, x_i[2] in slot 2, IN THE SHARED SLOT with all sensors
```

At the AP, antenna `m` observes the **superposition** over both slots:

```
r_m[t] = Σ_i  h_im · x_i[t]  +  noise            # the air sums over i — the compute
ŝ1, ŝ2 = STLC_combine(r_1[1], r_1[2], r_2[1], r_2[2])   # fixed, CSI-FREE linear combine
```

Because each sensor pre-shaped with its **own** `h_i`, the combiner output aligns the N
contributions, so `ŝ1 ≈ Σ_i s1_i` and `ŝ2 ≈ Σ_i s2_i` — the **per-slice sums**. De-quantizing
the recovered sums (accounting for the bit-slice place values) yields `Σ_i v_i`, the target
aggregate. The precise `STLC_precode` / `STLC_combine` coefficients (and the normalization,
the quantizer, and the bit-slice weighting) come from the paper — see §7.

---

## 4. Radio count and topology

Real over-the-air summation needs **≥2 simultaneous transmitters + 1 multi-antenna AP**:

- **AP:** one radio with **≥2 coherent RX channels** — a **B210 (2×2)** or **X310 (2×2)**.
  The two RX channels *must share the same LO* (same device, one `multi_usrp`) so the
  receive-diversity combine is meaningful. This is why the AP cannot be the RX-only N210
  (single channel) used in App 1.
- **Sensors:** ≥2 transmitters, one antenna each — the two **B210s** (`30CD424`, `30CD3F7`).
  For a first bring-up a single sensor validates the TX chain + combine mechanics (no sum yet);
  the aggregate needs ≥2 firing in the same slot.
- **Sync reference:** ideally a shared 10 MHz/PPS distribution to the sensors + AP (`--ref
  external`). DBPSK relaxes the *phase* need; timing still benefits from a common reference.

Interim, radio-light path (mirrors MARL §3): validate the full DSP **radio-free in loopback**
(sum two locally-generated STLC bursts through a simulated 2-antenna channel), then move to RF.

---

## 5. What to build

**Engine (C++ / `pyphy`, the one real addition):**

- `Radio.capture2(n) -> (ant0, ant1)` — a synchronized 2-channel UHD RX stream (single
  `multi_usrp`, `rx_subdev "A:A A:B"` or `"A:0 B:0"`, one stream command, aligned time-spec).
  This is receive diversity; without it there is no `M = 2`.

**Blocks (numpy, small):**

- `stlc_encode(s1, s2, h) -> x[2]` — sensor-side 2-slot precoding from local CSI `h`.
- `stlc_combine(r_ant0, r_ant1) -> (ŝ1, ŝ2)` — AP-side fixed CSI-free linear combiner.
- `estimate_channel(rx, pilot) -> h` — expose the existing preamble/pilot channel estimator
  as a block (the sounding that gives each sensor its `h_i`).

**Application logic (pure Python):**

- `quantize(v) / dequantize(ŝ)` — the digital-AirComp value↔bits map.
- `bit_slice(bits) / unslice(...)` — split into low-order-modulated slices with their place
  weights.
- `aggregate(sums) -> Σ v_i` — recombine the per-slice sums into the final aggregate.

**Reused as-is:** `pyphy.modulate/demodulate` (BPSK/DBPSK), `python/slot_sync.py`
(simultaneous slotting), `Radio.transmit` (raw TX), the pilot estimator internals.

None of this touches the `sdr_system` decode path — it all lives on the block API, which is
exactly why the block API was built.

---

## 6. Reward / output reconciliation

Where MARL's "output" is an ACK bool (the reward) and FL's is a delivered vector, this
application's output is a **computed scalar (or vector) aggregate** with an **error**. The
natural quality metric is the paper's: **normalized MSE** of the recovered sum versus the
true `Σ v_i`. So the app reports `(aggregate_estimate, nmse)`, not `(bytes, ack)`. This is the
**third archetype — compute/aggregation** — and it is the reason the abstraction layer must
grow beyond "payload + ACK" (see `../APPLICATIONS_INTRO.pdf` §4): a `ComputeApp` contributes
`next_value()` and reads back `on_aggregate(estimate)`, with the PHY orchestrating the
simultaneous multi-node TX + diversity-combined RX under the hood.

---

## 7. Open questions (blocking a faithful build)

These need the paper's equations before the blocks can be written exactly:

1. **CSI acquisition path** — TDD reciprocity (each sensor estimates `h_i` from an AP downlink
   pilot) or AP-estimates-then-feeds-back? This decides whether the AP must *transmit* (it can,
   as a B210) and how the sounding block is wired.
2. **Number of AP receive antennas `M`** (diversity order `2M`) — 2, or more? Sets `capture2`
   vs a general `captureM`.
3. **Exact STLC precoding matrix and combiner** — the 2-slot coefficients and normalization
   (`STLC_precode` / `STLC_combine` in §3).
4. **Quantization + bit-slicing map** — levels, slice count, place weights, and how BPSK vs
   DBPSK carry a slice (differential across slots vs across slices?).
5. **Aggregate estimator + power/gain alignment** — how the AP de-normalizes the combine output
   to an unbiased `Σ v_i` (per-sensor power control? a known scaling?), and the NMSE model.
6. **Sync assumption in the system model** — does the paper assume symbol-level or perfect
   sync? Sets how hard we must chase timing on the free-running rig (§2).

---

## 8. Phased plan

1. **DSP loopback (radio-free) — first.** Implement `stlc_encode` / `stlc_combine` /
   `quantize` / `bit_slice` and validate a **2-sensor → 1-AP sum through a simulated
   2-antenna AWGN channel**, 0 error at high SNR, NMSE curve vs SNR matching the paper's
   shape. This is the analogue of every other chain we validated radio-free first
   (`phy_flow_example.py`, `chain_evidence`).
2. **`capture2` engine block.** Add the synchronized 2-channel RX to the `Radio` wrapper;
   verify two coherent captures of a single known TX (equal timestamps, stable relative phase).
3. **Single-sensor RF.** One B210 sensor → 2×2 AP: STLC-encode, transmit, `capture2`, combine,
   recover the *single* sensor's value (no sum yet). Proves the TX chain + diversity combine +
   sounding on real hardware. Measure the timing/phase coherence budget here (§2).
4. **Two-sensor RF sum.** Add the second B210, `slot_sync` them into the same slot, and
   recover `v_1 + v_2`. Report NMSE. Try DBPSK first (no shared clock), then BPSK + `--ref
   external` if coherence demands it.
5. **Scale + compare.** More sensors; compare digital-STLC NMSE against a plain
   analog-superposition baseline (the paper's claim is lower NMSE than conventional analog and
   digital AirComp).

**Bottom line:** don't build a decoder — build a *transmit shaper* and a *2-antenna combiner*.
The medium does the addition; STLC + DBPSK make it survive our free-running LOs; the only real
engine work is the synchronized 2-channel capture. Get the paper's precoding/combiner/quantizer
equations (§7) and steps 1–4 are a few small `pyphy` blocks plus one UHD addition.
