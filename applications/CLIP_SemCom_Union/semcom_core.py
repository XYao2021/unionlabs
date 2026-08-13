#!/usr/bin/env python3
"""
semcom_core.py — CLIP-based semantic communication, core (channel-free) logic.

Ports arXiv:2507.08873 "Contrastive Language-Image Pre-Training Model based
Semantic Communication Performance Optimization" (Yang, Wei, Yu, Yang, Liu, Chen)
onto this repo's SDR PHY.

What crosses the air is the CLIP **image embedding** f_h (a float32 vector): the
base station runs a *pretrained* CLIP image encoder (no training) and sends the
embedding downlink; each user runs a follow-up task on the received (noisy)
embedding WITHOUT talking back — zero-shot classification by cosine similarity to
CLIP text embeddings (Eq. 1: y_hat = argmax_n cos(f_x, f_n)), or image
regeneration (stable diffusion, guided by f_h).

This module is pure numpy (+ optional torch/open_clip). It has NO channel and NO
radio — that lives in phy_port.py. It provides:
  * load_clip()          — real CLIP if open_clip/transformers/clip is installed,
                           else a deterministic MockClip so the pipeline runs.
  * pack_embedding / unpack_embedding  — the float32 wire codec (the "Codec").
  * classify / accuracy  — zero-shot cosine classifier.
  * synth_dataset        — a self-contained labelled image set for mock/offline.
  * MODEL_TABLE + link_metrics — the paper's delay/energy model for the optimiser.
"""
import struct, hashlib
import numpy as np

# ── the three selectable CLIP encoders (paper Table I / Sec. II-A) ────────────
#   dim   = embedding length transmitted over the air (float32[dim])
#   open_clip name + pretrained tag for the real backend
#   d_M    = relative model size  (drives the extraction-delay / BS-energy model)
#   macs_G = ~image-encoder GMACs (relative compute; nominal)
MODEL_TABLE = {
    "vit-b32": dict(id=0, dim=512, clip="ViT-B-32", pretrained="openai", d_M=1.0, macs_G=4.4),
    "vit-b16": dict(id=1, dim=512, clip="ViT-B-16", pretrained="openai", d_M=1.3, macs_G=17.6),
    "vit-l14": dict(id=2, dim=768, clip="ViT-L-14", pretrained="openai", d_M=3.9, macs_G=80.7),
}
ID2KEY = {v["id"]: k for k, v in MODEL_TABLE.items()}

# default toy label set (used by the synthetic dataset + mock text encoder)
DEFAULT_LABELS = ["airplane", "automobile", "bird", "cat", "deer",
                  "dog", "frog", "horse", "ship", "truck"]


# ══════════════════════════════════════════════════════════════════════════════
#  CLIP backends
# ══════════════════════════════════════════════════════════════════════════════
def _seed_of(s):
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16)


class MockClip:
    """Deterministic stand-in for CLIP so the whole pipeline (encode -> PHY ->
    classify) runs with no torch weights, no downloads, no radio. Both encoders
    live in ONE projected space: a fixed random projection of a downsampled image.
    The 'text' encoder embeds each class's *canonical* synthetic exemplar, so
    zero-shot cosine classification is meaningful and channel noise on the
    embedding degrades accuracy — reproducing the paper's noise-vs-accuracy trend.
    Clearly a mock; install open_clip for the real thing (see load_clip)."""

    def __init__(self, dim, name="mock"):
        self.dim = int(dim)
        self.name = name + f"(mock,d={dim})"
        rng = np.random.RandomState(1234)
        self._proj = rng.randn(8 * 8 * 3, self.dim).astype(np.float32) / np.sqrt(8 * 8 * 3)

    def _feat(self, img):
        import cv2
        small = cv2.resize(img.astype(np.float32) / 255.0, (8, 8), interpolation=cv2.INTER_AREA)
        v = small.reshape(-1) @ self._proj
        n = np.linalg.norm(v) + 1e-9
        return (v / n).astype(np.float32)

    def encode_image(self, img):
        return self._feat(img)

    def encode_texts(self, labels):
        # canonical (noise-free) exemplar of each class = the mock 'text prototype'
        return np.stack([self._feat(synth_image(lab, texture=0.0)) for lab in labels])


