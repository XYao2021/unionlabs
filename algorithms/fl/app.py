#!/usr/bin/env python3
"""
fl/app.py — Federated Learning as a plain uploaded algorithm.

NO import from the PHY framework, no radio code — the algorithm only says what to
transmit and what to receive; a one-line make(role) binding lets the uniform API read it.

    tx (client): transmit() = its locally-trained model vector (float32)
                        receive(aggregate) = adopt the server's global model
    rx (server): receive(client_model) = collect
                        transmit() = FedAvg aggregate -> the new global

Reuses the existing FL library (TinyMLP / local_train / fedavg) unchanged.
Run:  python3 phy/python/run_algo.py --algo fl --role loopback --steps 6
"""
import os, sys
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "applications", "FL_Union"))
from fl_core import get_dataset, iid_shards, local_train, fedavg   # noqa: E402
from mnist_sgd_over_sdr import TinyMLP                              # noqa: E402

HIDDEN, ROUNDS, LOCAL_STEPS, LR, BATCH = 32, 5, 40, 0.1, 64


class FL:
    def __init__(self, role, rounds=ROUNDS):
        self.role, self.rounds, self.r = role, rounds, 0
        Xtr, ytr, self.Xte, self.yte = get_dataset(synthetic=True, seed=0)
        if role == "tx":
            self.X, self.y = iid_shards(Xtr, ytr, 1, seed=1)[0]
            self.model = TinyMLP(hidden=HIDDEN, seed=0)
            self.rng = np.random.RandomState(100)
            self.spec = ("float32", (self.model.n_params,))
        else:
            self.gm = TinyMLP(hidden=HIDDEN, seed=0)
            self.buf = []
            self.spec = ("float32", (self.gm.n_params,))

    def transmit(self):
        if self.role == "rx":                    # server: FedAvg -> new global
            if not self.buf:
                return None
            self.gm._set_flat(fedavg(self.buf)); self.buf = []
            print(f"    [fl] server global test-acc={self.gm.accuracy(self.Xte, self.yte):.3f}")
            return self.gm.get_flat().astype(np.float32)
        if self.r >= self.rounds:                        # client: done
            return None
        self.r += 1
        local_train(self.model, self.X, self.y, LOCAL_STEPS, LR, BATCH, self.rng)
        return self.model.get_flat().astype(np.float32)

    def receive(self, msg):
        m = np.asarray(msg, np.float32)
        if self.role == "rx":
            self.buf.append(m)
        else:
            self.model._set_flat(m)                      # adopt the global
            print(f"    [fl] client round {self.r}: test-acc={self.model.accuracy(self.Xte, self.yte):.3f}")


def make(role):
    return FL(role)
