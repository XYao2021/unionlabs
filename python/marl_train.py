#!/usr/bin/env python3
"""
marl_train.py — ONLINE reinforcement-learning of a random-access policy against the
REAL radio, using the MARL_RA_Union actor-critic networks + the SDR PHY.

Two devices, one link: an agent (transmitter) learns WHEN to transmit, and an
Access Point (receiver) ACKs decoded frames. The reward comes from the *real*
channel (ACK = delivered, timeout = collision/loss). This is single-agent A2C
(the num_D=1 case of the MARL setup); it reuses `MARL_learning_Union.Actor_A2C`
and `Critic`, and `marl_env.RealChannelEnv` for the device model + reward.

Run the Access Point in one terminal, then train in another:

    # AP node
    python3 real_channel.py ap --rx-args serial=30CD3F7

    # agent node — online training on the real link
    python3 marl_train.py --tx-args serial=30CD424 --steps 150

Validate the learning loop with NO radio first:

    python3 marl_train.py --mock --steps 400

Online A2C per step: obs -> actor softmax -> sample {defer, transmit} -> env.step
(a real burst if transmit) -> reward -> one-step TD update of critic + actor.
Saves the trained actor to --out (default marl_actor.pt).
"""
import argparse
import os
import sys

import numpy as np
import torch
from torch.distributions.categorical import Categorical

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "applications", "MARL_RA_Union"))
from MARL_learning_Union import Actor_A2C, Critic          # noqa: E402
from MARL_setting_Union import LearningSettings             # noqa: E402
from marl_env import RealChannelEnv, MockChannel            # noqa: E402


def build_nets(dim_O, dim_A, cf):
    actor = Actor_A2C(dim_O, cf.dim_W, cf.dim_D, dim_A, cf.net_type, 0, cf)   # ac_type 0 = A2C
    critic = Critic(dim_O, cf.dim_W, cf.dim_D, cf.net_type, cf)
    return actor, critic


def train(env, cf, steps, lr, out, log_every=10):
    dim_O, dim_A = env.dim_O, env.dim_A
    actor, critic = build_nets(dim_O, dim_A, cf)
    opt_a = torch.optim.Adam(actor.parameters(), lr=lr, eps=1e-5)
    opt_c = torch.optim.Adam(critic.parameters(), lr=lr, eps=1e-5)
    print("[train] A2C  dim_O=%d dim_A=%d  width=%d depth=%d  lr=%.1e  gamma=%.2f"
          % (dim_O, dim_A, cf.dim_W, cf.dim_D, lr, cf.GAMMA))
    print("[train] actor params=%d" % sum(p.numel() for p in actor.parameters()))

    obs = env.reset()
    ret = deliv = coll = tx = 0
    rewards = []
    for t in range(steps):
        o = torch.as_tensor(obs, dtype=torch.float32)
        probs = actor(o)                                    # softmax over {defer, transmit}
        dist = Categorical(probs=probs)
        action = int(dist.sample().item())

        next_obs, reward, done, info = env.step(action)
        rewards.append(reward)
        ret += reward
        if info["attempted"]:
            tx += 1
            deliv += bool(info["delivered"])
            coll += (not info["delivered"])

        # one-step advantage-actor-critic update from the REAL reward
        v = critic(o)
        with torch.no_grad():
            v_next = critic(torch.as_tensor(next_obs, dtype=torch.float32))
        td = reward + cf.GAMMA * (1.0 - float(done)) * v_next - v
        critic_loss = td.pow(2)
        opt_c.zero_grad(); critic_loss.backward()
        torch.nn.utils.clip_grad_norm_(critic.parameters(), cf.critic_clip); opt_c.step()

        logp = torch.log(probs[action].clamp_min(1e-6))
        actor_loss = -logp * td.detach()
        opt_a.zero_grad(); actor_loss.backward()
        torch.nn.utils.clip_grad_norm_(actor.parameters(), cf.actor_clip); opt_a.step()

        obs = env.reset() if done else next_obs

        if (t + 1) % log_every == 0:
            with torch.no_grad():
                # transmit prob when a packet is queued and AoI is moderate
                p_tx = actor(torch.tensor([0.1, 0.1, 0.0], dtype=torch.float32))[1].item()
            print("[%3d/%d] avgR=%+.3f  deliv=%d coll=%d tx=%d  P(transmit|queued)=%.2f"
                  % (t + 1, steps, np.mean(rewards[-log_every:]), deliv, coll, tx, p_tx))

    torch.save(actor.state_dict(), out)
    print("[train] done. delivered=%d collisions=%d tx=%d over %d steps; sumR=%.1f"
          % (deliv, coll, tx, steps, ret))
    print("[train] saved actor -> %s" % out)


def main(argv):
    a = argparse.ArgumentParser(description="Online MARL training over the real radio")
    a.add_argument("--mock", action="store_true", help="offline: MockChannel, no radio")
    a.add_argument("--tx-args", default="serial=30CD424")
    a.add_argument("--sense-rx-args", default=None)
    a.add_argument("--steps", type=int, default=150)
    a.add_argument("--lr", type=float, default=4e-4)
    a.add_argument("--objective", type=int, default=0)
    a.add_argument("--out", default="marl_actor.pt")
    a.add_argument("--deliver-p", type=float, default=0.7, help="mock link success prob")
    args = a.parse_args(argv)

    cf = LearningSettings()
    cf.dim_W, cf.dim_D, cf.net_type = 64, 2, 0
    cf.GAMMA, cf.actor_clip, cf.critic_clip = 0.9, 10, 10

    ch = None
    if args.mock:
        env = RealChannelEnv(MockChannel(deliver_p=args.deliver_p, busy_p=0.0),
                             objective=args.objective, num_S=args.steps)
    else:
        from real_channel import RealChannel
        ch = RealChannel(tx_args=args.tx_args, sense_rx_args=args.sense_rx_args)
        if args.sense_rx_args:
            ch.calibrate()
        env = RealChannelEnv(ch, objective=args.objective, num_S=args.steps)
    try:
        train(env, cf, args.steps, args.lr, args.out)
    finally:
        if ch is not None:
            ch.close()


if __name__ == "__main__":
    main(sys.argv[1:])
