#!/usr/bin/env python3
"""
fl.py — Federated learning (FedAvg) of a tiny MNIST MLP over the USRP SDR link.

Lab topology:
    server = N210 (comp A, addr=192.168.20.2)  |  clients = 2x B210 (comp B)

Two transfer modes:
  * COMPRESSED up-only (default, --compress-ratio 0.05): each client sends only its
    top-k sparse DELTA up (~20 KB vs ~200 KB), with error feedback (residual) via TopK.
    The server sends the aggregate DOWN LOSSLESSLY — its nonzeros are just the union of
    the client supports (<= K*k coords), so it's sparse but loss-free, and server +
    clients track the EXACT FedAvg of the up-compressed deltas (no down-side residual).
    All nodes init from the same seed and apply the same aggregate -> stay synchronized.
    This is the practical hardware path.
  * FULL (--compress-ratio 0): send the whole model each round (simpler, ~200 KB).

torch-free (numpy) -> runs in the sdr-phy container.

VALIDATE WITH NO RADIOS FIRST:
    python3 fl.py --mock --clients 2 --rounds 20                 # compressed
    python3 fl.py --mock --clients 2 --rounds 20 --compress-ratio 0   # full model

ON THE RADIOS (start server first; one client process per B210):
    # server on the N210 host
    python3 fl.py server --clients 1 --rounds 20 \
        --rx-args addr=192.168.20.2 --tx-args addr=192.168.20.2 --rx-subdev A:0 --tx-subdev A:0
    # client on a B210 (ack-host = the server host IP)
    python3 fl.py client --client-id 0 --clients 1 --rounds 20 \
        --tx-args serial=30CD424 --rx-args serial=30CD424 --ack-host <server-ip>
"""
import argparse
import atexit
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sdr  # noqa: E402
from fl_core import (TinyMLP, deserialize_model, fedavg, get_dataset,  # noqa: E402
                     iid_shards, local_train, serialize_model)
# Reuse the sparse top-k codec + compressor already proven over the link.
from mnist_sgd_over_sdr import (TopK, deserialize as deser_sparse,  # noqa: E402
                                serialize as ser_sparse)


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _dense(n, idx, vals):
    d = np.zeros(n, np.float32)
    d[idx] = vals
    return d


def _phy(a):
    """PHY for the N210<->B210 link: DQPSK at an N210-exact 2e6 / symbol_rate 1e6."""
    return dict(
        scheme=a.scheme, waveform="sc", fec=True,
        rx_freq=915e6, tx_freq=915e6, tx_rate=2e6, rx_rate=2e6, symbol_rate=1e6,
        rx_ant="RX2", tx_ant="TX/RX", rx_subdev=a.rx_subdev, tx_subdev=a.tx_subdev,
        det_mult=3, ack_transport="tcp", ack_port=a.ack_port,
        bytes_length=a.chunk, viz=False,
    )


def _scratch(tag):
    p = os.path.join(tempfile.gettempdir(), "fl_%s.bin" % tag)
    atexit.register(lambda: os.path.exists(p) and os.remove(p))
    return p


def _send(a, payload, tag):
    path = _scratch(tag)
    with open(path, "wb") as f:
        f.write(payload)
    sdr.source_arq(tx_args=a.tx_args, rx_args=a.tx_args, tx_gain=a.tx_gain,
                   ack_host=a.ack_host, timeout=a.timeout, max_attempts=a.max_attempts,
                   payload_file=path, **_phy(a)).run()


def _recv(a, tag):
    path = _scratch(tag)
    if os.path.exists(path):
        os.remove(path)                    # never read a stale round
    sdr.sink_arq(rx_args=a.rx_args, tx_args=a.rx_args, rx_gain=a.rx_gain,
                 out_file=path, **_phy(a)).run()
    with open(path, "rb") as f:
        return f.read()


