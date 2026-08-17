#!/usr/bin/env python3
"""
echo_algo.py — a PLAIN algorithm. It knows nothing about the PHY, the radio, or the
phy_link framework. It only declares WHAT TO TRANSMIT and WHAT TO RECEIVE. This could
be any pre-existing user code — nothing here imports our framework.
"""
import numpy as np


class EchoAlgo:
    spec = ("float32", (16,))               # output type + shape (a plain tuple)

    def __init__(self, role, steps=5):
        self.role, self.steps, self.t = role, steps, 0
        self.rng = np.random.RandomState(0)
        self.sent = self.last = None
        self.matches = 0

    def transmit(self):                      # WHAT TO TRANSMIT  (None = done)
        if self.role == "rx":
            base = self.last if self.last is not None else np.zeros(16, np.float32)
            return (2.0 * base).astype(np.float32)
        if self.t >= self.steps:
            return None
        self.t += 1
        self.sent = self.rng.randn(16).astype(np.float32)
        return self.sent

    def receive(self, msg):                  # WHAT TO RECEIVE
        if self.role == "rx":
            self.last = msg
        else:
            ok = msg is not None and np.allclose(msg, 2.0 * self.sent, atol=1e-3)
            self.matches += int(ok)
            print(f"    [plain_echo] reply == 2 x request ? {ok}")
