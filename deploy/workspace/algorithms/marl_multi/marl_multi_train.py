#!/usr/bin/env python3
"""
marl_multi_train.py — MULTI-AGENT online RL of a random-access policy (the CONTENTION
regime). N agents share one Access Point and must LEARN to avoid transmitting in the
same slot. Independent A2C: each agent has its own Actor_A2C + Critic and learns from
its own reward; coordination emerges through the shared collision feedback.

Reuses marl_ra's Actor_A2C / Critic and marl_multi_env.MultiAgentRAEnv.

VALIDATE TODAY, no radio (mock collision model):
    python3 marl_multi_train.py --mock --agents 2 --steps 500
    python3 marl_multi_train.py --mock --agents 3 --steps 800 --objective 1

RUN TOMORROW with the 3rd USRP (2 agents + 1 AP). Start the AP first (scheme MUST
match), then train with one --tx-args per agent radio:
    # terminal 1 — Access Point (RX + ACK), QPSK to match the warm-LO operating point
    python3 marl_phy.py ap --scheme QPSK --rx-args serial=30CD3F7 --rx-gain 20
    # terminal 2 — two contending agents
    python3 marl_multi_train.py --tx-args serial=30CD424 --tx-args serial=<3RD-SERIAL> \
        --scheme QPSK --tx-gain 89 --steps 300 --pkt-int 3

The plot shows the money-shot: collision rate DROPS and total throughput RISES as the
agents learn to stagger — and a q-ALOHA baseline (fixed transmit prob) it should beat.
"""
import argparse
import os
import sys

import numpy as np
import torch
from torch.distributions.categorical import Categorical

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "drivers", "usrp", "python"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "_shared"))
from marl_learning import Actor_A2C, Critic          # noqa: E402
from marl_setting import LearningSettings             # noqa: E402
from marl_multi_env import (MultiAgentRAEnv, MockMultiChannel,   # noqa: E402
                            make_real_multi_env)


def build_nets(dim_O, dim_A, cf):
    actor = Actor_A2C(dim_O, cf.dim_W, cf.dim_D, dim_A, cf.net_type, 0, cf)
    critic = Critic(dim_O, cf.dim_W, cf.dim_D, cf.net_type, cf)
    return actor, critic


def _probe_obs(num_D):
    """A representative observation for reading P(transmit|queued): moderate AoI on
    all agents, own queue non-empty, channel idle."""
    return torch.tensor([0.1] * num_D + [0.1, 0.0], dtype=torch.float32)


def train(env, cf, steps, lr, log_every=20):
    """Independent A2C over N agents. Returns (actors, history)."""
    N = env.num_D
    dim_O, dim_A = env.dim_O, env.dim_A
    actors, critics, opt_a, opt_c = [], [], [], []
    for _ in range(N):
        ac, cr = build_nets(dim_O, dim_A, cf)
        actors.append(ac)
        critics.append(cr)
        opt_a.append(torch.optim.Adam(ac.parameters(), lr=lr, eps=1e-5))
        opt_c.append(torch.optim.Adam(cr.parameters(), lr=lr, eps=1e-5))
    print("[multi-train] %d agents  A2C  dim_O=%d dim_A=%d  width=%d  lr=%.1e  gamma=%.2f"
          % (N, dim_O, dim_A, cf.dim_W, lr, cf.GAMMA))

    obs = env.reset()
    coll_hist, thru_hist, ptx_hist = [], [], [[] for _ in range(N)]
    tot_deliv = tot_coll = 0
    coll_window = []
    probe = _probe_obs(N)
    for t in range(steps):
        acts = []
        os_t = [torch.as_tensor(o, dtype=torch.float32) for o in obs]
        probs_t = []
        for a in range(N):
            probs = actors[a](os_t[a])
            probs_t.append(probs)
            acts.append(int(Categorical(probs=probs).sample().item()))

        next_obs, rewards, done, info = env.step(acts)
        tot_coll += int(info["collision"])
        tot_deliv += sum(int(d) for d in info["delivered"])
        coll_window.append(int(info["collision"]))
        coll_hist.append(np.mean(coll_window[-50:]))         # rolling collision rate
        thru_hist.append(tot_deliv / (t + 1))                # deliveries / slot

        # per-agent one-step A2C update from that agent's own real reward
        for a in range(N):
            v = critics[a](os_t[a])
            with torch.no_grad():
                v_next = critics[a](torch.as_tensor(next_obs[a], dtype=torch.float32))
            td = rewards[a] + cf.GAMMA * (1.0 - float(done)) * v_next - v
            opt_c[a].zero_grad()
            td.pow(2).backward()
            torch.nn.utils.clip_grad_norm_(critics[a].parameters(), cf.critic_clip)
            opt_c[a].step()

            logp = torch.log(probs_t[a][acts[a]].clamp_min(1e-6))
            (-logp * td.detach()).backward()
            torch.nn.utils.clip_grad_norm_(actors[a].parameters(), cf.actor_clip)
            opt_a[a].step()
            opt_a[a].zero_grad()

        with torch.no_grad():
            for a in range(N):
                ptx_hist[a].append(actors[a](probe)[1].item())

        obs = env.reset() if done else next_obs
        if (t + 1) % log_every == 0:
            ptx = [ptx_hist[a][-1] for a in range(N)]
            print("[%3d/%d] collision-rate=%.2f  throughput=%.3f/slot  deliv=%d coll=%d  "
                  "P(tx|q)=%s" % (t + 1, steps, coll_hist[-1], thru_hist[-1],
                                  tot_deliv, tot_coll, ["%.2f" % p for p in ptx]))
    hist = dict(coll=coll_hist, thru=thru_hist, ptx=ptx_hist,
                deliv=tot_deliv, collisions=tot_coll, N=N, steps=steps)
    return actors, hist


