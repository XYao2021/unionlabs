#!/usr/bin/env python3
"""
marl_multi_env.py — MULTI-AGENT random-access environment (the CONTENTION regime).

N agents share ONE Access Point. Every slot each agent independently decides
transmit / defer, and the shared medium resolves them:

    0 transmitters   -> idle slot (wasted)
    1 transmitter    -> a REAL burst -> real ACK / loss   (single-agent PHY physics)
    >=2 transmitters -> COLLISION    -> none decode, no ACK (the shared-medium rule)

This is the regime where a learned MARL policy earns its keep over q-ALOHA: agents
must LEARN to *not* transmit in the same slot (stagger / back off) or they waste it
for everyone. With a single agent there is no collision partner, so aggressive
"always transmit" is optimal and MARL can only match a fixed-p baseline — here it
can genuinely beat it.

Channels are dependency-injected and share one interface,
    channel.step(tx_flags: list[bool]) -> delivered: list[bool]
    channel.sense() -> {'busy':bool,'power_db':float}
    channel.close()
so the env code is identical offline and on hardware:

  - MockMultiChannel : offline collision + per-link delivery model. RUN TODAY, no radio.
  - MultiRealChannel : N warm sources (one B210 each) -> one AP. Real single-tx
                       physics; >=2 overlapping bursts are a logical collision by
                       default (reliable) or physically fired with `physical=True`.
                       Needs N agent radios + 1 AP radio (test when the 3rd USRP is in).

Observation per agent (matches marl_ra, dim_O = num_D + 2):
    [ AoI_0/60, ..., AoI_{N-1}/60,  own_queue/Q_max,  channel_busy_last_slot ]
so each agent sees every agent's Age-of-Information (the coordination signal) plus
its own backlog and whether the channel was used last slot.
"""
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "drivers", "usrp", "python"))


# ─────────────────────────────────────────────────────────────────────────────
#  Channels
# ─────────────────────────────────────────────────────────────────────────────
class MockMultiChannel:
    """Offline stand-in for MultiRealChannel. Exactly-one-transmitter succeeds with
    prob `deliver_p` (the real link's per-burst delivery rate); two or more collide
    (all fail). Lets you validate the multi-agent learning with no radios."""

    def __init__(self, num_D=2, deliver_p=0.85, seed=0):
        self.num_D = num_D
        self.deliver_p = deliver_p
        self._r = random.Random(seed)

    def step(self, tx_flags):
        idx = [i for i, f in enumerate(tx_flags) if f]
        delivered = [False] * self.num_D
        if len(idx) == 1 and self._r.random() < self.deliver_p:
            delivered[idx[0]] = True          # lone transmitter -> maybe delivered
        return delivered                       # >=2 -> collision (all False)

    def sense(self):
        return {"busy": False, "power_db": -20.0}

    def close(self):
        pass


class MultiRealChannel:
    """N warm transmitters (one B210 per agent) sharing ONE Access Point. A slot with
    exactly one transmitter fires a real burst and returns its real ACK/loss; a slot
    with >=2 transmitters is a collision.

    physical=False (default, RELIABLE): a >=2 slot is resolved logically (no ACK to
      anyone) without firing — the medium-access DECISION is what's being learned and
      the collision outcome is a known property of the shared medium. Robust for a
      first hardware bring-up (no burst-timing alignment needed).
    physical=True (stretch): the transmitters actually fire simultaneously (threads),
      so the overlap physically garbles at the AP. More "real" but timing-sensitive.

    Needs len(tx_args_list) agent radios + 1 AP radio. The AP (marl_phy.py ap,
    serve-forever) must already be running and its scheme MUST match `scheme`."""

    def __init__(self, tx_args_list, tx_gain=89, scheme="QPSK", ack_host="127.0.0.1",
                 ack_port=5599, timeout_ms=2000, physical=False, binary=None, **opts):
        from marl_phy import WarmSource
        self.num_D = len(tx_args_list)
        self.physical = physical
        self._tx = []
        for a in tx_args_list:
            self._tx.append(WarmSource(tx_args=a, tx_gain=tx_gain, scheme=scheme,
                                       ack_host=ack_host, ack_port=ack_port,
                                       timeout_ms=timeout_ms, binary=binary, **opts))

    def step(self, tx_flags):
        idx = [i for i, f in enumerate(tx_flags) if f]
        delivered = [False] * self.num_D
        if len(idx) == 1:
            delivered[idx[0]] = self._tx[idx[0]].fire()          # real single burst
        elif len(idx) >= 2 and self.physical:
            import threading
            res = {}

            def _fire(i):
                try:
                    res[i] = self._tx[i].fire()
                except Exception:
                    res[i] = False
            ts = [threading.Thread(target=_fire, args=(i,)) for i in idx]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            delivered = [res.get(i, False) for i in range(self.num_D)]
        # else (>=2, logical): collision — all remain False
        return delivered

    def sense(self):
        return {"busy": False, "power_db": -20.0}

    def close(self):
        for t in self._tx:
            try:
                t.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