# ─────────────────────────────────────────────────────────────────────────────
#  Mock — full FedAvg round in one process, no radios (validates ML + wire format)
# ─────────────────────────────────────────────────────────────────────────────
def run_mock(a):
    Xtr, ytr, Xte, yte = get_dataset(synthetic=a.synthetic)
    shards = iid_shards(Xtr, ytr, a.clients, seed=1)
    rngs = [np.random.RandomState(100 + k) for k in range(a.clients)]
    g = TinyMLP(hidden=a.hidden, seed=0)
    n = g.n_params

    if a.compress_ratio <= 0:                                  # ── FULL model ──
        wire = len(serialize_model(0, g.get_flat()))
        print("[fl-mock] FULL model | n_params=%d | %d B/model over the wire" % (n, wire))
        for r in range(a.rounds):
            gflat = g.get_flat()
            flats, wts = [], []
            for k in range(a.clients):
                _, gk = deserialize_model(serialize_model(r, gflat))
                c = TinyMLP(hidden=a.hidden, seed=0); c._set_flat(gk.copy())
                local_train(c, shards[k][0], shards[k][1], a.local_steps, a.lr, a.batch, rngs[k])
                _, cf = deserialize_model(serialize_model(r, c.get_flat()))
                flats.append(cf); wts.append(len(shards[k][1]))
            g._set_flat(fedavg(flats, wts))
            print("[fl-mock] round %2d/%d  test-acc=%.4f" % (r + 1, a.rounds, g.accuracy(Xte, yte)))
        print("[fl-mock] done. final test-acc=%.4f" % g.accuracy(Xte, yte)); return

    # ── COMPRESSED up-only (synchronized mirrors; LOSSLESS aggregate down) ──
    #   Up:   each client top-k's its delta (error feedback). Only the up link is lossy.
    #   Down: the server sends the EXACT aggregate — its nonzeros are just the union of
    #         the client supports (<= K*k coords), so it's sparse but loss-free. Server
    #         and clients apply the same aggregate -> they track the true FedAvg of the
    #         up-compressed deltas, no down-side residual needed.
    clients = [TinyMLP(hidden=a.hidden, seed=0) for _ in range(a.clients)]
    up = [TopK(n, a.compress_ratio) for _ in range(a.clients)]
    kc = up[0].k
    print("[fl-mock] COMPRESSED up-only | n=%d | up k=%d (%.1f%%, ~%d B) | down = exact aggregate"
          % (n, kc, 100 * a.compress_ratio, 17 + kc * 8))
    for r in range(a.rounds):
        deltas = []
        for c, sh, u, rng in zip(clients, shards, up, rngs):
            base = c.get_flat().copy()
            local_train(c, sh[0], sh[1], a.local_steps, a.lr, a.batch, rng)
            delta = c.get_flat() - base
            c._set_flat(base)                                  # apply the aggregate, not own delta
            idx, vals = u.compress(delta)
            _, _, idx, vals = deser_sparse(ser_sparse(r, n, idx, vals))   # through the wire frame
            deltas.append(_dense(n, idx, vals))
        U = np.mean(deltas, axis=0).astype(np.float32)         # FedAvg of the sparse deltas
        nz = np.nonzero(U)[0].astype(np.uint32)                # LOSSLESS: all of U's support
        uv = U[nz].astype(np.float32)
        _, _, nz, uv = deser_sparse(ser_sparse(r, n, nz, uv))  # through the wire frame
        g.apply_sparse(nz, uv)
        for c in clients:
            c.apply_sparse(nz, uv)                             # mirrors track the server exactly
        print("[fl-mock] round %2d/%d  test-acc=%.4f  (down %d coords)"
              % (r + 1, a.rounds, g.accuracy(Xte, yte), nz.size))
    print("[fl-mock] done. final test-acc=%.4f" % g.accuracy(Xte, yte))


