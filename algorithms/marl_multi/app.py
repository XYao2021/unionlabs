#!/usr/bin/env python3
"""
marl_multi/app.py — REAL multi-agent random access (the regime where MARL beats fixed-p ALOHA).

N independent agents share one slotted medium and one access point. Each slot every agent's
actor chooses transmit/defer; **>=2 transmitters collide (nobody decodes)**, exactly 1 gets
through and is ACKed. Each agent runs its own online A2C and — crucially — can only see its
OWN no-ACK (not the collision), so it must *learn to back off*. With a collision penalty the
agents converge to the symmetric-optimal rate P(transmit) ≈ 1/N (they discover it without being
told), which fixed-p ALOHA cannot do without hand-tuning.

Reuses the REAL policy networks (`actor_MLP`/`critic_MLP` from MARL_RA_Union). Needs torch.
The multi-node PHY driver is `phy_link.run_slotted` — the burst→modem hand-off (the port to the
PHY) is the `channel.transfer(...)` call inside it; swap the channel for the real radio to run
it over USRPs.

Run:  ./run.sh --algo marl_multi --role multi --agents 4 --steps 1500
"""
import os, sys
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "applications", "MARL_RA_Union"))
import torch                                                   # noqa: E402
from torch.distributions.categorical import Categorical        # noqa: E402
from MARL_learning_Union import actor_MLP, critic_MLP          # the REAL policy nets  # noqa: E402

DIM_O, DIM_A, WIDTH, DEPTH = 3, 2, 64, 2
GAMMA, LR, CLIP = 0.9, 4e-4, 10.0
LAM, Q_MAX, COLL_PEN = 0.2, 20, 1.0      # arrivals (moderate load), queue cap, collision penalty
LOG_EVERY = 300
BURST = np.arange(8, dtype=np.float32)
_next = [0]                              # hands out a distinct id/seed per make() call


class Agent:
    spec = ("float32", (8,))

    def __init__(self, aid, seed):
        self.aid = aid
        torch.manual_seed(seed); self.rng = np.random.RandomState(seed)
        self.actor = actor_MLP(DIM_O, WIDTH, DEPTH, DIM_A, 0)   # ac_type 0 = A2C (softmax)
        self.critic = critic_MLP(DIM_O, WIDTH, DEPTH)
        self.opt_a = torch.optim.Adam(self.actor.parameters(), lr=LR, eps=1e-5)
        self.opt_c = torch.optim.Adam(self.critic.parameters(), lr=LR, eps=1e-5)
        self.t = self.queue = self.aoi = self.delivered = self.collisions = self.txs = 0
        self._pending = None

    def _obs(self):
        return np.array([self.aoi / 60.0, self.queue / Q_MAX, 0.0], np.float32)

    def transmit(self):                                        # decide: burst (transmit) or None (defer)
        self.t += 1
        self.queue = min(self.queue + int(self.rng.poisson(LAM)), Q_MAX)
        o = torch.as_tensor(self._obs(), dtype=torch.float32)
        probs = self.actor(o)
        action = int(Categorical(probs=probs).sample().item())
        v = self.critic(o)
        transmitted = action == 1 and self.queue > 0
        self.txs += int(transmitted)
        self._pending = (o, action, probs, v, transmitted)
        return BURST if transmitted else None                  # None = defer (run_slotted handles it)

    def receive(self, msg):
        pass                                                   # random access: only the ACK matters

    def on_result(self, ack):                                  # ack = did MY burst get through?
        if self._pending is None:
            return
        o, action, probs, v, transmitted = self._pending
        collided = transmitted and not ack                     # transmitted but no ACK == lost/collided
        if transmitted and ack:                                # throughput objective (slotted-ALOHA)
            self.queue -= 1; self.aoi = 0; self.delivered += 1
            reward = 1.0                                        #   deliver  -> +1
        elif collided:
            self.aoi = min(self.aoi + 1, 60); self.collisions += 1
            reward = -COLL_PEN                                  #   collide  -> -penalty (learn to back off)
        else:
            self.aoi = min(self.aoi + 1, 60)
            reward = 0.0                                        #   defer/idle -> neutral
        with torch.no_grad():
            v_next = self.critic(torch.as_tensor(self._obs(), dtype=torch.float32))
        td = reward + GAMMA * v_next - v
        self.opt_c.zero_grad(); td.pow(2).backward()
        torch.nn.utils.clip_grad_norm_(self.critic.parameters(), CLIP); self.opt_c.step()
        logp = torch.log(probs[action].clamp_min(1e-6))
        self.opt_a.zero_grad(); (-logp * td.detach()).backward()
        torch.nn.utils.clip_grad_norm_(self.actor.parameters(), CLIP); self.opt_a.step()
        self._pending = None
        if self.aid == 0 and self.t % LOG_EVERY == 0:
            print(f"    [marl_multi] agent0 step {self.t}: P(transmit)~{self.p_transmit():.2f}  "
                  f"delivered={self.delivered} collisions={self.collisions}")

    def p_transmit(self):
        with torch.no_grad():
            return self.actor(torch.tensor([0.3, 0.3, 0.0], dtype=torch.float32))[1].item()


def make(role):
    i = _next[0]; _next[0] += 1
    return Agent(i, seed=100 + i)