#  Multi-agent environment
# ─────────────────────────────────────────────────────────────────────────────
class MultiAgentRAEnv:
    """N-agent slotted random access over an injected multi-channel. step() takes a
    LIST of N actions and returns LISTS of N obs / rewards (one per agent)."""

    def __init__(self, channel, num_D=2, objective=0, pkt_int=10, Q_max=50,
                 num_S=600, seed=0, coll_penalty=1.0):
        self.ch = channel
        self.num_D = num_D
        self.objective = objective
        self.pkt_int = pkt_int
        self.Q_max = Q_max
        self.num_S = num_S
        # explicit penalty for transmitting INTO a collision. Without it, an agent
        # that collides sees the same reward as one that defers (no delivery either
        # way) -> no gradient to back off -> independent learners just both spam and
        # collide. Penalising a wasted/colliding transmit makes "defer when busy" the
        # learned behaviour, so coordination emerges. 0 recovers the naive reward.
        self.coll_penalty = coll_penalty
        self.dim_O = num_D + 2                 # [AoI x num_D, own queue, ch_busy]
        self.dim_A = 2                         # {0 defer, 1 transmit}
        self._rng = np.random.RandomState(seed)
        self.reset()

    def reset(self):
        self.t = 0
        self.queue = [1] * self.num_D
        self.since = [0] * self.num_D          # Age-of-Information per agent
        self.delivered = [0] * self.num_D
        self.collisions = 0                    # slots with >=2 transmitters
        self.idle = 0                          # slots with 0 transmitters
        self.busy_slots = 0                    # slots with >=1 transmitter
        self.deliv_now = [False] * self.num_D
        self._collided = [False] * self.num_D     # transmitted into a >=2 slot
        self._last_busy = 0
        # independent Poisson arrivals per agent (like the sim's generate_pkt)
        self._arr = np.zeros((self.num_D, self.num_S), dtype=int)
        for a in range(self.num_D):
            i = self._rng.poisson(self.pkt_int)
            while i < self.num_S:
                self._arr[a, i] = 1
                i += max(1, self._rng.poisson(self.pkt_int))
        return self._obs()

    def _obs(self):
        aoi = [s / 60.0 for s in self.since]
        return [np.array(aoi + [self.queue[a] / self.Q_max, float(self._last_busy)],
                         dtype=np.float32) for a in range(self.num_D)]

    def _reward(self, a):
        pen = self.coll_penalty if self._collided[a] else 0.0
        if self.objective == 0:                # fair Age-of-Information (minus collision cost)
            return -1.0 * self.since[a] / (15.0 * self.num_D) - pen
        # objective 1: throughput (+1 delivered) minus collision cost
        return (1.0 if self.deliv_now[a] else 0.0) - pen

    def step(self, actions):
        """actions: list of N ints in {0,1}. Returns (obs_list, reward_list, done, info)."""
        # 1) arrivals
        for a in range(self.num_D):
            if self._arr[a, min(self.t, self.num_S - 1)] and self.queue[a] < self.Q_max:
                self.queue[a] += 1
        # 2) who actually transmits (chose to AND has a packet queued)
        tx_flags = [bool(actions[a] == 1 and self.queue[a] > 0) for a in range(self.num_D)]
        n_tx = sum(tx_flags)
        # 3) shared medium resolves the slot
        delivered = self.ch.step(tx_flags)
        self.deliv_now = [False] * self.num_D
        # an agent "collided" if it transmitted in a slot with >=2 transmitters
        self._collided = [tx_flags[a] and n_tx >= 2 for a in range(self.num_D)]
        for a in range(self.num_D):
            if delivered[a]:
                self.queue[a] -= 1
                self.since[a] = 0              # AoI reset on a real ACK
                self.delivered[a] += 1
                self.deliv_now[a] = True
        if n_tx >= 2:
            self.collisions += 1
        elif n_tx == 0:
            self.idle += 1
        if n_tx >= 1:
            self.busy_slots += 1
        # 4) time advances; AoI grows
        for a in range(self.num_D):
            self.since[a] += 1
        self._last_busy = 1 if n_tx > 0 else 0
        self.t += 1
        rewards = [self._reward(a) for a in range(self.num_D)]
        done = self.t >= self.num_S
        info = {"n_tx": n_tx, "collision": n_tx >= 2, "tx_flags": tx_flags,
                "delivered": delivered, "queues": list(self.queue),
                "aoi": list(self.since)}
        return self._obs(), rewards, done, info


