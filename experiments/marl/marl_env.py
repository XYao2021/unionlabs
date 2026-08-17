#!/usr/bin/env python3
"""
marl_env.py — a gym-style environment that runs a MARL random-access agent against
the REAL radio instead of the simulator's channel model.

It keeps the MARL device model (Poisson arrivals, finite queue, Age-of-Information,
throughput) but replaces the simulated channel with an injected `channel` object
that exposes the `real_channel.RealChannel` interface:

    channel.sense()      -> {'busy': bool, 'power_db': float} | None
    channel.transmit()   -> bool     (ACK = delivered / no-ACK = collision-or-loss)

Per decision epoch (`step(action)`):
    - a packet may arrive (Poisson), enqueued if the buffer has room
    - observe: [ AoI (since last success), queue, channel-busy ]   (sim's convention)
    - if action==1 and the queue is non-empty: transmit ONE packet over the radio
        delivered (real ACK) -> dequeue, AoI reset, throughput credited
        not delivered        -> nothing (packet stays; the POLICY decides to retry)
    - AoI grows by one epoch; reward per the objective (0 fair-AoI / 1 max tput)

Observation / reward follow `marl_ra` (obs = [AoI/60, queue/Q_max, ch_usage];
objective 0 reward = -AoI/(15*num_D)), so a policy trained/eval'd here matches the
simulator's conventions. Channel is dependency-injected: pass a `RealChannel` for
hardware, or the built-in `MockChannel` to validate the env logic offline.

Single real agent (num_D=1) — the 2-B210 case (agent radio + AP radio). Real
multi-agent collisions need more radios; simulated peers are a future extension.

Usage:
    from real_channel import RealChannel, AccessPoint
    from marl_env import RealChannelEnv
    # (AccessPoint runs on the other node/terminal)
    with RealChannel(tx_args="serial=30CD424") as ch:
        env = RealChannelEnv(ch)
        obs = env.reset()
        for _ in range(env.num_S):
            action = policy(obs)                 # e.g. a trained MARL actor, or random
            obs, reward, done, info = env.step(action)
            if done: break
"""
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "drivers", "usrp", "python"))


class MockChannel:
    """Offline stand-in for RealChannel: `deliver_p` sets the ACK probability, and
    sensing reports busy with `busy_p`. Lets you validate the env without radios."""
    def __init__(self, deliver_p=0.7, busy_p=0.3, seed=0):
        self._r = random.Random(seed)
        self.deliver_p = deliver_p
        self.busy_p = busy_p

    def sense(self):
        return {"busy": self._r.random() < self.busy_p, "power_db": -20.0}

    def transmit(self):
        return self._r.random() < self.deliver_p


