#!/usr/bin/env python3
"""
marl_multi_phy_experiment.py — FAITHFUL multi-agent random-access experiment that
reproduces the *physics and the reward* of the original paper (marl_ra), rather
than the toy ideal-collision model. N agents (TX) contend for one access point (RX).

Channel  (paper's `wireless_channel`, marl_network.py + marl_setting.py):
    beta_i = r_loss * (r_dist / d_i)**path_exp          # distance pathloss, exp 4
    h ~ CN(0,1)^num_Rx                                   # Rayleigh fading, 2 Rx antennas
    P_rx = t_pow * beta_i * |h|^2                        # received power
    SINR = P_rx_winner / (BW*N0 + sum P_rx_interferers)  # capture / near-far effect
    decode iff SINR >= gamma_th                          # noise floor N0 = -174 dBm/Hz
  -> distance MATTERS (closer = stronger); a strong node can CAPTURE a collision slot.

Reward  (paper objective 2 = "fair rate", marl_ra.py:282-285):
    U(rate_i) = (rate_i*scale + 0.7)**(1-alpha)/(1-alpha),  alpha = 12   (alpha-fair)
  -> strongly concave: a starved agent values delivery far more than a rich one, so the
     agents equalise their rates (max-min fairness) instead of one hogging the channel.

Observation (paper's dim_O = N+2):  [AoI_0..AoI_{N-1}, own_queue, channel_busy]
  -> every agent sees ALL agents' Age-of-Information (the coordination signal), so a
     symmetric policy is *representable* (the toy's own-AoI-only obs could not).

Reuses the REAL actor_MLP/critic_MLP from experiments/marl_ra. Needs torch.

Run:  python3 phy/tools/marl_multi_phy_experiment.py --dists 18,18,18,18 --tag equidistant
      python3 phy/tools/marl_multi_phy_experiment.py --dists 6,12,20,32  --tag unequal
"""
import argparse, csv, math, os, sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))   # tools->usrp->drivers->repo
sys.path.insert(0, os.path.join(REPO, "experiments", "_shared"))
import torch                                                     # noqa: E402
from torch.distributions.categorical import Categorical          # noqa: E402
from marl_learning import actor_MLP, critic_MLP            # the REAL nets  # noqa: E402

# ── paper PHY constants (marl_setting.py) ──
T_POW = 0.001 * 10 ** (0 / 10)          # tx power, 0 dBm (linear W)
N_POW = 0.001 * 10 ** (-174 / 10)       # noise PSD, -174 dBm/Hz (linear W/Hz)
BW = 10e6                               # 10 MHz
R_LOSS = 10 ** (-40 / 10)               # reference loss, -40 dB @ 1 m
R_DIST = 1.0                            # reference distance
PATH_EXP = 4                            # pathloss exponent
NUM_RX = 2                              # Rx antennas
GAMMA_TH_DB = 8.0                       # decode threshold (~QPSK), dB

# ── learning / reward constants ──
WIDTH, DEPTH, DIM_A = 64, 2, 2
GAMMA, LR, CLIP = 0.9, 4e-4, 10.0
ALPHA, RSCALE = 12.0, 3.0               # alpha-fair utility (objective 2)
COLL_PEN = 3.0                          # back-off gradient (stands in for CSMA carrier-sensing)
OBJECTIVE = 2                           # paper reward: 0=fair-AoI, 1=max-rate, 2=alpha-fair
OBJ_NAME = {0: "fair-AoI", 1: "max-rate", 2: "alpha-fair"}
SHARED = False                          # True = parameter sharing (one shared policy for all agents)
Q_MAX, PKT_INT = 50, 4


def snr_mean_db(d):
    """Mean (fading-averaged) SNR at distance d, for reporting."""
    beta = R_LOSS * (R_DIST / d) ** PATH_EXP
    snr = T_POW * beta * NUM_RX / (BW * N_POW)     # E[|h|^2] = num_Rx
    return 10 * math.log10(snr)