class RealClip:
    """Real pretrained CLIP via open_clip (preferred) — no training, CPU is fine."""

    def __init__(self, model_key, device="cpu"):
        import torch, open_clip
        spec = MODEL_TABLE[model_key]
        self.torch = torch
        self.device = device
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(
            spec["clip"], pretrained=spec["pretrained"])
        self.model.eval().to(device)
        self.tokenizer = open_clip.get_tokenizer(spec["clip"])
        self.dim = spec["dim"]
        self.name = spec["clip"]

    def encode_image(self, img):
        from PIL import Image
        x = self.preprocess(Image.fromarray(img.astype(np.uint8))).unsqueeze(0).to(self.device)
        with self.torch.no_grad():
            f = self.model.encode_image(x)
            f = f / f.norm(dim=-1, keepdim=True)
        return f.cpu().numpy().astype(np.float32)[0]

    def encode_texts(self, labels):
        toks = self.tokenizer([f"a photo of a {l}" for l in labels]).to(self.device)
        with self.torch.no_grad():
            f = self.model.encode_text(toks)
            f = f / f.norm(dim=-1, keepdim=True)
        return f.cpu().numpy().astype(np.float32)


def load_clip(model_key, device="cpu", force_mock=False):
    """Return a CLIP backend for `model_key` (vit-b32|vit-b16|vit-l14). Uses real
    open_clip weights when available, else a deterministic MockClip."""
    if model_key not in MODEL_TABLE:
        raise ValueError(f"unknown CLIP model {model_key!r}; pick from {list(MODEL_TABLE)}")
    if not force_mock:
        try:
            return RealClip(model_key, device)
        except Exception as e:                       # open_clip/torch missing or offline
            print(f"[semcom] real CLIP unavailable ({type(e).__name__}: {e}); using MockClip")
    return MockClip(MODEL_TABLE[model_key]["dim"], MODEL_TABLE[model_key]["clip"])


# ══════════════════════════════════════════════════════════════════════════════
#  Synthetic labelled images (self-contained; used by mock / offline demos)
# ══════════════════════════════════════════════════════════════════════════════
def synth_image(label, texture=0.15, size=64, seed_extra=0):
    """A deterministic 64x64x3 image for `label`: a class-specific base colour
    (depends on the label ONLY, so all samples of a class share it) plus per-sample
    seeded gaussian texture. Noise-free (texture=0) gives the class 'canonical'."""
    base = np.random.RandomState(_seed_of(label) & 0x7FFFFFFF).randint(30, 226, size=3)
    img = np.tile(base, (size, size, 1)).astype(np.float32)
    if texture > 0:
        tex = np.random.RandomState((_seed_of(label) ^ (0x9E3779B1 * (seed_extra + 1))) & 0x7FFFFFFF)
        img += tex.randn(size, size, 3) * (texture * 255.0)
    return np.clip(img, 0, 255).astype(np.uint8)


def synth_dataset(labels=DEFAULT_LABELS, per_class=8, texture=0.15, seed=0):
    """-> (images[N,H,W,3] uint8, y[N] int, labels[list]). Reproducible."""
    imgs, ys = [], []
    for ci, lab in enumerate(labels):
        for j in range(per_class):
            imgs.append(synth_image(lab, texture=texture, seed_extra=seed * 1000 + j + 1))
            ys.append(ci)
    return np.stack(imgs), np.array(ys, np.int64), list(labels)


# ══════════════════════════════════════════════════════════════════════════════
#  The Codec — float32 embedding <-> bytes (self-describing; the wire format)
# ══════════════════════════════════════════════════════════════════════════════
MAGIC = b"SEMC"
QUANT = {"f32": 0, "f16": 1, "int8": 2}
IQUANT = {v: k for k, v in QUANT.items()}


def pack_embedding(vec, model_id=0, sample_id=0, quant="f32"):
    """[SEMC | ver | model_id | quant | dim(u16) | sample(u32)] + body.
    quant f32 (lossless) | f16 (half) | int8 (+f32 scale) — the paper notes
    embeddings can be quantized to cut transmission latency/energy."""
    vec = np.asarray(vec, np.float32).ravel()
    hdr = MAGIC + struct.pack("<BBBHI", 1, model_id & 0xFF, QUANT[quant], vec.size, sample_id)
    if quant == "f32":
        body = vec.tobytes()
    elif quant == "f16":
        body = vec.astype(np.float16).tobytes()
    elif quant == "int8":
        scale = float(np.max(np.abs(vec))) or 1.0
        q = np.clip(np.round(vec / scale * 127.0), -127, 127).astype(np.int8)
        body = struct.pack("<f", scale) + q.tobytes()
    else:
        raise ValueError(quant)
    return hdr + body


