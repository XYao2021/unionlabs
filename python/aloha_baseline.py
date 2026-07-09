#!/usr/bin/env python3
"""
aloha_baseline.py — q-ALOHA (fixed transmit-probability) baseline over the REAL
channel, for head-to-head comparison with the learned MARL policy.

q-ALOHA is the classic random-access benchmark: each epoch, transmit with a FIXED
probability p (no learning, no state) — the `device_type 1` baseline in
MARL_RA_Union. This runs it over `marl_env.RealChannelEnv` (same device model,
same real DQPSK link, same reward), sweeps p, and reports delivery rate / mean AoI
/ throughput. With `--actor <path>` it also evaluates a trained MARL actor
(inference) on the same link and overlays it as a reference.

    # AP node
    python3 real_channel.py ap --rx-args serial=30CD3F7

    # sweep q-ALOHA over p (+ compare a trained MARL actor)
    python3 aloha_baseline.py --sweep 0.25,0.5,0.75,1.0 --steps 100 \
        --actor ../applications/MARL_RA_Union/results/real_dqpsk_400.pt

    # offline logic check (no radio)
    python3 aloha_baseline.py --sweep 0.25,0.5,0.75,1.0 --mock --steps 300

For a SINGLE agent (no contention) the optimum is p->1 (always transmit); the MARL
policy learned P(transmit|queued)~0.9, so it should sit near the best fixed-p.
Where q-ALOHA and MARL *separate* is multi-agent contention (needs more radios).
"""
import argparse
import os
import random
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from marl_env import RealChannelEnv, MockChannel                # noqa: E402


def run_policy(env, policy, steps):
    """Roll a policy(obs)->action over the env; collect metrics + reward trace."""
    obs = env.reset()
    aois, rewards = [], []
    delivered = tx = 0
    for _ in range(steps):
        a = policy(obs)
        obs, r, done, info = env.step(a)
        rewards.append(r)
        aois.append(info["aoi"])
        if info["attempted"]:
            tx += 1
            delivered += bool(info["delivered"])
        if done:
            obs = env.reset()
    return dict(mean_aoi=float(np.mean(aois)), delivered=delivered, tx=tx,
                delivery_rate=(delivered / tx if tx else 0.0),
                deliv_per_step=delivered / steps, sumR=float(np.sum(rewards)))


def aloha_policy(p):
    return lambda obs: 1 if random.random() < p else 0


def run_interleaved(named_policies, envs, steps_per_policy, block):
    """Round-robin the policies in small blocks over ONE shared channel, so every
    policy samples the SAME CFO windows (removes the sequential-window confound).
    envs share the channel; give them the same seed for identical traffic."""
    st = [{"obs": e.reset(), "aois": [], "rewards": [], "deliv": 0, "tx": 0, "n": 0}
          for e in envs]
    while any(s["n"] < steps_per_policy for s in st):
        for i, (_, pol) in enumerate(named_policies):
            for _ in range(block):
                if st[i]["n"] >= steps_per_policy:
                    break
                obs2, r, done, info = envs[i].step(pol(st[i]["obs"]))
                st[i]["rewards"].append(r); st[i]["aois"].append(info["aoi"])
                if info["attempted"]:
                    st[i]["tx"] += 1; st[i]["deliv"] += bool(info["delivered"])
                st[i]["obs"] = envs[i].reset() if done else obs2
                st[i]["n"] += 1
    out = []
    for (name, _), s in zip(named_policies, st):
        out.append(dict(name=name, mean_aoi=float(np.mean(s["aois"])),
                        delivered=s["deliv"], tx=s["tx"],
                        delivery_rate=(s["deliv"] / s["tx"] if s["tx"] else 0.0),
                        deliv_per_step=s["deliv"] / max(1, s["n"]),
                        sumR=float(np.sum(s["rewards"]))))
    return out


def actor_policy(path, dim_O, dim_A):
    import torch
    from torch.distributions.categorical import Categorical
    from marl_train import build_nets
    from MARL_setting_Union import LearningSettings
    cf = LearningSettings(); cf.dim_W, cf.dim_D, cf.net_type = 64, 2, 0
    actor, _ = build_nets(dim_O, dim_A, cf)
    actor.load_state_dict(torch.load(path)); actor.eval()

    def pol(obs):
        with torch.no_grad():
            probs = actor(torch.as_tensor(obs, dtype=torch.float32))
        return int(Categorical(probs=probs).sample().item())
    return pol


def make_env(args):
    if args.mock:
        return RealChannelEnv(MockChannel(deliver_p=args.deliver_p, busy_p=0.0),
                              objective=args.objective, num_S=args.steps), None
    from real_channel import RealChannel
    ch = RealChannel(tx_args=args.tx_args, tx_gain=args.tx_gain,
                     rx_gain=args.rx_gain, scheme=args.scheme)
    return RealChannelEnv(ch, objective=args.objective, num_S=args.steps), ch