class PhyChannel:
    """Pathloss + Rayleigh fading + thermal noise, with SINR capture (near-far)."""

    def __init__(self, dists, seed=0):
        self.n = len(dists)
        self.beta = [R_LOSS * (R_DIST / d) ** PATH_EXP for d in dists]
        self.noise = BW * N_POW
        self.gth = 10 ** (GAMMA_TH_DB / 10)
        self.rng = np.random.RandomState(seed)

    def step(self, tx_flags):
        idx = [i for i, f in enumerate(tx_flags) if f]
        delivered = [False] * self.n
        if not idx:
            return delivered, None
        prx = {}
        for i in idx:
            h = (self.rng.normal(size=NUM_RX) + 1j * self.rng.normal(size=NUM_RX)) / math.sqrt(2)
            prx[i] = T_POW * self.beta[i] * float(np.sum(np.abs(h) ** 2))   # |h|^2 ~ Exp(1)
        winner = max(idx, key=lambda i: prx[i])                            # strongest captures
        interference = sum(prx[j] for j in idx if j != winner)
        sinr = prx[winner] / (self.noise + interference)
        if sinr >= self.gth:                                               # else outage/collision
            delivered[winner] = True
        return delivered, dict(sinr_db=10 * math.log10(sinr), winner=winner)


class Agent:
    def __init__(self, dim_o, seed):
        torch.manual_seed(seed)
        self.actor = actor_MLP(dim_o, WIDTH, DEPTH, DIM_A, 0)
        self.critic = critic_MLP(dim_o, WIDTH, DEPTH)
        self.opt_a = torch.optim.Adam(self.actor.parameters(), lr=LR, eps=1e-5)
        self.opt_c = torch.optim.Adam(self.critic.parameters(), lr=LR, eps=1e-5)


def alpha_fair(rate):
    """paper objective 2: U(rate) = (rate*scale + 0.7)^(1-alpha)/(1-alpha)."""
    return (rate * RSCALE + 0.7) ** (1 - ALPHA) / (1 - ALPHA)