class RealChannelEnv:
    """Single-agent random-access environment backed by an injected channel."""

    def __init__(self, channel, num_D=1, objective=0, pkt_int=10, Q_max=50,
                 num_S=600, pkt_bits=1500 * 8, seed=0):
        self.ch = channel
        self.num_D = num_D                       # kept for obs/reward scaling parity
        self.objective = objective
        self.pkt_int = pkt_int                   # mean inter-arrival (epochs/packet)
        self.Q_max = Q_max
        self.num_S = num_S
        self.pkt_bits = pkt_bits
        self.dim_O = num_D + 2                   # [AoI x num_D, queue, ch_usage]
        self.dim_A = 2                           # {0 defer, 1 transmit}
        self._rng = np.random.RandomState(seed)
        self.reset()

    def reset(self):
        self.t = 0
        self.queue = 1
        self.since_success = 0                   # Age-of-Information (epochs)
        self.delivered = 0
        self.collisions = 0
        self._delivered_now = False
        # Poisson arrival schedule for the episode (like the sim's generate_pkt)
        self._arrivals = np.zeros(self.num_S, dtype=int)
        step_i = self._rng.poisson(self.pkt_int)
        while step_i < self.num_S:
            self._arrivals[step_i] = 1
            step_i += max(1, self._rng.poisson(self.pkt_int))
        return self._obs(ch_busy=0)

    def _obs(self, ch_busy):
        # sim convention: AoI/60, queue/Q_max, ch_usage. num_D-1 "peer" AoIs (none
        # here) padded with our own AoI so the vector width matches a num_D model.
        aoi = self.since_success / 60.0
        return np.array([aoi] * self.num_D + [self.queue / self.Q_max, float(ch_busy)],
                        dtype=np.float32)

    def _reward(self):
        if self.objective == 0:                  # fair Age-of-Information
            return -1.0 * self.since_success / (15.0 * self.num_D)
        # objective 1: throughput — +1 for each real delivery this step, else 0.
        # (Maximising sum of deliveries == maximising delivered packets/time; O(1)
        # scale so it actually drives the A2C, unlike a tiny running-average.)
        return 1.0 if self._delivered_now else 0.0

    def step(self, action):
        """action: 1 = transmit, 0 = defer. Returns (obs, reward, done, info)."""
        # 1) packet arrival
        if self._arrivals[min(self.t, self.num_S - 1)]:
            if self.queue < self.Q_max:
                self.queue += 1
        # 2) sense the channel (for the observation)
        s = self.ch.sense()
        ch_busy = int(bool(s["busy"])) if s else 0
        # 3) act: transmit one packet if the policy says so and we have one queued
        delivered = None
        attempted = bool(action == 1 and self.queue > 0)
        if attempted:
            delivered = self.ch.transmit()       # real single-shot: ACK vs collision/loss
            if delivered:
                self.queue -= 1
                self.since_success = 0           # AoI reset on a real ACK
                self.delivered += 1
            else:
                self.collisions += 1
        # 4) time advances; AoI grows
        self.since_success += 1
        self.t += 1
        self._delivered_now = bool(delivered)
        reward = self._reward()
        done = self.t >= self.num_S
        info = {"delivered": delivered, "attempted": attempted, "queue": self.queue,
                "aoi": self.since_success, "ch_busy": ch_busy}
        return self._obs(ch_busy), reward, done, info


def make_real_env(tx_args="serial=30CD424", sense_rx_args=None, num_D=1,
                  objective=0, num_S=600, **ch_kw):
    """Build a RealChannelEnv on real hardware. Remember to run an AccessPoint on
    the AP radio in another terminal/node."""
    from real_channel import RealChannel
    ch = RealChannel(tx_args=tx_args, sense_rx_args=sense_rx_args, **ch_kw)
    if sense_rx_args:
        ch.calibrate()
    return RealChannelEnv(ch, num_D=num_D, objective=objective, num_S=num_S), ch


def _random_policy(obs):
    return 1 if random.random() < 0.5 else 0


def main(argv):
    import argparse
    a = argparse.ArgumentParser(description="MARL real-radio env (self-test)")
    a.add_argument("--mock", action="store_true", help="offline: use MockChannel (no radio)")
    a.add_argument("--tx-args", default="serial=30CD424")
    a.add_argument("--sense-rx-args", default=None)
    a.add_argument("--epochs", type=int, default=20)
    a.add_argument("--objective", type=int, default=0)
    args = a.parse_args(argv)

    if args.mock:
        env = RealChannelEnv(MockChannel(deliver_p=0.7, busy_p=0.3),
                             objective=args.objective, num_S=args.epochs)
        ch = None
    else:
        env, ch = make_real_env(tx_args=args.tx_args, sense_rx_args=args.sense_rx_args,
                                objective=args.objective, num_S=args.epochs)
    try:
        obs = env.reset()
        totR = 0.0
        for i in range(args.epochs):
            action = _random_policy(obs)
            obs, reward, done, info = env.step(action)
            totR += reward
            if action == 0:
                tag = "defer"
            elif not info["attempted"]:
                tag = "TX (empty queue)"
            else:
                tag = "TX->deliver" if info["delivered"] else "TX->coll/loss"
            print("[epoch %2d] a=%d %-13s queue=%d aoi=%d busy=%d r=%.3f"
                  % (i + 1, action, tag, info["queue"], info["aoi"], info["ch_busy"], reward))
            if done:
                break
        print("[env] delivered=%d collisions=%d  sumR=%.2f" %
              (env.delivered, env.collisions, totR))
    finally:
        if ch is not None:
            ch.close()


if __name__ == "__main__":
    main(sys.argv[1:])