# ─────────────────────────────────────────────────────────────────────────────
#  Radio server / client (one node per process)
# ─────────────────────────────────────────────────────────────────────────────
def run_server(a):
    _, _, Xte, yte = get_dataset(synthetic=a.synthetic)
    g = TinyMLP(hidden=a.hidden, seed=0)
    n = g.n_params
    compressed = a.compress_ratio > 0
    print("[server] FedAvg | %d clients | %d rounds | %s"
          % (a.clients, a.rounds, "compressed up / lossless down" if compressed else "full model"))
    for r in range(a.rounds):
        if not compressed:                                     # FULL model
            flats = []
            for k in range(a.clients):
                _send(a, serialize_model(r, g.get_flat()), "srv_down")
                _, cf = deserialize_model(_recv(a, "srv_up"))
                flats.append(cf)
                print("[server] round %d: client %d model received" % (r + 1, k))
            g._set_flat(fedavg(flats))
        else:                                                  # COMPRESSED up / LOSSLESS down
            deltas = []
            for k in range(a.clients):                         # phase 1: collect up-deltas
                _, _, idx, vals = deser_sparse(_recv(a, "srv_up"))
                deltas.append(_dense(n, idx, vals))
                print("[server] round %d: client %d delta received" % (r + 1, k))
            U = np.mean(deltas, axis=0).astype(np.float32)
            nz = np.nonzero(U)[0].astype(np.uint32)            # exact aggregate support
            uv = U[nz].astype(np.float32)
            g.apply_sparse(nz, uv)
            for k in range(a.clients):                         # phase 2: broadcast the exact aggregate
                _send(a, ser_sparse(r, n, nz, uv), "srv_down")
        print("[server] round %d/%d  test-acc=%.4f" % (r + 1, a.rounds, g.accuracy(Xte, yte)))


def run_client(a):
    Xtr, ytr, _, _ = get_dataset(synthetic=a.synthetic)
    shard = iid_shards(Xtr, ytr, a.clients, seed=1)[a.client_id]
    rng = np.random.RandomState(100 + a.client_id)
    c = TinyMLP(hidden=a.hidden, seed=0)
    n = c.n_params
    up = TopK(n, a.compress_ratio) if a.compress_ratio > 0 else None
    print("[client %d] %d rounds | shard=%d samples | %s"
          % (a.client_id, a.rounds, len(shard[1]), "compressed" if up else "full model"))
    for r in range(a.rounds):
        if up is None:                                         # FULL model
            _, gf = deserialize_model(_recv(a, "cli_down"))
            c._set_flat(gf.copy())
            local_train(c, shard[0], shard[1], a.local_steps, a.lr, a.batch, rng)
            _send(a, serialize_model(r, c.get_flat()), "cli_up")
        else:                                                  # COMPRESSED deltas
            base = c.get_flat().copy()
            local_train(c, shard[0], shard[1], a.local_steps, a.lr, a.batch, rng)
            delta = c.get_flat() - base
            c._set_flat(base)
            idx, vals = up.compress(delta)
            _send(a, ser_sparse(r, n, idx, vals), "cli_up")    # phase 1: send my delta up
            _, _, ui, uv = deser_sparse(_recv(a, "cli_down"))  # phase 2: get the aggregate
            c.apply_sparse(ui, uv)
        print("[client %d] round %d/%d  done" % (a.client_id, r + 1, a.rounds))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("role", nargs="?", choices=["server", "client"])
    p.add_argument("--mock", action="store_true", help="run FedAvg in one process, no radios")
    p.add_argument("--synthetic", action="store_true", help="use synthetic data (offline)")
    p.add_argument("--clients", type=int, default=2)
    p.add_argument("--client-id", type=int, default=0)
    p.add_argument("--rounds", type=int, default=20)
    p.add_argument("--local-steps", type=int, default=30)
    p.add_argument("--hidden", type=int, default=64)
    p.add_argument("--lr", type=float, default=0.1)
    p.add_argument("--batch", type=int, default=64)
    p.add_argument("--compress-ratio", type=float, default=0.05,
                   help="top-k fraction of params sent per delta (0 = send the full model)")
    # radio / PHY
    p.add_argument("--tx-args", default="serial=30CD424")
    p.add_argument("--rx-args", default="serial=30CD424")
    p.add_argument("--rx-subdev", default="A:A")
    p.add_argument("--tx-subdev", default="A:A")
    p.add_argument("--tx-gain", type=float, default=85)
    p.add_argument("--rx-gain", type=float, default=20)
    p.add_argument("--scheme", default="DQPSK")
    p.add_argument("--chunk", type=int, default=125)
    p.add_argument("--ack-host", default="127.0.0.1")
    p.add_argument("--ack-port", type=int, default=5599)
    p.add_argument("--timeout", type=int, default=3000)
    p.add_argument("--max-attempts", type=int, default=0)
    a = p.parse_args()

    if a.mock:
        run_mock(a)
    elif a.role == "server":
        run_server(a)
    elif a.role == "client":
        run_client(a)
    else:
        p.error("give a role (server|client) or --mock")


if __name__ == "__main__":
    main()