def main(argv):
    a = argparse.ArgumentParser(description="q-ALOHA baseline over the real channel")
    a.add_argument("--mock", action="store_true")
    a.add_argument("--tx-args", default="serial=30CD424")
    a.add_argument("--tx-gain", type=float, default=85)
    a.add_argument("--rx-gain", type=float, default=20)
    a.add_argument("--scheme", default="DQPSK")
    a.add_argument("--objective", type=int, default=0)
    a.add_argument("--steps", type=int, default=100)
    a.add_argument("--p", type=float, default=None, help="single fixed-p run")
    a.add_argument("--sweep", default=None, help="comma list of p, e.g. 0.25,0.5,0.75,1.0")
    a.add_argument("--actor", default=None, help="trained MARL actor .pt to compare")
    a.add_argument("--interleave", type=int, default=0,
                   help="block size for interleaved (window-matched) comparison; "
                        "0 = sequential sweep. --steps is then per-policy.")
    a.add_argument("--deliver-p", type=float, default=0.85, help="mock link success prob")
    a.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "applications", "MARL_RA_Union", "results", "aloha_vs_marl.png"))
    args = a.parse_args(argv)

    ps = ([args.p] if args.p is not None
          else [float(x) for x in args.sweep.split(",")] if args.sweep
          else [0.25, 0.5, 0.75, 1.0])

    # ── interleaved (window-matched) comparison of specific policies ──
    if args.interleave:
        from real_channel import RealChannel
        ch = (None if args.mock else RealChannel(tx_args=args.tx_args, tx_gain=args.tx_gain,
              rx_gain=args.rx_gain, scheme=args.scheme))
        mk = (lambda: RealChannelEnv(MockChannel(deliver_p=args.deliver_p, busy_p=0.0),
                                     objective=args.objective, num_S=600)) if args.mock \
            else (lambda: RealChannelEnv(ch, objective=args.objective, num_S=600))
        named = [("q-ALOHA p=%.2f" % p, aloha_policy(p)) for p in ps]
        envs = [mk() for _ in ps]
        if args.actor:
            envs.append(mk())
            named.append(("MARL", actor_policy(args.actor, envs[0].dim_O, envs[0].dim_A)))
        try:
            res = run_interleaved(named, envs, args.steps, args.interleave)
        finally:
            if ch is not None:
                ch.close()
        for m in res:
            print("[%-14s] delivery=%.2f (%d/%d)  mean-AoI=%.2f  deliv/step=%.3f"
                  % (m["name"], m["delivery_rate"], m["delivered"], m["tx"],
                     m["mean_aoi"], m["deliv_per_step"]))
        _plot_bars(res, args.out)
        return

    rows = []
    env, ch = make_env(args)
    try:
        for p in ps:
            m = run_policy(env, aloha_policy(p), args.steps)
            m["p"] = p
            rows.append(m)
            print("[q-ALOHA p=%.2f] delivery=%.2f (%d/%d)  mean-AoI=%.2f  deliv/step=%.3f"
                  % (p, m["delivery_rate"], m["delivered"], m["tx"], m["mean_aoi"],
                     m["deliv_per_step"]))
        marl = None
        if args.actor:
            marl = run_policy(env, actor_policy(args.actor, env.dim_O, env.dim_A), args.steps)
            print("[MARL actor]      delivery=%.2f (%d/%d)  mean-AoI=%.2f  deliv/step=%.3f"
                  % (marl["delivery_rate"], marl["delivered"], marl["tx"],
                     marl["mean_aoi"], marl["deliv_per_step"]))
    finally:
        if ch is not None:
            ch.close()

    _plot(rows, marl, args.out)


def _plot_bars(res, out):
    """Bar comparison for the interleaved (window-matched) experiment."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        names = [r["name"] for r in res]
        colors = ["C2" if r["name"] == "MARL" else "C0" for r in res]
        fig, ax = plt.subplots(1, 2, figsize=(max(7, 1.6 * len(res) + 4), 4.4))
        ax[0].bar(names, [r["deliv_per_step"] for r in res], color=colors)
        ax[0].set_ylabel("deliveries / step"); ax[0].set_title("Throughput (higher better)")
        ax[1].bar(names, [r["mean_aoi"] for r in res], color=colors)
        ax[1].set_ylabel("mean AoI"); ax[1].set_title("Age-of-Information (lower better)")
        for a_ in ax:
            a_.grid(alpha=.3, axis="y"); a_.tick_params(axis="x", rotation=20)
        fig.suptitle("q-ALOHA vs learned MARL — interleaved, window-matched (real DQPSK)")
        fig.tight_layout(); fig.savefig(out, dpi=110); plt.close(fig)
        print("[baseline] plot -> %s" % os.path.abspath(out))
    except Exception as e:
        print("[baseline] (plot skipped: %s)" % e)


def _plot(rows, marl, out):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        p = [r["p"] for r in rows]
        fig, ax = plt.subplots(1, 2, figsize=(11, 4.2))
        ax[0].plot(p, [r["deliv_per_step"] for r in rows], "o-", label="q-ALOHA")
        ax[1].plot(p, [r["mean_aoi"] for r in rows], "o-", color="C3", label="q-ALOHA")
        if marl:
            ax[0].axhline(marl["deliv_per_step"], ls="--", color="C2",
                          label="MARL (learned)")
            ax[1].axhline(marl["mean_aoi"], ls="--", color="C2", label="MARL (learned)")
        ax[0].set_xlabel("q-ALOHA transmit prob p"); ax[0].set_ylabel("deliveries / step")
        ax[0].set_title("Throughput"); ax[0].grid(alpha=.3); ax[0].legend()
        ax[1].set_xlabel("q-ALOHA transmit prob p"); ax[1].set_ylabel("mean AoI")
        ax[1].set_title("Age-of-Information (lower better)"); ax[1].grid(alpha=.3); ax[1].legend()
        fig.suptitle("q-ALOHA baseline vs learned MARL — real DQPSK link")
        fig.tight_layout(); fig.savefig(out, dpi=110); plt.close(fig)
        print("[baseline] plot -> %s" % os.path.abspath(out))
    except Exception as e:
        print("[baseline] (plot skipped: %s)" % e)


if __name__ == "__main__":
    main(sys.argv[1:])