def eval_policy(env, policy_fn, steps):
    """Run a fixed multi-agent policy (no learning). policy_fn(obs_list)->actions.
    Returns dict(throughput, collision_rate, delivered, collisions)."""
    obs = env.reset()
    deliv = coll = 0
    aoi_sum = 0.0
    for t in range(steps):
        acts = policy_fn(obs)
        obs, rewards, done, info = env.step(acts)
        deliv += sum(int(d) for d in info["delivered"])
        coll += int(info["collision"])
        aoi_sum += float(np.mean(info["aoi"]))       # mean AoI across agents this slot
        if done:
            obs = env.reset()
    return dict(throughput=deliv / steps, collision_rate=coll / steps,
                mean_aoi=aoi_sum / steps, delivered=deliv, collisions=coll)


def aloha_multi(p, N):
    """q-ALOHA: every agent transmits with fixed probability p (independently)."""
    import random as _r

    def pol(obs_list):
        return [1 if _r.random() < p else 0 for _ in range(N)]
    return pol


def actor_multi(actors):
    """Greedy-sampled learned policy from the trained per-agent actors."""
    def pol(obs_list):
        acts = []
        for a, o in enumerate(obs_list):
            with torch.no_grad():
                probs = actors[a](torch.as_tensor(o, dtype=torch.float32))
            acts.append(int(Categorical(probs=probs).sample().item()))
        return acts
    return pol


def _save(hist, out, marl_eval=None, aloha_evals=None):
    base = os.path.splitext(out)[0]
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    np.savetxt(base + "_collision.txt", np.array(hist["coll"]),
               header="rolling collision rate", comments="# ")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        ncol = 3 if (marl_eval and aloha_evals) else 2
        fig, ax = plt.subplots(1, ncol, figsize=(5.2 * ncol, 4.4))
        ax[0].plot(hist["coll"], color="C3")
        ax[0].set_title("Collision rate (should DROP)"); ax[0].set_xlabel("training step")
        ax[0].set_ylabel("rolling collision rate"); ax[0].grid(alpha=.3); ax[0].set_ylim(-.02, 1.02)
        for a in range(hist["N"]):
            ax[1].plot(hist["ptx"][a], label="agent %d" % a)
        ax[1].set_title("P(transmit | queued) per agent"); ax[1].set_xlabel("training step")
        ax[1].set_ylabel("P(transmit)"); ax[1].set_ylim(-.02, 1.02)
        ax[1].legend(fontsize=8); ax[1].grid(alpha=.3)
        if ncol == 3:
            labels = ["MARL"] + ["ALOHA p=%.2f" % p for p, _ in aloha_evals]
            thru = [marl_eval["throughput"]] + [e["throughput"] for _, e in aloha_evals]
            colls = [marl_eval["collision_rate"]] + [e["collision_rate"] for _, e in aloha_evals]
            x = np.arange(len(labels))
            ax[2].bar(x - 0.2, thru, width=0.4, label="throughput/slot", color="C2")
            ax[2].bar(x + 0.2, colls, width=0.4, label="collision rate", color="C3")
            ax[2].set_xticks(x); ax[2].set_xticklabels(labels, rotation=20, fontsize=8)
            ax[2].set_title("MARL vs q-ALOHA (eval)"); ax[2].legend(fontsize=8); ax[2].grid(alpha=.3, axis="y")
        fig.suptitle("Multi-agent MARL — %d agents contending for one AP" % hist["N"])
        fig.tight_layout(); fig.savefig(base + ".png", dpi=110); plt.close(fig)
        print("[multi-train] plot -> %s.png" % os.path.abspath(base))
    except Exception as e:
        print("[multi-train] (plot skipped: %s)" % e)


