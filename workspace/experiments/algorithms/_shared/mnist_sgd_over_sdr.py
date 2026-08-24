#!/usr/bin/env python3
"""
mnist_sgd_over_sdr.py — train a tiny MLP on MNIST where, every iteration, the
**top-k compressed gradient** is transmitted over the USRP B210 SDR link.

It is federated/distributed SGD with your radio as the network:

    worker (TX/source_arq)                     server (RX/sink_arq)
    ─────────────────────                      ────────────────────
    compute gradient on a batch                receive top-k bytes
    top-k (k=5%) + error feedback   ── RF ──►  apply the SAME sparse update
    apply the sparse update locally            (mirror model stays in sync)
    serialize -> send via sdr.source_arq       deserialize <- sdr.sink_arq

Both ends start from the same seed and apply the identical sparse update, so
their models track each other exactly — matching accuracy proves the gradient
crossed the 915 MHz link error-free (CRC+FEC+ARQ guarantee it, or it retransmits).

The SDR is driven exactly like run.py: this script imports `sdr` and calls
`sdr.source_arq(...)` / `sdr.sink_arq(...)` **inside the training loop**. The
compressed gradient is handed to the PHY through ONE reused scratch file per
side (overwritten each round, deleted on exit — no per-iteration file clutter).

Run in two terminals (start the SERVER first):

    python3 mnist_sgd_over_sdr.py server --rounds 30 --rx-args serial=30CD3F7
    python3 mnist_sgd_over_sdr.py worker --rounds 30 --tx-args serial=30CD424

Measure the real link throughput first (no training, one N-byte blob, timed):

    python3 mnist_sgd_over_sdr.py server --probe 32768 --rx-args serial=30CD3F7
    python3 mnist_sgd_over_sdr.py worker --probe 32768 --tx-args serial=30CD424
"""
import argparse
import atexit
import gzip
import os
import struct
import sys
import tempfile
import time
import urllib.request

import numpy as np

# Import the auto-generated SDR wrapper (same module run.py uses).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "drivers", "usrp", "python"))
import sdr  # noqa: E402

MAGIC = b"GRAD"
MNIST_MIRROR = "https://storage.googleapis.com/cvdf-datasets/mnist/"
CACHE = os.path.join(os.path.expanduser("~"), ".cache", "mnist_over_sdr")


# ─────────────────────────────────────────────────────────────────────────────
#  MNIST (download idx.gz from the CVDF mirror, parse with numpy — no extra deps)
# ─────────────────────────────────────────────────────────────────────────────
def _fetch(name):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    if not os.path.exists(path):
        print(f"[data] downloading {name} ...")
        urllib.request.urlretrieve(MNIST_MIRROR + name, path)
    with gzip.open(path, "rb") as f:
        return f.read()


def load_mnist():
    def images(buf):
        n = struct.unpack(">I", buf[4:8])[0]
        a = np.frombuffer(buf[16:], np.uint8).reshape(n, 28 * 28)
        return (a.astype(np.float32) / 255.0)

    def labels(buf):
        return np.frombuffer(buf[8:], np.uint8)

    Xtr = images(_fetch("train-images-idx3-ubyte.gz"))
    ytr = labels(_fetch("train-labels-idx1-ubyte.gz"))
    Xte = images(_fetch("t10k-images-idx3-ubyte.gz"))
    yte = labels(_fetch("t10k-labels-idx1-ubyte.gz"))
    return Xtr, ytr, Xte, yte


# ─────────────────────────────────────────────────────────────────────────────
#  Tiny MLP  (784 -> H -> 10), numpy forward/backward, params as one flat vector
# ─────────────────────────────────────────────────────────────────────────────
class TinyMLP:
    def __init__(self, hidden=100, seed=0):
        rng = np.random.RandomState(seed)          # same seed on both ends
        self.W1 = (rng.randn(784, hidden) * np.sqrt(2.0 / 784)).astype(np.float32)
        self.b1 = np.zeros(hidden, np.float32)
        self.W2 = (rng.randn(hidden, 10) * np.sqrt(2.0 / hidden)).astype(np.float32)
        self.b2 = np.zeros(10, np.float32)

    # flat-vector view of all parameters (order fixed: W1, b1, W2, b2)
    def _parts(self):
        return (self.W1, self.b1, self.W2, self.b2)

    @property
    def n_params(self):
        return sum(p.size for p in self._parts())

    def get_flat(self):
        return np.concatenate([p.ravel() for p in self._parts()])

    def add_flat(self, delta):                     # params += delta  (in place)
        i = 0
        for p in self._parts():
            p.ravel()[:] += delta[i:i + p.size]
            i += p.size

    def apply_sparse(self, idx, vals):             # params[idx] += vals
        flat = self.get_flat()
        flat[idx] += vals
        self._set_flat(flat)

    def _set_flat(self, flat):
        i = 0
        for p in self._parts():
            p.ravel()[:] = flat[i:i + p.size]
            i += p.size

    def forward(self, X):
        z1 = X @ self.W1 + self.b1
        a1 = np.maximum(z1, 0.0)
        z2 = a1 @ self.W2 + self.b2
        z2 -= z2.max(1, keepdims=True)
        e = np.exp(z2)
        p = e / e.sum(1, keepdims=True)
        return z1, a1, p

    def grad(self, X, y):
        """Flat gradient of cross-entropy on batch (X, y)."""
        n = X.shape[0]
        z1, a1, p = self.forward(X)
        d2 = p.copy()
        d2[np.arange(n), y] -= 1.0
        d2 /= n
        gW2 = a1.T @ d2
        gb2 = d2.sum(0)
        d1 = (d2 @ self.W2.T) * (z1 > 0)
        gW1 = X.T @ d1
        gb1 = d1.sum(0)
        return np.concatenate([gW1.ravel(), gb1, gW2.ravel(), gb2]).astype(np.float32)

    def accuracy(self, X, y):
        _, _, p = self.forward(X)
        return float((p.argmax(1) == y).mean())


