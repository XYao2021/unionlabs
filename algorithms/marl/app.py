#!/usr/bin/env python3
"""
marl/app.py — single-agent random access with ONLINE A2C learning, as an uploaded algorithm.

The agent observes [AoI, queue, channel-busy], its actor chooses **transmit vs defer**, and it
LEARNS from the real ACK (delivered=reward) via one-step advantage-actor-critic — the *same*
policy networks and update rule the full stack uses (`MARL_learning_Union` + `marl_train.py`),
but driven through the uniform PHY API instead of a bespoke channel.

  - The **decision** happens in `transmit()` (observe → actor → sample transmit/defer).
  - The **A2C update** happens in `on_result(ack)`, once the outcome (delivered vs lost) is known.
  - On *defer* (or an empty queue) it sends a tiny idle marker so the round-trip continues —
    single-agent point-to-point, so no collisions. True multi-agent contention (N agents → 1 AP,
    real collisions) needs the multi-node extension; this is the single-agent learning loop.

Reuses the REAL networks from applications/MARL_RA_Union/MARL_learning_Union.py. Needs torch.

Connection to the PHY: this file has NO radio code. The burst returned by transmit() is carried
to the AP by the uniform API — the marked `channel.transfer(...)` call inside `phy_link.run_loopback`
— and the delivered/collision outcome comes back as on_result(ack). Swap the channel for the radio
backend and the same algorithm runs over USRPs.

Run:  ./run.sh --algo marl --steps 400                          # learns on the lossless channel
      ./run.sh --algo marl --channel pyphy --snr-db 4 --steps 400   # learns over the real modem
"""
import os, sys
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "applications", "MARL_RA_Union"))
import torch                                                   # noqa: E402
from torch.distributions.categorical import Categorical        # noqa: E402
from MARL_learning_Union import actor_MLP, critic_MLP          # the REAL policy nets  # noqa: E402

DIM_O, DIM_A, WIDTH, DEPTH = 3, 2, 64, 2          # obs=[AoI,queue,busy], actions={defer,transmit}
GAMMA, LR, CLIP = 0.9, 4e-4, 10.0                 # match applications/MARL_RA_Union
LAM, Q_MAX, AOI_NORM = 0.6, 20, 60.0              # Poisson arrivals, queue cap, AoI normaliser
LOG_EVERY = 50
BURST = np.arange(8, dtype=np.float32)            # a real data burst
IDLE  = np.zeros(8, dtype=np.float32)             # marker sent on defer/empty (harmless, single-agent)


class MARL:
    spec = ("float32", (8,))

    def __init__(self, role, seed=0):
        self.role = role
        if role != "tx":
            return                                # rx (AP): just acknowledges; no policy
        torch.manual_seed(seed)
        self.rng = np.random.RandomState(seed)
        self.actor = actor_MLP(DIM_O, WIDTH, DEPTH, DIM_A, 0)   # ac_type 0 = A2C (softmax head)
        self.critic = critic_MLP(DIM_O, WIDTH, DEPTH)
        self.opt_a = torch.optim.Adam(self.actor.parameters(), lr=LR, eps=1e-5)
        self.opt_c = torch.optim.Adam(self.critic.parameters(), lr=LR, eps=1e-5)
        self.t = self.queue = self.aoi = self.delivered = self.tx_when_q = self.q_slots = 0
        self.rewards = []
        self._pending = None

    def _obs(self):
        return np.array([self.aoi / AOI_NORM, self.queue / Q_MAX, 0.0], np.float32)

    def transmit(self):
        if self.role != "tx":
            return None                           # AP sends no data back
        self.t += 1
        self.queue = min(self.queue + int(self.rng.poisson(LAM)), Q_MAX)   # arrivals
        o = torch.as_tensor(self._obs(), dtype=torch.float32)
        probs = self.actor(o)
        action = int(Categorical(probs=probs).sample().item())             # 0=defer, 1=transmit
        v = self.critic(o)
        had_data = self.queue > 0
        transmitted = action == 1 and had_data
        self.q_slots += int(had_data)
        self.tx_when_q += int(action == 1 and had_data)
        self._pending = (o, action, probs, v, transmitted)
        return BURST if transmitted else IDLE

    def receive(self, msg):
        pass

    def on_result(self, ack):
        if self.role != "tx" or self._pending is None:
            return
        o, action, probs, v, transmitted = self._pending
        if transmitted and ack:                   # delivered -> serve one packet, reset AoI
            self.queue -= 1; self.aoi = 0; self.delivered += 1
        else:
            self.aoi += 1
        reward = -self.aoi / 15.0                  # objective-0 (Age-of-Information); higher=better
        self.rewards.append(reward)
        # one-step advantage-actor-critic update (mirrors marl_train.py)
        with torch.no_grad():
            v_next = self.critic(torch.as_tensor(self._obs(), dtype=torch.float32))
        td = reward + GAMMA * v_next - v
        self.opt_c.zero_grad(); td.pow(2).backward()
        torch.nn.utils.clip_grad_norm_(self.critic.parameters(), CLIP); self.opt_c.step()
        logp = torch.log(probs[action].clamp_min(1e-6))
        self.opt_a.zero_grad(); (-logp * td.detach()).backward()
        torch.nn.utils.clip_grad_norm_(self.actor.parameters(), CLIP); self.opt_a.step()
        self._pending = None
        if self.t % LOG_EVERY == 0:
            self._report()

    def _report(self):
        ptxq = (self.tx_when_q / self.q_slots) if self.q_slots else 0.0
        print(f"    [marl] step {self.t}: delivered={self.delivered}  "
              f"P(transmit|queued)={ptxq:.2f}  mean-reward(last {LOG_EVERY})="
              f"{np.mean(self.rewards[-LOG_EVERY:]):.3f}")


def make(role):
    return MARL(role)
