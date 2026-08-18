#!/usr/bin/env python3
"""
echo/app.py — the worked example / smoke test for the uniform algorithm API.

Round-trip: the tx sends a random float32 vector; the rx replies with
2x it; the tx checks that the reply equals 2 x its request. This exercises the
whole contract (produce -> transmit -> consume -> reply -> transmit -> consume) end to
end, with no radio.

    python3 union/run_algo.py --algo echo --role loopback
    PYTHONPATH=drivers/usrp/bindings arch -x86_64 python3 union/run_algo.py \
        --algo echo --role loopback --channel usrp --sim-snr-db 6
"""
import os, sys
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "union"))
from phy_link import SdrApp, PayloadSpec          # noqa: E402

N = 16


class App(SdrApp):
    spec = PayloadSpec("float32", (N,))

    def __init__(self, role="tx", steps=5, seed=0):
        super().__init__(role)
        self.steps, self.t = steps, 0
        self.rng = np.random.RandomState(seed)
        self.sent = None      # tx: last request  |  rx: last consumed
        self.matches = 0

    def produce(self):
        if self.role == "rx":                       # reply = 2x what we consumed
            base = self.sent if self.sent is not None else np.zeros(N, np.float32)
            return (2.0 * base).astype(np.float32)
        if self.t >= self.steps:                           # tx: done
            return None
        self.t += 1
        self.sent = self.rng.randn(N).astype(np.float32)
        return self.sent

    def consume(self, msg):
        if self.role == "rx":
            self.sent = msg                                # remember input for the reply
        else:                                              # tx: check the reply
            ok = msg is not None and np.allclose(msg, 2.0 * self.sent, atol=1e-3)
            self.matches += int(ok)
            print(f"    [echo] reply == 2 x request ? {ok}")

    def on_result(self, ack):
        pass
