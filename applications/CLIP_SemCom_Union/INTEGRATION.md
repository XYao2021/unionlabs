# CLIP Semantic Communication ↔ SDR PHY — integration design note

How the CLIP semantic-communication scheme (arXiv:2507.08873) hooks onto the USRP PHY in
this repo. The insight that makes it easy: **what crosses the air is a float32 vector** (the
CLIP image embedding `f_h`), so this is the **data-transfer archetype** — the same shape as
Federated Learning, which already runs over our radio. The CLIP model is the *source coder*;
our PHY is the unchanged *channel*.

**Status: built and validated radio-free** (contrast the STC-AirComp note, which is still
design-only). Real CLIP weights and the PPO optimiser are opt-in / phase-2.

---

## 1. What maps to what

| Paper (semantic comm) | SDR system (this repo) |
|---|---|
| image `x` at the base station | `semcom_core.synth_dataset` (mock) or any image folder |
| CLIP image encoder → embedding `f_h` (float32[512/768]) | `clip.encode_image(x)` (real `open_clip`, else `MockClip`) |
| the semantic symbols sent downlink | `SemComCodec.pack(f_h)` → bytes → **the PHY payload** |
| OFDMA downlink channel + wireless noise | our modem: `PhyLink` backend `pyphy` (AWGN) or `radio` (USRP) |
| user receives (noisy) embedding | `PhyLink` delivers bytes → `SemComCodec.unpack` → `f_x` |
| follow-up task, no return link (Eq. 1) | `core.classify(f_x, f_texts)` — zero-shot cosine |
| CLIP text encoder for labels `f_1..f_N` | `clip.encode_texts(labels)` — built **locally at the RX** |
| image regeneration (stable diffusion) | phase-2 stub — same transport, heavy RX decoder |
| CLIP-model + RB-allocation optimisation (PPO) | phase-2 — reuse our RL infra (see §5) |

The datapath in one line: `image → CLIP embed → pack → PHY → unpack → classify`. Only the
CLIP encode/decode is new; the transport is the proven float32-vector byte-pipe.

---

## 2. The API port (what "connect to our PHY" means here)  ⭐

`phy_port.py` is the seam, built as the **data-transfer archetype on the `SdrApp` contract**:

- **`PayloadSpec(dtype="float32", shape=(dim,))`** — the app declares its output shape/type;
  `dim` is 512 (ViT-B) or 768 (ViT-L/14).
- **`SemComCodec.pack/unpack`** — one self-describing wire format
  (`[SEMC | ver | model_id | quant | dim | sample] + body`); `quant ∈ {f32, f16, int8}`
  trades payload size for precision (the paper notes embeddings can be quantized to cut
  latency/energy — `int8` is 4× smaller than `f32`).
- **`PhyLink` — one transport, three interchangeable backends:**
  - `ideal` — lossless in-memory (logic check / mock).
  - `pyphy` — our **real modem** (`modulate`/`fec`/`soft_llr`) + AWGN at a target Es/N0,
    radio-free. **No CRC gate**, so residual bit errors corrupt the embedding floats — this
    is the semantic-comm regime and reproduces the paper's Fig. 3.
  - `radio` — the USRP link via `sdr.source_arq` / `sink_arq`, the exact byte-pipe `fl.py`
    uses (reliable ARQ).
- **`SemComTxApp.next_payload()` / `SemComRxApp.on_payload()`** — the `SdrApp` methods. The
  PHY *pulls* an embedding from the TX app and *hands* each received embedding to the RX app.

Adding this app to the (proposed) `phy_link.py` contract is therefore "implement 2 methods +
1 spec" — nothing below the seam changes. It is a clean reference for the data-transfer
archetype in the UnionLabs abstraction.

---

## 3. Two channel regimes — and which reproduces the paper

The paper's central phenomenon is that **wireless noise degrades the semantic embedding**,
so classification accuracy falls with SNR (Fig. 3). Our digital PHY offers two regimes:

1. **Reliable (default over the radio).** CRC-16 + FEC + stop-and-wait ARQ deliver the
   embedding **losslessly** — so accuracy is preserved and the *cost of noise appears as
   retransmissions / delay / energy*. This is the faithful digital-link regime and is what
   `PhyLink(backend="radio")` does today.
2. **Noisy / semantic (reproduces Fig. 3).** Send the embedding **without a CRC gate** so
   residual bit errors pass through and corrupt the floats → accuracy degrades with SNR.
   This is `PhyLink(backend="pyphy")` radio-free, and (phase-2) a single-shot
   `--max-attempts 1` on the radio with the CRC check disabled on this stream.

