#!/usr/bin/env python3
"""
fl_core.py — shared building blocks for MNIST-over-SDR distributed learning
(federated `fl.py` and, later, decentralized `dl.py`).

Reuses the numpy model + MNIST loader from `mnist_sgd_over_sdr.py` (so everything
stays torch-free and runs in the sdr-phy container) and adds:

  * iid_shards()        — split MNIST into K disjoint shards, one per client/node
  * serialize_model()   — frame a full flat parameter vector as bytes (for the radio
    deserialize_model()   byte-pipe --payload-file / --out-file); complements the
                          sparse-gradient serialize() in mnist_sgd_over_sdr.py
  * local_train()       — E minibatch-SGD steps on a shard
  * fedavg()            — weighted mean of client models (the aggregation step)
  * average_models()    — plain mean (decentralized consensus step)

The transport is intentionally NOT here: FL/DL call an injected channel (an in-process
mock for offline validation, or the real source_arq/sink_arq byte-pipe on hardware),
so the learning logic is identical on the mock and over the USRP link.
"""
import os
import struct
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "phy", "python"))
from mnist_sgd_over_sdr import TinyMLP, load_mnist   # noqa: E402  (reuse)

MODEL_MAGIC = b"MODL"


# ─────────────────────────────────────────────────────────────────────────────
#  Data sharding
# ─────────────────────────────────────────────────────────────────────────────
def iid_shards(X, y, K, seed=0):
    """Split (X, y) into K disjoint IID shards (shuffled). Returns a list of (Xk, yk)."""
    rng = np.random.RandomState(seed)
    perm = rng.permutation(len(X))
    return [(X[p], y[p]) for p in np.array_split(perm, K)]


# ─────────────────────────────────────────────────────────────────────────────
#  Full-model (de)serialization — the wire format for a whole parameter vector
# ─────────────────────────────────────────────────────────────────────────────
def serialize_model(round_idx, flat):
    """[MODL | ver=1 | round | n] + float32[n]. Self-delimiting; trailing ARQ chunk
    padding on the receive side is ignored (we read exactly n floats)."""
    flat = np.asarray(flat, np.float32)
    return MODEL_MAGIC + struct.pack("<BII", 1, round_idx, flat.size) + flat.tobytes()


def deserialize_model(buf):
    if buf[:4] != MODEL_MAGIC:
        raise ValueError("bad model frame magic (not a MODL frame)")
    _, round_idx, n = struct.unpack("<BII", buf[4:13])
    flat = np.frombuffer(buf, np.float32, count=n, offset=13).copy()
    return round_idx, flat


# ─────────────────────────────────────────────────────────────────────────────
#  Local training + aggregation
# ─────────────────────────────────────────────────────────────────────────────
def local_train(model, X, y, steps, lr, batch, rng):
    """Run `steps` minibatch-SGD steps on (X, y); updates `model` in place."""
    n = len(X)
    bs = min(batch, n)
    for _ in range(steps):
        b = rng.randint(0, n, size=bs)
        g = model.grad(X[b], y[b])
        model.add_flat(-lr * g)
    return model


def fedavg(client_flats, weights=None):
    """FedAvg: weighted mean of client parameter vectors -> the new global model.
    Averaging client MODELS (each = global + local delta) equals global + mean(delta)."""
    if weights is None:
        weights = [1.0] * len(client_flats)
    w = np.asarray(weights, np.float64)
    w /= w.sum()
    out = np.zeros_like(client_flats[0], np.float64)
    for wi, cf in zip(w, client_flats):
        out += wi * cf
    return out.astype(np.float32)


def average_models(flats):
    """Plain mean of parameter vectors — the decentralized consensus mixing step."""
    return np.mean(np.stack([np.asarray(f, np.float32) for f in flats]), axis=0).astype(np.float32)


# ─────────────────────────────────────────────────────────────────────────────
#  Data helper (MNIST, or a torch-free synthetic fallback if it can't download)
# ─────────────────────────────────────────────────────────────────────────────
def get_dataset(synthetic=False, n_synth=6000, seed=0):
    """Return (Xtr, ytr, Xte, yte). Falls back to a learnable 784-dim / 10-class
    synthetic set if MNIST can't be fetched (offline) so the FL/DL machinery can still
    be validated end-to-end."""
    if not synthetic:
        try:
            return load_mnist()
        except Exception as e:      # noqa: BLE001
            print("[fl_core] MNIST unavailable (%s) -> synthetic dataset" % e)
    rng = np.random.RandomState(seed)
    W = rng.randn(784, 10).astype(np.float32)          # a fixed linear ground truth
    def make(n):
        X = (rng.randn(n, 784) * 0.5).astype(np.float32)
        y = (X @ W).argmax(1).astype(np.uint8)
        return X, y
    Xtr, ytr = make(n_synth)
    Xte, yte = make(2000)
    return Xtr, ytr, Xte, yte
