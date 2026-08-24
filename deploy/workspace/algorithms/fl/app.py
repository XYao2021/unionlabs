#!/usr/bin/env python3
"""
fl/app.py — Federated Learning on MNIST as a plain uploaded algorithm.

NO import from the PHY framework, no radio code. The algorithm only says WHAT TO
TRANSMIT and WHAT TO RECEIVE; the one-line make(role) binding lets the uniform API
read it, and the middleware carries the payload over whichever PHY is selected.

    client:  transmit() = its locally-trained model vector (float32)
             receive(aggregate) = adopt the server's new global model
    server:  receive(client_model) = collect into the round buffer
             transmit() = FedAvg over the buffer -> the new global model
    relay:   BOTH — an intermediate node that receives a model and re-transmits it,
             on the way up AND on the way back (store-and-forward; the client is out
             of the server's radio range).

One round-trip == one FL round: local SGD -> uplink -> FedAvg -> downlink -> adopt.

ROLE NAMES
    The experimenter types the algorithm's own role names, not the PHY's tx/rx:
    ROLES below maps each one onto the end of the link it drives.

RUN
    # radio-free, both ends in one process (lossless channel):
    ./run.sh --algo fl --role loopback --steps 20

    # THE WHOLE STAR FROM ONE FILE — 1 server + 2 clients, every link over TCP/IP
    # (no radio needed). Each node reads the same file and says only which node it is:
    ./run.sh --algo fl --topology fl-star-tcp --node srv      # start the server first
    ./run.sh --algo fl --topology fl-star-tcp --node c0
    ./run.sh --algo fl --topology fl-star-tcp --node c1
    ./run.sh topology fl-star-tcp             # ...or every node that lives on this box
    # the same star on the radios (B210 clients -> RX-only N210, reply over TCP):
    ./run.sh --algo fl --topology fl-star-radio --node srv
    # a chain whose hops use DIFFERENT media — n1 --air--> n2 --TCP--> n3, which is what
    # an RX-only N210 in the middle can do (start downstream first):
    ./run.sh --algo fl --topology fl-chain-mixed --node n3
    # which client am I, and how many are there, then come from the file — no
    # FL_CLIENT_ID / FL_CLIENTS to keep in step by hand.

    # radio-free, but through the REAL modem + AWGN:
    ./run.sh --algo fl --role loopback --steps 20 --channel pyphy --snr-db 8

    # radio-free 3-node chain: client -> relay -> server (every hop over the PHY):
    ./run.sh --algo fl --role chain --relays 1 --steps 20

    # over the radio, two hosts, run SEPARATELY — start the server FIRST:
    ./run.sh --algo fl --role server --rx-args addr=192.168.20.2
    ./run.sh --algo fl --role client --tx-args serial=30CD424 \
             --ack-host <SERVER_IP> --net-host <SERVER_IP> --steps 20

    # over the radio with a relay in the middle — start server, then relay, then client:
    ./run.sh --algo fl --role server --rx-args addr=192.168.20.2 --net-port 5701
    ./run.sh --algo fl --role relay  --rx-args serial=<A> --tx-args serial=<B> \
             --net-port 5700 --down-host <SERVER_IP> --down-port 5701
    ./run.sh --algo fl --role client --tx-args serial=30CD424 \
             --ack-host <RELAY_IP> --net-host <RELAY_IP> --net-port 5700 --steps 20

DATA
    Real MNIST, downloaded once to ~/.cache/mnist_over_sdr and reused. If it cannot be
    fetched (offline host), fl_core falls back to a learnable synthetic set and says so,
    so the run still completes.

KNOBS (environment variables; defaults mirror algorithms/_shared/fl.py)
    FL_HIDDEN=64  FL_ROUNDS=20  FL_LOCAL_STEPS=30  FL_LR=0.1  FL_BATCH=64
    FL_CLIENTS=1  FL_CLIENT_ID=0     one shard per client process; give each its own id
    FL_SYNTHETIC=1                    skip MNIST, use the synthetic set
    Over the radio the payload is the whole model (hidden=64 -> ~200 kB/round), so for a
    quick on-air demo shrink it:  FL_HIDDEN=16 ./run.sh --algo fl --role client ...
"""
import os, sys
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "_shared"))
from fl_core import get_dataset, iid_shards, local_train, fedavg   # noqa: E402
from mnist_sgd_over_sdr import TinyMLP                              # noqa: E402