def run(dists, steps, seed, record_every):
    n = len(dists)
    dim_o = n + 2
    ch = PhyChannel(dists, seed=seed)
    rng = np.random.RandomState(seed + 1)
    if SHARED:                                        # parameter sharing (CTDE): ONE policy for all
        sh = Agent(dim_o, seed=100)                   # -> identical by construction -> symmetric 1/N
        agents = [sh for _ in range(n)]
    else:
        agents = [Agent(dim_o, seed=100 + i) for i in range(n)]  # independent learners

    queue = [1] * n
    since = [0] * n                                   # AoI per agent
    delivered = [0] * n
    tot_deliv = tot_coll = tot_idle = 0
    # Poisson arrivals per agent
    arr = np.zeros((n, steps + 1), dtype=int)
    for a in range(n):
        i = rng.poisson(PKT_INT)
        while i < steps:
            arr[a, i] = 1
            i += max(1, rng.poisson(PKT_INT))
    last_busy = 0.0

    def obs():
        aoi = [s / 60.0 for s in since]
        return [torch.tensor(aoi + [queue[a] / Q_MAX, last_busy], dtype=torch.float32)
                for a in range(n)]

    probe = torch.tensor([0.1] * n + [0.3, 0.0], dtype=torch.float32)
    traj = []
    o = obs()
    for t in range(steps):
        for a in range(n):
            if arr[a, t] and queue[a] < Q_MAX:
                queue[a] += 1
        probs, vals, acts = [], [], []
        for a in range(n):
            p = agents[a].actor(o[a]); probs.append(p)
            vals.append(agents[a].critic(o[a]))
            acts.append(int(Categorical(probs=p).sample().item()))
        tx = [bool(acts[a] == 1 and queue[a] > 0) for a in range(n)]
        n_tx = sum(tx)
        deliv, _ = ch.step(tx)
        for a in range(n):
            if deliv[a]:
                queue[a] -= 1; since[a] = 0; delivered[a] += 1
        got = any(deliv)
        if n_tx >= 2 and not got:
            tot_coll += 1
        elif n_tx == 0:
            tot_idle += 1
        for a in range(n):
            if not deliv[a]:
                since[a] = min(since[a] + 1, 600)
        tot_deliv += sum(int(d) for d in deliv)
        last_busy = 1.0 if n_tx > 0 else 0.0
        o2 = obs()

        collided = [tx[a] and not deliv[a] for a in range(n)]   # wasted transmit (collision/outage)
        for a in range(n):                            # per-agent A2C update (paper reward objective)
            if OBJECTIVE == 1:                        # max-rate: +1 per delivery (throughput-optimal)
                base = 1.0 if deliv[a] else 0.0
            elif OBJECTIVE == 0:                      # fair-AoI: minimise age since last success
                base = -since[a] / (15.0 * n)
            else:                                     # alpha-fair rate (objective 2)
                base = alpha_fair(delivered[a] / (t + 1))
            reward = base - COLL_PEN * float(collided[a])
            p = agents[a].actor(o[a])                 # recompute vs CURRENT params
            v = agents[a].critic(o[a])                # (robust to shared-weight in-place updates)
            with torch.no_grad():
                v_next = agents[a].critic(o2[a])
            td = reward + GAMMA * v_next - v
            agents[a].opt_c.zero_grad(); td.pow(2).backward()
            torch.nn.utils.clip_grad_norm_(agents[a].critic.parameters(), CLIP)
            agents[a].opt_c.step()
            logp = torch.log(p[acts[a]].clamp_min(1e-6))
            agents[a].opt_a.zero_grad(); (-logp * td.detach()).backward()
            torch.nn.utils.clip_grad_norm_(agents[a].actor.parameters(), CLIP)
            agents[a].opt_a.step()
        o = o2

        if record_every and (t + 1) % record_every == 0:
            with torch.no_grad():
                ptx = [agents[a].actor(probe)[1].item() for a in range(n)]
            traj.append(dict(slot=t + 1, delivered=tot_deliv, collisions=tot_coll,
                             idle=tot_idle, p_tx=ptx))
    return dict(n=n, steps=steps, delivered=delivered, tot_deliv=tot_deliv,
                tot_coll=tot_coll, tot_idle=tot_idle, traj=traj)