Validated numbers (mock CLIP, our turbo-coded QPSK modem, `--channel pyphy`):

```
 SNR dB |  BER   | accuracy
   0.0  | 0.208  |  0.000
   2.0  | 0.073  |  0.000
   4.0  | 0.001  |  0.700
   6.0  | 0.000  |  0.988      <- matches the paper's Fig. 3 shape
```

FEC matters: uncoded QPSK at 6 dB still has BER 0.034 → accuracy ~0 (any bit error in the
512-float payload wrecks it), while turbo at 6 dB is error-free → 0.988. So the embedding
**wants strong FEC** (turbo/LDPC-soft), which our PHY already has.

---

## 4. Radio topology

- **BS / TX** (the base station): a B210 (`serial=…`) transmits the embeddings.
- **User / RX**: the N210 (`addr=192.168.20.2`) receives and classifies. The RX builds the
  CLIP **text** features locally (it knows the label set), so no side channel is needed.
- Same 915 MHz, 2e6/1e6, DQPSK-or-OFDM link as FL/MARL. One image = one (possibly
  multi-chunk) ARQ message; `--chunk 125` is the validated framing size.
- Multi-user (the paper's `U=5`) = run several RX users; the BS sends each user's embedding
  in turn. True simultaneous OFDMA RB allocation is a phase-2 PHY feature (see §5).

---

## 5. Phase-2: the PPO performance optimiser

The paper's contribution beyond the datapath is a **PPO agent at the BS** that, per user,
picks the **CLIP model** `k ∈ {B/32, B/16, L/14}` and the **OFDMA resource-block allocation**
`α` to minimise task loss subject to delay (Eq. 10d) and energy (Eq. 10e) budgets. Mapping to
our stack:

- **State** `[interference per RB, user locations, free RBs]` → on our side, live link
  quality (EVM / SNR / BER from the PHY) + which streams are busy.
- **Action** `[k, α]` → choose the CLIP model (`--model`) and the transmission resource
  (rate / waveform / RB). Model selection is already a runnable knob; RB allocation needs the
  OFDMA multi-RB scheduling layer (phase-2 PHY work, overlaps with STC-AirComp's multi-node
  needs).
- **Reward** (Eq. 11) `= −(classification loss) − λ_D·(delay overrun) − λ_E·(energy overrun)`
  → computed from **real** delivered accuracy + measured delay/energy. `semcom_core.link_metrics`
  already returns bits/rate/delay/energy per (model, RB, quant); `core.accuracy` gives the loss.
- **Infra reuse**: we already have an RL environment + trainer for the radio
  (`marl_env.py`, A2C in `marl_train.py`). The semantic-comm optimiser is the same
  `observe → act → reward` loop with this action/reward — so phase-2 is "swap the env's
  action/reward", not new plumbing.

The paper reports PPO vs SAC/DQN: +40% convergence, 4× accumulated reward. Reproducing that
head-to-head is the phase-2 goal once RB allocation is exposed.

---

## 6. Phased plan

1. **Datapath + API port — DONE.** `semcom_core.py` (CLIP backend, codec, classifier) +
   `phy_port.py` (PayloadSpec, PhyLink×3, SdrApp pair) + `semcom.py` (CLI). Validated
   radio-free: mock clean accuracy 1.0; `pyphy` AWGN reproduces Fig. 3.
2. **Real CLIP.** `pip install open_clip_torch` → `--model vit-l14` (drops `--mock`); torch is
   already in the lab env. Confirm ViT-L/14 > B/16 > B/32 accuracy + robustness (paper Fig. 2).
3. **Over the radio.** `rx`/`tx` roles over B210→N210 (reliable ARQ); confirm a delivered
   embedding classifies correctly. Then the single-shot noisy stream for the on-air Fig. 3.
4. **Regeneration task.** Add the stable-diffusion RX decoder (`diffusers`) guided by `f_h`
   (heavy; optional). The transport is unchanged — only the RX follow-up task differs.
5. **PPO optimiser (§5).** Expose OFDMA RB allocation, wire the (model, RB) action + accuracy/
   delay/energy reward into the RL env, compare against SAC/DQN.

**Bottom line:** the CLIP embedding is a float32 vector, so semantic communication rides our
existing data-transfer path unchanged — the only new code is the CLIP encode/decode and a thin
codec. The port is done and reproduces the paper's noise-vs-accuracy behaviour through our real
modem; real weights and the PPO optimiser are incremental, not structural.