def unpack_embedding(buf):
    """bytes -> (vec float32[dim], model_id, sample_id). Trailing ARQ padding ok."""
    if buf[:4] != MAGIC:
        raise ValueError("bad SEMC frame magic")
    ver, model_id, q, dim, sample_id = struct.unpack("<BBBHI", buf[4:13])
    off = 13
    if q == QUANT["f32"]:
        vec = np.frombuffer(buf, np.float32, count=dim, offset=off).copy()
    elif q == QUANT["f16"]:
        vec = np.frombuffer(buf, np.float16, count=dim, offset=off).astype(np.float32)
    elif q == QUANT["int8"]:
        scale = struct.unpack_from("<f", buf, off)[0]
        vec = np.frombuffer(buf, np.int8, count=dim, offset=off + 4).astype(np.float32) * (scale / 127.0)
    else:
        raise ValueError(f"unknown quant code {q}")
    return vec, model_id, sample_id


def embedding_nbytes(dim, quant="f32"):
    return 13 + {"f32": 4 * dim, "f16": 2 * dim, "int8": 4 + dim}[quant]


# ══════════════════════════════════════════════════════════════════════════════
#  Zero-shot classifier (paper Eq. 1) + the image-to-image similarity metric
# ══════════════════════════════════════════════════════════════════════════════
def _unit(x):
    x = np.asarray(x, np.float64)                  # float64 so a garbage (~1e38) vector can't overflow
    return (x / (np.linalg.norm(x, axis=-1, keepdims=True) + 1e-9)).astype(np.float32)


def cosine(a, b):
    return float(np.dot(_unit(a), _unit(b)))


def classify(f_img, f_texts):
    """y_hat = argmax_n cos(f_img, f_n)  (Eq. 1). f_texts: [N, dim]."""
    sims = _unit(np.atleast_2d(f_texts)) @ _unit(f_img)
    return int(np.argmax(sims)), sims


def accuracy(f_imgs, y_true, f_texts):
    preds = np.array([classify(f, f_texts)[0] for f in f_imgs])
    return float(np.mean(preds == np.asarray(y_true))), preds


# ══════════════════════════════════════════════════════════════════════════════
#  Transmission / delay / energy model (paper Eqs. 2-9) — for the PPO optimiser
# ══════════════════════════════════════════════════════════════════════════════
def rate_bps(n_rb=1, W=20e6, P=0.2, phi=1.0, I=0.0, N0=4e-15):
    """Shannon rate over n_rb resource blocks (Eq. 2), Hz-bandwidth W each."""
    return n_rb * W * np.log2(1 + P * phi / (I + W * N0))


def link_metrics(model_key, n_rb=1, quant="f32", **rb):
    """Delay & energy for sending one image's semantics with `model_key`
    (Eqs. 3-9, nominal constants). Returns dict(bits, rate, t_tx, t_ext, energy)."""
    spec = MODEL_TABLE[model_key]
    bits = 8 * embedding_nbytes(spec["dim"], quant)
    r = rate_bps(n_rb=n_rb, **rb)
    t_tx = bits / r
    t_ext = spec["macs_G"] * 1e9 * 1e-9            # nominal extraction time ~ GMACs (relative)
    energy = spec["d_M"] * 1e-3 + P_TX * t_tx      # BS extract + transmit energy (relative)
    return dict(bits=bits, rate=r, t_tx=t_tx, t_ext=t_ext, energy=energy, dim=spec["dim"])


P_TX = 0.2  # W, nominal transmit power for the energy term


if __name__ == "__main__":
    # tiny self-test: encode -> pack -> unpack -> classify (no channel, no radio)
    imgs, y, labels = synth_dataset(per_class=6, texture=0.12)
    clip = load_clip("vit-b32", force_mock=True)
    f_txt = clip.encode_texts(labels)
    f_img = np.stack([clip.encode_image(im) for im in imgs])
    acc, _ = accuracy(f_img, y, f_txt)
    buf = pack_embedding(f_img[0], model_id=MODEL_TABLE["vit-b32"]["id"], sample_id=0)
    vec, mid, sid = unpack_embedding(buf)
    ok = np.allclose(vec, f_img[0])
    print(f"[selftest] mock clean accuracy={acc:.3f}  codec_roundtrip={'OK' if ok else 'FAIL'} "
          f"bytes={len(buf)} dim={vec.size}")
