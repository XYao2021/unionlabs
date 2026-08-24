# CLIP Semantic Communication over the SDR PHY

The **third application** on the shared SDR PHY. It ports

> S. Yang, D. Wei, H. Yu, Z. Yang, Y. Liu, M. Chen, *"Contrastive Language–Image
> Pre-Training Model based Semantic Communication Performance Optimization,"*
> arXiv:2507.08873, 2025.

onto this repo's radio: instead of sending an image's pixels, the base station runs a
**pretrained CLIP image encoder** and transmits only the compact **float32 embedding**
`f_h`; each user runs its follow-up task on the received (noisy) embedding **without a
return link and without joint training** — zero-shot **classification** by cosine
similarity to CLIP *text* embeddings (`ŷ = argmax_n cos(f_x, f_n)`), or image
**regeneration** (stable diffusion, guided by `f_h`).

Because the thing crossing the air is a float32 vector, this is the **data-transfer
archetype** — architecturally FL's sibling — and it drops straight onto our PHY.

**Status: built and validated radio-free.** Mock CLIP + real modem (pyphy) reproduce the
paper's accuracy-vs-noise curve (Fig. 3). Real CLIP weights and the PPO optimiser are
opt-in / phase-2 (see `INTEGRATION.md`).

---

## Files

| File | What |
|---|---|
| `semcom_core.py` | CLIP backend (real `open_clip` **or** deterministic mock), the **float32 embedding codec**, the zero-shot classifier (Eq. 1), a self-contained synthetic dataset, and the paper's delay/energy model (Eqs. 2–9). |
| `phy_port.py` | **The API port.** `PayloadSpec` + `SemComCodec` + `PhyLink` with three backends (`ideal`, `pyphy`, `radio`) + the `SdrApp` pair (`SemComTxApp.next_payload` / `SemComRxApp.on_payload`). |
| `semcom.py` | CLI: `demo` (radio-free), `tx` / `rx` (real radio), the SNR sweep, the 3-model tradeoff. |
| `requirements.txt` | Optional deps for **real** CLIP; the mock path needs only numpy + opencv. |
| `INTEGRATION.md` | How it connects to the PHY, the reliable-vs-noisy regimes, and the PPO phase-2 plan. |

## Quickstart (no radio, no torch weights)

```bash
cd algorithms/clip_semcom

# 1) end-to-end sanity: encode -> lossless channel -> classify
python3 semcom.py demo --mock

# 2) reproduce the paper's accuracy-vs-noise (Fig. 3) through OUR real modem + FEC:
PYTHONPATH=../../../../drivers/usrp/bindings arch -x86_64 python3 semcom.py demo --mock \
    --channel pyphy --scheme QPSK --fec turbo --snr-sweep 0,2,4,6,10

# 3) the 3-CLIP-model accuracy / payload / delay / energy tradeoff (Table I):
python3 semcom.py demo --mock --model-sweep
```

Representative output of (2) — accuracy climbs with SNR, tracking BER, exactly the
paper's Fig. 3, but measured through our turbo-coded QPSK modem:

```
 SNR dB |      BER | accuracy | channel
    0.0 |   0.2077 |    0.000 | pyphy/QPSK+turbo
    2.0 |   0.0726 |    0.000 | pyphy/QPSK+turbo
    4.0 |   0.0006 |    0.700 | pyphy/QPSK+turbo
    6.0 |   0.0000 |    0.988 | pyphy/QPSK+turbo
```

## Over the radio (two hosts)

Same B210→N210 link and byte-pipe `fl.py` uses. Start the receiver first:

```bash
# RX host (N210): receive embeddings and classify
python3 semcom.py rx --rx-args addr=192.168.20.2 --num 8

# TX host (B210): encode images, send each embedding
python3 semcom.py tx --tx-args serial=30CD424 --ack-host <RX_HOST_IP>
```

The radio backend uses reliable ARQ, so the embedding is delivered losslessly (the cost of
noise shows up as retransmissions/delay). For the *semantic* regime where noise corrupts
the embedding (the paper's setting), use the radio-free `--channel pyphy` sweep above, or
the single-shot mode discussed in `INTEGRATION.md`.

## Using real CLIP

```bash
pip install -r requirements.txt        # torch is already present in the lab env
python3 semcom.py demo --model vit-l14 # drops the --mock flag => real ViT-L/14 weights
```

`load_clip()` auto-detects `open_clip`; if it's missing or offline it prints a notice and
falls back to the mock, so nothing ever hard-fails. Models: `vit-b32`, `vit-b16`, `vit-l14`
(the paper's three).

## See also

- `../EXPERIMENT_GUIDE.pdf` (or `.md`) — step-by-step commands to run every application (radio-free + hardware); this app is §3.
- `../APPLICATIONS_INTRO.pdf` — all applications introduced together.
- `../fl/fl.py` — the FL data-transfer app this mirrors (same PHY byte-pipe).
- `../../../../drivers/usrp/bindings/pyphy.cpp` — the block API the `pyphy` channel backend uses.
