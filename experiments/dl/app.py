#!/usr/bin/env python3
"""
dl/app.py — DECENTRALIZED learning on MNIST as a plain uploaded algorithm.

The counterpart of experiments/fl (federated): there is NO server and NO access point.
Every node is the same program — it holds its own data shard, trains locally, exchanges
models with its GRAPH NEIGHBOURS ONLY, and mixes what it receives by plain averaging
(consensus). Agreement across the network has to emerge from those local exchanges.

    every node:  transmit() = mix in whatever neighbours sent, train locally, send the model
                 receive(m) = a neighbour's model -> into the mixing buffer

One round = mix -> local SGD -> one exchange with each neighbour. Federated learning
averages at one server; decentralized learning averages at every node, over the topology
the experimenter chooses.

NO import from the PHY framework and no radio code: the middleware carries each edge of
the graph over the PHY (union/phy_link.run_gossip), so the same file runs radio-free or
over USRPs.

RUN  (topology is the experimenter's choice: ring by default, fully connected, or custom)
    ./run.sh --algo dl --role gossip --agents 6 --steps 20                  # ring
    ./run.sh --algo dl --role gossip --agents 6 --steps 20 --topology full
    ./run.sh --algo dl --role gossip --agents 6 --steps 20 --topology 0-1,1-2,2-3,3-4,4-5
    ./run.sh --algo dl --role gossip --agents 6 --steps 20 --channel pyphy --snr-db 8

    # EACH NODE AS ITS OWN PROCESS — one terminal (or one computer) per node.
    # Every node is told which node it is, how many there are, and the graph; from that
    # they all derive the same exchange schedule, so no coordinator is needed. Over TCP,
    # three terminals on this machine:
    ./run.sh --algo dl --node 0 --agents 3 --steps 20
    ./run.sh --algo dl --node 1 --agents 3 --steps 20
    ./run.sh --algo dl --node 2 --agents 3 --steps 20

    # on three different computers (host per node, indexed by node id):
    ./run.sh --algo dl --node 0 --agents 3 --peers 10.0.0.1,10.0.0.2,10.0.0.3 --steps 20

    # over the radio, each node naming the USRP that this process owns:
    ./run.sh --algo dl --node 0 --agents 3 --peer-link wireless --radio serial=30CD424
    ./run.sh --algo dl --node 1 --agents 3 --peer-link wireless --radio addr=192.168.40.2
    ./run.sh --algo dl --node 2 --agents 3 --peer-link wireless --radio serial=30CD3F7

    # two peers exchanging directly (the smallest decentralized network):
    ./run.sh --algo dl --role loopback --steps 20

    # over the radio, two hosts, run SEPARATELY — start the responder FIRST:
    ./run.sh --algo dl --role responder --radio addr=192.168.20.2
    ./run.sh --algo dl --role initiator --radio serial=30CD424 \
             --ack-host <PEER_IP> --net-host <PEER_IP> --steps 20

DATA
    Real MNIST by default (cached in ~/.cache/mnist_over_sdr), split into one IID shard
    per node. Swap the dataset by changing the get_dataset() call below — anything that
    returns (Xtr, ytr, Xte, yte) as flat float32 arrays works unchanged.

KNOBS (environment variables)
    DL_HIDDEN=64  DL_ROUNDS=20  DL_LOCAL_STEPS=30  DL_LR=0.1  DL_BATCH=64
    DL_SYNTHETIC=1     skip MNIST, use the synthetic fallback set
    DL_NONIID=1        label-skew shards (each node sees ~2 digits) instead of IID. With IID
                       data every node learns nearly the same model and the topology hardly
                       matters; non-IID is where the graph choice shows up.
    DL_NODE_ID=k       this node's shard when the two ends run as separate processes
    DL_NODES=n         how many shards to split the data into, in that same case
"""
import os, sys
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "_shared"))
from fl_core import (get_dataset, iid_shards, label_shards,               # noqa: E402
                     local_train, average_models)
from mnist_sgd_over_sdr import TinyMLP                                      # noqa: E402