def main(argv):
    a = argparse.ArgumentParser(description="Multi-agent MARL over the shared channel")
    a.add_argument("--mock", action="store_true", help="offline MockMultiChannel (no radios)")
    a.add_argument("--agents", type=int, default=2, help="mock: number of agents")
    a.add_argument("--tx-args", action="append", default=[],
                   help="real: one agent radio per flag (repeat). e.g. --tx-args serial=A "
                        "--tx-args serial=B")
    a.add_argument("--tx-gain", type=float, default=89)
    a.add_argument("--rx-gain", type=float, default=20)
    a.add_argument("--scheme", default="QPSK", help="modulation; MUST match the AP")
    a.add_argument("--physical", action="store_true",
                   help="real: fire colliding bursts simultaneously (else logical collision)")
    a.add_argument("--pkt-int", type=int, default=3, help="mean epochs/packet (lower=heavier)")
    a.add_argument("--steps", type=int, default=500)
    a.add_argument("--lr", type=float, default=4e-4)
    a.add_argument("--objective", type=int, default=0)
    a.add_argument("--deliver-p", type=float, default=0.85, help="mock per-link delivery prob")
    a.add_argument("--coll-penalty", type=float, default=1.0,
                   help="reward penalty for transmitting into a collision (drives "
                        "coordination; 0 = naive reward that just spams)")
    a.add_argument("--compare-aloha", default="0.5,1.0",
                   help="comma p-list to eval q-ALOHA against MARL (empty to skip)")
    a.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "marl_multi", "results", "marl_multi.pt"))
    args = a.parse_args(argv)

    cf = LearningSettings()
    cf.dim_W, cf.dim_D, cf.net_type = 64, 2, 0
    cf.GAMMA, cf.actor_clip, cf.critic_clip = 0.9, 10, 10

    ch = None
    if args.mock:
        N = args.agents
        env = MultiAgentRAEnv(MockMultiChannel(num_D=N, deliver_p=args.deliver_p),
                              num_D=N, objective=args.objective, num_S=args.steps,
                              pkt_int=args.pkt_int, coll_penalty=args.coll_penalty)
    else:
        if not args.tx_args:
            a.error("real mode needs >=2 --tx-args (one per agent radio); "
                    "or use --mock to validate offline")
        env, ch = make_real_multi_env(args.tx_args, objective=args.objective,
                                      num_S=args.steps, pkt_int=args.pkt_int,
                                      tx_gain=args.tx_gain, scheme=args.scheme,
                                      physical=args.physical, coll_penalty=args.coll_penalty)
        N = env.num_D

    try:
        actors, hist = train(env, cf, args.steps, args.lr)
        # save each agent's actor
        base = os.path.splitext(args.out)[0]
        for a_i in range(N):
            torch.save(actors[a_i].state_dict(), "%s_agent%d.pt" % (base, a_i))
        print("[multi-train] done. delivered=%d collisions=%d over %d steps"
              % (hist["deliv"], hist["collisions"], args.steps))

        # eval MARL vs q-ALOHA in the SAME env model (mock: exact; real: fresh window)
        marl_eval = aloha_evals = None
        ps = [float(x) for x in args.compare_aloha.split(",") if x.strip()] \
            if args.compare_aloha else []
        if ps:
            marl_eval = eval_policy(env, actor_multi(actors), args.steps)
            aloha_evals = [(p, eval_policy(env, aloha_multi(p, N), args.steps)) for p in ps]
            print("[eval] MARL          throughput=%.3f/slot  collision-rate=%.2f  mean-AoI=%.2f"
                  % (marl_eval["throughput"], marl_eval["collision_rate"], marl_eval["mean_aoi"]))
            for p, e in aloha_evals:
                print("[eval] q-ALOHA p=%.2f throughput=%.3f/slot  collision-rate=%.2f  mean-AoI=%.2f"
                      % (p, e["throughput"], e["collision_rate"], e["mean_aoi"]))
        _save(hist, args.out, marl_eval, aloha_evals)
    finally:
        if ch is not None:
            ch.close()


if __name__ == "__main__":
    main(sys.argv[1:])