# ─────────────────────────────────────────────────────────────────────────────
#  Top-k gradient compression with error feedback (residual accumulation)
# ─────────────────────────────────────────────────────────────────────────────
class TopK:
    def __init__(self, n, ratio):
        self.k = max(1, int(round(n * ratio)))
        self.residual = np.zeros(n, np.float32)

    def compress(self, grad):
        g = self.residual + grad                   # add last round's leftovers
        idx = np.argpartition(np.abs(g), -self.k)[-self.k:]
        idx = np.sort(idx).astype(np.uint32)
        vals = g[idx].astype(np.float32)
        self.residual = g
        self.residual[idx] = 0.0                    # carry the untransmitted rest
        return idx, vals


def serialize(round_idx, n_params, idx, vals):
    """[MAGIC|ver|round|n|k] + uint32 idx[k] + float32 vals[k]  (self-delimiting)."""
    head = MAGIC + struct.pack("<BIII", 1, round_idx, n_params, idx.size)
    return head + idx.tobytes() + vals.tobytes()


def deserialize(buf):
    if buf[:4] != MAGIC:
        raise ValueError("bad payload magic (not a GRAD frame)")
    _, round_idx, n_params, k = struct.unpack("<BIII", buf[4:17])
    off = 17
    idx = np.frombuffer(buf, np.uint32, count=k, offset=off).copy()
    off += 4 * k
    vals = np.frombuffer(buf, np.float32, count=k, offset=off).copy()
    return round_idx, n_params, idx, vals            # trailing chunk padding ignored


# ─────────────────────────────────────────────────────────────────────────────
#  PHY invocation — same helpers run.py uses, called inside the loop
# ─────────────────────────────────────────────────────────────────────────────
def phy_common(a):
    """Shared PHY options. OFDM (--waveform ofdm) is CFO-robust — use it when the
    free-running-clock offset makes single-carrier retransmit heavily."""
    d = dict(
        scheme=a.scheme, waveform=a.waveform, fec=True,
        rx_freq=915e6, tx_freq=915e6, rx_rate=1.6e6, tx_rate=1.6e6,
        rx_subdev="A:A", tx_subdev="A:A", rx_ant="RX2", tx_ant="TX/RX",
        det_mult=3, ack_transport="tcp", ack_port=a.ack_port,
        bytes_length=a.chunk, timer_interval=a.timer_interval, viz=False,
    )
    if a.waveform == "ofdm":
        d.update(ofdm_fft=64, ofdm_cp=16)
    return d


def send_payload(a, payload, tmp_path):
    """Write bytes to the reused scratch file and transmit them via source_arq."""
    with open(tmp_path, "wb") as f:
        f.write(payload)
    extra = {"ofdm_tx_peak": 0.5} if a.waveform == "ofdm" else {}
    sdr.source_arq(
        tx_args=a.tx_args, rx_args=a.tx_args, tx_gain=a.tx_gain,
        ack_host=a.ack_host, timeout=a.timeout, max_attempts=a.max_attempts,
        payload_file=tmp_path, **extra, **phy_common(a),
    ).run()


def recv_payload(a, tmp_path):
    """Receive one payload into the reused scratch file via sink_arq; return bytes."""
    if os.path.exists(tmp_path):
        os.remove(tmp_path)                          # so we never read a stale round
    sdr.sink_arq(
        rx_args=a.rx_args, tx_args=a.rx_args, rx_gain=a.rx_gain,
        out_file=tmp_path, **phy_common(a),
    ).run()
    with open(tmp_path, "rb") as f:
        return f.read()


def scratch(role):
    """One reusable temp file per side; auto-deleted on exit."""
    path = os.path.join(tempfile.gettempdir(), f"sdr_grad_{role}.bin")
    atexit.register(lambda: os.path.exists(path) and os.remove(path))
    return path