# ── every node is a peer. "peer" = one node running as its own process (--node k);
#    on a plain point-to-point link one of the two ends still has to speak first. ──
ROLES = {"peer": "peer", "initiator": "tx", "responder": "rx"}


def _env(name, cast, default):
    v = os.environ.get(name)
    return default if v is None or v == "" else cast(v)


HIDDEN      = _env("DL_HIDDEN", int, 64)
ROUNDS      = _env("DL_ROUNDS", int, 20)
LOCAL_STEPS = _env("DL_LOCAL_STEPS", int, 30)
LR          = _env("DL_LR", float, 0.1)
BATCH       = _env("DL_BATCH", int, 64)
SYNTHETIC   = _env("DL_SYNTHETIC", int, 0)
NONIID      = _env("DL_NONIID", int, 0)      # 1 = label-skew split (topology starts to matter)


class Peer:
    def __init__(self, role, index=None, total=None):
        # index/total come from the group runner; the env vars cover the separate-process
        # case, where each host is started by hand and has to be told which shard is its own
        self.id = _env("DL_NODE_ID", int, 0) if index is None else int(index)
        self.n_nodes = _env("DL_NODES", int, 2) if total is None else int(total)
        self.role, self.rounds, self.r = role, ROUNDS, 0
        Xtr, ytr, self.Xte, self.yte = get_dataset(synthetic=bool(SYNTHETIC), seed=0)
        split = label_shards if NONIID else iid_shards
        self.X, self.y = split(Xtr, ytr, self.n_nodes, seed=1)[self.id]
        self.model = TinyMLP(hidden=HIDDEN, seed=0)        # same init on every node
        self.rng = np.random.RandomState(100 + self.id)
        self.buf = []                                      # neighbour models, this round
        self.spec = ("float32", (self.model.n_params,))
        # running as my own process (--node k): this terminal is mine, so report every round.
        # In the all-in-one-process runner only node 0 narrates, to keep the log readable.
        self.solo = os.environ.get("UNION_ROLE") == "peer"
        if self.id == 0 or self.solo:
            print(f"[dl] {self.n_nodes} peers, {len(self.X)} samples each "
                  f"({'NON-IID label-skew' if NONIID else 'IID'}), hidden={HIDDEN}, "
                  f"{LOCAL_STEPS} local SGD steps/round, {self.model.n_params} params/exchange")

    # ── WHAT TO TRANSMIT: consensus mix, then local SGD, then send ──
    def transmit(self):
        if self.r >= self.rounds:
            return None
        self.r += 1
        if self.buf:                                       # mix in the neighbours' models
            self.model._set_flat(average_models([self.model.get_flat()] + self.buf))
            self.buf = []
        local_train(self.model, self.X, self.y, LOCAL_STEPS, LR, BATCH, self.rng)
        if self.id == 0 or self.solo:
            print(f"    [dl] round {self.r}: node{self.id} test-acc="
                  f"{self.model.accuracy(self.Xte, self.yte):.4f}")
        return self.model.get_flat().astype(np.float32)

    # ── WHAT TO RECEIVE: a neighbour's model, held for the next mix ──
    def receive(self, msg):
        self.buf.append(np.asarray(msg, np.float32))

    def accuracy(self):
        return self.model.accuracy(self.Xte, self.yte)


def make(role, index=None, total=None):
    return Peer(role, index, total)


def report(nodes):
    """End-of-run summary. With the whole network in one process this answers the real
    question — did the peers reach consensus? A standalone node can only speak for itself."""
    accs = [nd.accuracy() for nd in nodes]
    if len(nodes) == 1:
        print(f"[dl] node {nodes[0].id}: final test-acc={accs[0]:.4f} "
              f"after {nodes[0].r} rounds with neighbours over the graph")
        return
    flats = np.stack([nd.model.get_flat() for nd in nodes])
    spread = float(np.abs(flats - flats.mean(0)).max())    # max deviation from the mean model
    print(f"[dl] per-node test-acc: [{', '.join(f'{a:.4f}' for a in accs)}]")
    print(f"[dl] mean={np.mean(accs):.4f}  min={np.min(accs):.4f}  max={np.max(accs):.4f}  "
          f"| model disagreement (max |w_i - w_mean|)={spread:.5f}")