def plot(res, dists, tag, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n, traj = res["n"], res["traj"]
    slot = [r["slot"] for r in traj]
    mids, w_thru, w_coll = [], [], []
    prev = dict(slot=0, delivered=0, collisions=0)
    for r in traj:
        span = r["slot"] - prev["slot"]
        mids.append((r["slot"] + prev["slot"]) / 2.0)
        w_thru.append((r["delivered"] - prev["delivered"]) / span)
        w_coll.append((r["collisions"] - prev["collisions"]) / span)
        prev = r
    ptx = [[r["p_tx"][i] for r in traj] for i in range(n)]
    snr = [snr_mean_db(d) for d in dists]

    plt.rcParams.update({"font.size": 10, "axes.grid": True, "grid.alpha": 0.3,
                         "axes.spines.top": False, "axes.spines.right": False})
    fig, ax = plt.subplots(1, 4, figsize=(18, 4.2))

    # panel 1 — the physical setup: distance & SNR per agent
    ax[0].bar(range(n), snr, color="#6a51a3")
    ax[0].axhline(GAMMA_TH_DB, color="#c0392b", ls="--", lw=1.4, label=f"decode th = {GAMMA_TH_DB:.0f} dB")
    ax[0].set_xlabel("agent"); ax[0].set_ylabel("mean SNR (dB)")
    ax[0].set_title("Channel setup (pathloss+noise)")
    ax[0].set_xticks(range(n)); ax[0].set_xticklabels([f"{i}\n{dists[i]:.0f}m" for i in range(n)])
    ax[0].legend(fontsize=8)

    # panel 2 — learning curve
    opt = (1 - 1.0 / n) ** (n - 1)
    ax[1].plot(mids, w_thru, color="#1b7f4b", lw=2, label="throughput/slot")
    ax[1].plot(mids, w_coll, color="#c0392b", lw=2, label="collision rate")
    ax[1].axhline(opt, color="#888", ls="--", lw=1.4, label=f"ALOHA opt = {opt:.2f}")
    ax[1].set_xlabel("slot"); ax[1].set_ylabel("rate/slot"); ax[1].set_ylim(0, 1)
    ax[1].set_title("Learning curve"); ax[1].legend(fontsize=8)

    # panel 3 — per-agent P(transmit)
    for i in range(n):
        ax[2].plot(slot, ptx[i], lw=1.8, label=f"agent {i} ({dists[i]:.0f}m)")
    ax[2].axhline(1.0 / n, color="#888", ls="--", lw=1.4, label=f"1/N = {1.0/n:.2f}")
    ax[2].set_xlabel("slot"); ax[2].set_ylabel("P(transmit | queued)"); ax[2].set_ylim(0, 1)
    ax[2].set_title("Per-agent policy"); ax[2].legend(fontsize=8)

    # panel 4 — fairness
    d = res["delivered"]
    bars = ax[3].bar(range(n), d, color="#2c6fbb")
    ax[3].set_xlabel("agent"); ax[3].set_ylabel("packets delivered")
    jain = (sum(d) ** 2) / (n * sum(x * x for x in d)) if sum(d) else 0.0
    ax[3].set_title(f"Fairness (Jain = {jain:.2f})")
    ax[3].set_xticks(range(n))
    for b, v in zip(bars, d):
        ax[3].text(b.get_x() + b.get_width() / 2, b.get_height(), str(v),
                   ha="center", va="bottom", fontsize=8)

    fig.suptitle(f"Faithful multi-agent MARL ({tag}) — {n} TX → 1 RX, "
                 f"pathloss+noise+fading channel, {OBJ_NAME[OBJECTIVE]} reward", fontsize=12, y=1.03)
    fig.tight_layout()
    os.makedirs(out, exist_ok=True)
    png = os.path.join(out, f"B_phy_{tag}.png")
    fig.savefig(png, dpi=140, bbox_inches="tight")
    print(f"[B] wrote {png}  (throughput={res['tot_deliv']/res['steps']:.3f}/slot, "
          f"collision={res['tot_coll']/res['steps']:.3f}, delivered={d}, Jain={jain:.3f})")

    csv_path = os.path.join(out, f"B_phy_{tag}_traj.csv")
    with open(csv_path, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["slot", "delivered", "collisions", "idle"] + [f"p_tx{i}" for i in range(n)])
        for r in traj:
            wr.writerow([r["slot"], r["delivered"], r["collisions"], r["idle"]] + r["p_tx"])


def main():
    global COLL_PEN, OBJECTIVE, SHARED
    ap = argparse.ArgumentParser()
    ap.add_argument("--dists", default="18,18,18,18", help="per-agent distance to AP (m)")
    ap.add_argument("--tag", default="equidistant")
    ap.add_argument("--steps", type=int, default=4000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--record-every", type=int, default=100)
    ap.add_argument("--coll-pen", type=float, default=COLL_PEN,
                    help="back-off penalty (higher=more conservative)")
    ap.add_argument("--objective", type=int, default=OBJECTIVE, choices=[0, 1, 2],
                    help="paper reward: 0=fair-AoI, 1=max-rate, 2=alpha-fair")
    ap.add_argument("--shared", action="store_true",
                    help="parameter sharing: one policy for all agents -> symmetric 1/N")
    ap.add_argument("--out", default=os.path.join(REPO, "results", "marl_multi"))
    a = ap.parse_args()
    COLL_PEN = a.coll_pen
    OBJECTIVE = a.objective
    SHARED = a.shared
    dists = [float(x) for x in a.dists.split(",")]
    print(f"[B] {len(dists)} TX -> 1 RX  dists={dists}  "
          f"mean SNR(dB)={[round(snr_mean_db(d), 1) for d in dists]}  "
          f"(decode th={GAMMA_TH_DB} dB)")
    res = run(dists, a.steps, a.seed, a.record_every)
    plot(res, dists, a.tag, a.out)


if __name__ == "__main__":
    main()