# ─────────────────────────────────────────────────────────────────────────────
#  Throughput probe — send one known N-byte blob, time it, report KB/s
# ─────────────────────────────────────────────────────────────────────────────
def probe(a):
    tmp = scratch(a.role)
    if a.role == "worker":
        blob = (b"PROBE" + os.urandom(a.probe))[:a.probe]
        print(f"[probe] sending {a.probe} bytes (chunk={a.chunk}, {a.scheme}) ...")
        t0 = time.time()
        send_payload(a, blob, tmp)
        dt = time.time() - t0
        print(f"[probe] {a.probe} B in {dt:.2f} s  ->  "
              f"{a.probe / dt / 1024:.2f} KB/s ({a.probe * 8 / dt / 1000:.1f} kbps)")
        print(f"[probe] a 79,510-param top-k(5%) gradient (~31.8 KB) would take "
              f"~{31800 / (a.probe / dt):.1f} s/round at this rate")
    else:
        print(f"[probe] receiving {a.probe} bytes ...")
        buf = recv_payload(a, tmp)
        print(f"[probe] received {len(buf)} bytes (>= {a.probe} incl. padding)")


# ─────────────────────────────────────────────────────────────────────────────
#  Training loops
# ─────────────────────────────────────────────────────────────────────────────
def run_worker(a):
    Xtr, ytr, Xte, yte = load_mnist()
    model = TinyMLP(a.hidden, seed=0)
    comp = TopK(model.n_params, a.topk)
    tmp = scratch("worker")
    rng = np.random.RandomState(1234)
    Xte_s, yte_s = Xte[:2000], yte[:2000]
    print(f"[worker] MLP {model.n_params} params, top-k={a.topk} "
          f"-> {comp.k} coords/round (~{17 + comp.k * 8} bytes)")

    for r in range(a.rounds):
        b = rng.randint(0, Xtr.shape[0], a.batch)
        g = model.grad(Xtr[b], ytr[b])
        idx, vals = comp.compress(g)
        update = (-a.lr * vals).astype(np.float32)   # the sparse SGD step
        model.apply_sparse(idx, update)              # apply locally...
        send_payload(a, serialize(r, model.n_params, idx, update), tmp)  # ...and send
        acc = model.accuracy(Xte_s, yte_s)
        print(f"[worker] round {r + 1}/{a.rounds}  sent {comp.k} coords  "
              f"train-acc(sample)={acc:.3f}")
    print("[worker] done.")


def run_server(a):
    _, _, Xte, yte = load_mnist()
    model = TinyMLP(a.hidden, seed=0)                # SAME init as the worker
    tmp = scratch("server")
    print(f"[server] MLP {model.n_params} params — reconstructing from RF gradients")

    for r in range(a.rounds):
        buf = recv_payload(a, tmp)
        try:
            rr, n, idx, update = deserialize(buf)
        except ValueError as e:
            print(f"[server] round {r + 1}: {e} — skipping")
            continue
        if n != model.n_params:
            print(f"[server] round {r + 1}: param mismatch ({n} != {model.n_params})")
            continue
        model.apply_sparse(idx, update)              # same sparse update as worker
        acc = model.accuracy(Xte, yte)
        print(f"[server] round {r + 1}/{a.rounds}  applied {idx.size} coords "
              f"(tx round {rr})  test-acc={acc:.4f}")
    print("[server] done.")


# ─────────────────────────────────────────────────────────────────────────────
def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)   # show progress live when piped to a file
    except Exception:
        pass
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("role", choices=["worker", "server"])
    p.add_argument("--rounds", type=int, default=30)
    p.add_argument("--probe", type=int, default=0,
                   help="throughput-probe mode: send/recv this many bytes, no training")
    # model / optimisation
    p.add_argument("--hidden", type=int, default=100)
    p.add_argument("--batch", type=int, default=128)
    p.add_argument("--lr", type=float, default=0.5)
    p.add_argument("--topk", type=float, default=0.05, help="top-k fraction (default 5%%)")
    # PHY
    p.add_argument("--scheme", default="QPSK")
    p.add_argument("--waveform", default="sc", choices=["sc", "ofdm"],
                   help="sc (single-carrier) or ofdm (CFO-robust; use on a marginal link)")
    p.add_argument("--chunk", type=int, default=512, help="payload bytes per chunk "
                   "(bigger = fewer round-trips, but longer bursts fail more under CFO)")
    p.add_argument("--timeout", type=int, default=400,
                   help="source ACK timeout (ms) — must exceed the ~170ms ACK round-trip")
    p.add_argument("--timer-interval", type=int, default=20,
                   help="sink FIFO poll interval (ms) — SETS the ACK latency (was 1000)")
    p.add_argument("--max-attempts", type=int, default=0,
                   help="source give-up limit per chunk; 0 = never give up. Keep 0 so a "
                        "hard chunk can't desync the paired worker/server loop.")
    p.add_argument("--tx-args", default="serial=30CD424")
    p.add_argument("--rx-args", default="serial=30CD3F7")
    p.add_argument("--tx-gain", type=float, default=78)
    p.add_argument("--rx-gain", type=float, default=20)
    p.add_argument("--ack-host", default="127.0.0.1")
    p.add_argument("--ack-port", type=int, default=5599)
    a = p.parse_args()

    if a.probe:
        probe(a)
    elif a.role == "worker":
        run_worker(a)
    else:
        run_server(a)


if __name__ == "__main__":
    main()