def make_real_multi_env(tx_args_list, num_D=None, objective=0, num_S=600, pkt_int=10,
                        tx_gain=89, scheme="QPSK", physical=False, coll_penalty=1.0,
                        **ch_kw):
    """Build a MultiAgentRAEnv on real hardware. Start the AP (marl_phy.py ap,
    matching scheme) on the AP radio first; each entry in tx_args_list is one agent
    radio (e.g. ['serial=30CD424', 'serial=<3rd>'])."""
    num_D = num_D if num_D is not None else len(tx_args_list)
    ch = MultiRealChannel(tx_args_list, tx_gain=tx_gain, scheme=scheme,
                          physical=physical, **ch_kw)
    return MultiAgentRAEnv(ch, num_D=num_D, objective=objective, num_S=num_S,
                           pkt_int=pkt_int, coll_penalty=coll_penalty), ch


def main(argv):
    """Self-test: run a few slots with a random policy (mock by default)."""
    import argparse
    a = argparse.ArgumentParser(description="Multi-agent RA env (self-test)")
    a.add_argument("--agents", type=int, default=2)
    a.add_argument("--epochs", type=int, default=20)
    a.add_argument("--objective", type=int, default=0)
    a.add_argument("--deliver-p", type=float, default=0.85)
    args = a.parse_args(argv)

    env = MultiAgentRAEnv(MockMultiChannel(num_D=args.agents, deliver_p=args.deliver_p),
                          num_D=args.agents, objective=args.objective, num_S=args.epochs)
    obs = env.reset()
    for i in range(args.epochs):
        actions = [1 if random.random() < 0.5 else 0 for _ in range(args.agents)]
        obs, rewards, done, info = env.step(actions)
        tag = "COLLISION" if info["collision"] else ("idle" if info["n_tx"] == 0 else "ok")
        print("[slot %2d] acts=%s n_tx=%d %-9s deliv=%s q=%s r=%s"
              % (i + 1, actions, info["n_tx"], tag,
                 [int(d) for d in info["delivered"]], info["queues"],
                 ["%+.2f" % r for r in rewards]))
        if done:
            break
    print("[env] delivered=%s collisions=%d idle=%d"
          % (env.delivered, env.collisions, env.idle))


if __name__ == "__main__":
    main(sys.argv[1:])
