# STC-AirComp — Space-Time Coded Over-the-Air Computation

The **second application** on the shared SDR PHY, and the first that is *not* a
decode-and-forward link. Instead of recovering one device's bytes, the access point reads
a **function (the sum / average) of many devices transmitting at once** — over-the-air
computation (AirComp) — using a **Space-Time Line Code (STLC)** for **receive diversity**.

> Reference: Y.-S. Lee, K.-H. Lee, and B. C. Jung, "Space-time coded over-the-air
> computation with receive diversity for 6G massive IoT networks," *IEEE Wireless
> Communications Letters*, vol. 15, pp. 31–35, 2026. (Companion: "Digital Over-the-Air
> Computation with Space-Time Processing for 6G Massive IoT Networks.")

**Status:** **phase-1 DSP built and validated radio-free** (design note `INTEGRATION.md` §8).
`stc_core.py` implements the STLC precoder/combiner + digital-AirComp bit-slicing, and
`stc_aircomp.py` reproduces the paper's **NMSE-vs-SNR** curve (→ `results/stc_aircomp/`),
showing STLC's diversity removes the single-antenna error floor. Also available through the
uniform API as the **compute archetype** (`algorithms/stc_aircomp/`, `--role aircomp`):

```
python3 algorithms/stc_aircomp/stc_aircomp.py --sensors 8 --bits 4   # NMSE-vs-SNR figure
./run.sh --algo stc_aircomp --role aircomp --agents 8 --snr-db 15 --steps 200 # uniform-API port
```

Remaining for hardware (phases 2–5): the synchronized 2-channel RX capture (`capture2`) and
the on-air bring-up. See `INTEGRATION.md` for the plan and open questions.

---

## How it differs from Application 1 (the reliable link)

| | Reliable Digital Link (MARL / OSU / FL) | **STC-AirComp** |
|---|---|---|
| Senders active at once | 1 (per slot) | **N simultaneously** |
| Air combines signals | avoided (collisions are failures) | **exploited** (superposition *is* the compute) |
| Receiver output | the delivered bytes + ACK | **an aggregate** (the sum) |
| Receiver work | full sync + demod + FEC + CRC | CSI + a fixed linear combine |
| Modulation | BPSK…256-QAM, differential | **BPSK / DBPSK** (low order) |
| FEC / ARQ | yes | **none** on this path |

Where App 1 asks *"did this device's bytes arrive?"*, STC-AirComp asks *"what is the
aggregate of all devices' measurements?"* — the quantity a massive-IoT fusion center
actually wants, computed without a per-device access bottleneck.

## The scheme

**Each sensor** (single TX antenna, knows only its own channel):

1. **Quantize** its measurement to bits.
2. **Bit-slice** into low-order symbols — **BPSK / DBPSK**.
3. **STLC-encode** across **2 time slots**, precoding with its *own* channel estimate
   (local CSI at the transmitter).
4. **Transmit**, time-aligned with the other sensors → signals **superimpose** at the AP.

**Access point** (≥ 2 receive antennas):

5. **Capture** the 2 slots on the diversity antennas.
6. **Linear-combine** — a *fixed, CSI-free* STLC combiner → the **superimposed (summed)**
   symbols, with full diversity.
7. **De-map / de-bit-slice / de-quantize** → the **aggregate**.

## Why STLC (and why the receiver is nearly free)

STLC is the transmit-side dual of the Alamouti space-time *block* code: the **transmitter**
uses **local** CSI to precode so the **receiver** gets full diversity from a **simple,
CSI-free linear combine**. Consequences that suit our hardware:

- **Only local CSI at each transmitter** — no global CSI, low overhead (Bang Chul Jung's
  group's signature technique).
- **The receiver does almost no data processing** — just CSI sounding + the fixed combine +
  de-quantize. No per-device equalization, FEC, or ARQ.
- **DBPSK is differential**, so the AP needs no absolute phase reference — which likely
  removes the shared-10 MHz-reference requirement our free-running LOs would otherwise
  impose. (Time alignment across sensors is still required.)

## What we already have vs. what to build

| Need | Status |
|---|---|
| BPSK / DBPSK modulation | **have** — `pyphy.modulate` |
| Custom TX preprocessing (quantize, bit-slice, STLC) | **have** — compose on `pyphy` blocks |
| Simultaneous, slot-aligned TX from N sensors | **have** — `../marl_ra/slot_sync.py` + `Radio.transmit` |
| STLC encode / combine, quantize / bit-slice, sum reconstruction | **built** — `stc_core.py` (validated radio-free) |
| NMSE-vs-SNR evaluation + uniform-API port | **built** — `stc_aircomp.py`, `algorithms/stc_aircomp/` |
| Local CSI estimation (channel sounding) | expose — wrap existing pilot estimator |
| **Receive diversity (≥2 RX antennas)** | **build** — `capture2` (B210 / X310 are 2×2) |

The realistic work: **one engine addition** (a synchronized 2-channel RX capture) **+ three
small numpy blocks** + wiring the sounding step. None of it touches the `sdr_system` decode
path — it all lives on the `pyphy` block API. Details and the validation plan are in
`INTEGRATION.md`.

## See also

- `../EXPERIMENT_GUIDE.pdf` (or `.md`) — step-by-step commands to run every application (radio-free + hardware); this app's planned bring-up sequence is in §2.
- `../APPLICATIONS_INTRO.pdf` — both applications introduced side by side.
- `../marl_ra/` — Application 1 (the reliable-link control archetype).
- `../../../../drivers/usrp/python/phy_flow_example.py` — worked `pyphy` flowgraph (the substrate this app builds on).
- `../../../../drivers/usrp/bindings/pyphy.cpp` — the block API; `drivers/usrp/bindings/build.sh` to build it.
