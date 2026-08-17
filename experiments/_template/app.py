#!/usr/bin/env python3
"""
_template/app.py — copy this folder to experiments/<your_name>/ and fill it in.

Your algorithm only has to say WHAT TO TRANSMIT and WHAT TO RECEIVE. It needs NO
import from our framework and no PHY/radio code. Provide a plain object with:

    transmit()      -> np.ndarray | None   the output to send (None = done)
    receive(msg)                            the received array, into your algorithm
    spec (optional) = (dtype, shape)        e.g. ("float32", (8,))
    on_result(ack)  (optional)              delivered/collision bool (RL reward)

...plus a one-line make(role) so the framework can build one per node. The framework
reads this and handles the codec, round-trip, modem, and radio.

OPTIONAL — name your own roles. The PHY's node types are tx (transmits), rx (receives)
and relay (BOTH: receives from upstream, re-transmits downstream). If your algorithm
calls them something else, declare the map and the experimenter types YOUR names:

    ROLES = {"client": "tx", "server": "rx", "relay": "relay"}
    ./run.sh --algo <your_name> --role client        # == --role tx

make(role) then receives your own name, so the two always match. Omit ROLES and your
algorithm simply speaks tx/rx/relay. See experiments/fl/app.py for a worked example.

Run radio-free:  python3 union/run_algo.py --algo <your_name> --role loopback
"""
import numpy as np


class MyAlgo:
    spec = ("float32", (8,))                 # output type + shape (optional)

    def __init__(self, role):
        self.role = role                     # "tx" or "rx"
        # ... your algorithm's state here ...

    def transmit(self):                      # WHAT TO TRANSMIT  (None = done)
        if self.role == "rx":
            return np.zeros(8, np.float32)   # your reply (usually depends on the last receive)
        return np.ones(8, np.float32)        # your next output

    def receive(self, msg):                  # WHAT TO RECEIVE  (msg: a numpy array)
        pass

    # def on_result(self, ack):              # OPTIONAL: delivered/collision reward
    #     pass


def make(role):                              # the framework calls this per node/role
    return MyAlgo(role)


# For an EXISTING algorithm, leave it untouched in its own file and just map its
# methods here, e.g.:
#     from my_algorithm import Model
#     def make(role):
#         m = Model()
#         class Bind:
#             spec = ("float32", (m.dim,))
#             transmit = m.next_output          # your callable -> what to transmit
#             receive  = m.take_input           # your callable(msg) -> what to receive
#         return Bind()