# ── the algorithm's own role vocabulary -> the node type of the PHY each one is ──
ROLES = {"client": "tx", "server": "rx", "relay": "relay"}


def _env(name, cast, default):
    v = os.environ.get(name)
    return default if v is None or v == "" else cast(v)


# Which client am I, and how many are there? Typed as FL_CLIENT_ID / FL_CLIENTS, or —
# when the run is wired by a topology file — published by the middleware as this node's
# place among the nodes that share its role. The algorithm's own variables still win, so
# nothing that worked before changes.
FL_ROLE_INDEX = _env("UNION_ROLE_INDEX", int, 0)
FL_ROLE_COUNT = _env("UNION_ROLE_COUNT", int, 1)

HIDDEN      = _env("FL_HIDDEN", int, 64)
ROUNDS      = _env("FL_ROUNDS", int, 20)
LOCAL_STEPS = _env("FL_LOCAL_STEPS", int, 30)
LR          = _env("FL_LR", float, 0.1)
BATCH       = _env("FL_BATCH", int, 64)
CLIENTS     = _env("FL_CLIENTS", int, _env("UNION_CLIENTS", int, FL_ROLE_COUNT))
CLIENT_ID   = _env("FL_CLIENT_ID", int, FL_ROLE_INDEX)
SYNTHETIC   = _env("FL_SYNTHETIC", int, 0)


class FL:
    def __init__(self, role, rounds=ROUNDS):
        self.role = role
        self.is_server = role in ("server", "rx")
        self.is_relay = role == "relay"
        self.rounds, self.r = rounds, 0
        self.model = TinyMLP(hidden=HIDDEN, seed=0)          # same init on every node
        self.spec = ("float32", (self.model.n_params,))
        if self.is_relay:
            # a relay carries models, it does not train: no data, no model state of its own
            self.fwd = None
            print(f"[fl] relay: store-and-forward, {self.model.n_params} params/hop")
            return
        Xtr, ytr, self.Xte, self.yte = get_dataset(synthetic=bool(SYNTHETIC), seed=0)
        if self.is_server:
            self.buf = []
            print(f"[fl] server: FedAvg over {CLIENTS} client(s), hidden={HIDDEN}, "
                  f"{self.model.n_params} params/round")
        else:
            self.X, self.y = iid_shards(Xtr, ytr, CLIENTS, seed=1)[CLIENT_ID]
            self.rng = np.random.RandomState(100 + CLIENT_ID)
            print(f"[fl] client {CLIENT_ID}: {len(self.X)} samples, hidden={HIDDEN}, "
                  f"{LOCAL_STEPS} local SGD steps/round, {self.model.n_params} params/round")

    # ── WHAT TO TRANSMIT ──
    def transmit(self):
        if self.is_relay:                                    # forward whatever came in
            return self.fwd
        if self.is_server:                                   # FedAvg -> the new global
            if not self.buf:
                return None
            self.model._set_flat(fedavg(self.buf))
            self.buf = []
            self.r += 1
            print(f"    [fl] round {self.r}: global test-acc="
                  f"{self.model.accuracy(self.Xte, self.yte):.4f}")
            return self.model.get_flat().astype(np.float32)
        if self.r >= self.rounds:                            # client: training finished
            return None
        self.r += 1
        local_train(self.model, self.X, self.y, LOCAL_STEPS, LR, BATCH, self.rng)
        return self.model.get_flat().astype(np.float32)

    # ── WHAT TO RECEIVE ──
    def receive(self, msg):
        m = np.asarray(msg, np.float32)
        if self.is_relay:
            self.fwd = m                                     # hold it for the next hop
        elif self.is_server:
            self.buf.append(m)                               # collect this round's client
        else:
            self.model._set_flat(m)                          # adopt the global model
            print(f"    [fl] client {CLIENT_ID} round {self.r}: test-acc="
                  f"{self.model.accuracy(self.Xte, self.yte):.4f}")


def make(role):
    return FL(role)
