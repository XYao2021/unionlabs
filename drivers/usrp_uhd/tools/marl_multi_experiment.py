#!/usr/bin/env python3
"""
marl_multi_experiment.py — run the REAL multi-agent MARL random-access experiment
(N transmitters contending for one access point) through the uniform PHY API and
plot the learning curves.

Topology:  N agents = N TX  ->  1 RX (the access point).  Each slot every agent's
A2C actor picks transmit/defer; 0 tx = idle, exactly 1 = delivered (ACK), >=2 =
collision (no ACK). Agents learn to back off from their own missing ACKs only.
The medium is `phy_link.run_slotted` — its `channel.transfer(...)` is the port to
the PHY (swap `ideal` for the radio to run the same experiment over USRPs).

Outputs (to --out, default results/marl_multi/):
  marl_multi_learning.png   3 panels: throughput+collisions, per-agent P(tx), fairness
  marl_multi_traj.csv       the raw checkpoint trajectory

Run:  python3 phy/tools/marl_multi_experiment.py --agents 4 --slots 4000
"""
import argparse, csv, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))   # tools->usrp_uhd->drivers->repo
sys.path.insert(0, os.path.join(REPO, "union"))
import phy_link as pl                                     # noqa: E402
import run_algo                                           # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="multi-agent MARL random-access experiment + plots")
    ap.add_argument("--algo", default="marl_multi", help="algorithm folder under algorithms/")
    ap.add_argument("--agents", type=int, default=4, help="number of transmitters (TX)")
    ap.add_argument("--slots", type=int, default=4000, help="slotted-medium steps")
    ap.add_argument("--record-every", type=int, default=100, help="trajectory checkpoint stride")
    ap.add_argument("--channel", default="ideal", choices=["ideal", "pyphy"])
    ap.add_argument("--scheme", default="QPSK")
    ap.add_argument("--fec", default="turbo")
    ap.add_argument("--snr-db", type=float, default=8.0)
    ap.add_argument("--out", default=os.path.join(REPO, "results", "marl_multi"))
    a = ap.parse_args()

    n = a.agents
    factory, how, _mod = run_algo.load_app_factory(a.algo)
    print(f"[exp] loaded algorithms/{a.algo} via {how}")
    agents = [factory("agent") for _ in range(n)]          # N independent TX agents -> 1 AP
    ch = (pl.make_channel("pyphy", scheme=a.scheme, fec=(a.fec or None), snr_db=a.snr_db)
          if a.channel == "pyphy" else pl.make_channel("ideal"))
    print(f"[exp] {n} TX -> 1 RX, {a.slots} slots, channel={ch.name}")

    st = pl.run_slotted(agents, ch, slots=a.slots, record_every=a.record_every)
    traj = st["traj"]

    opt = (1 - 1.0 / n) ** (n - 1)                          # slotted-ALOHA optimum throughput
    thru = st["delivered"] / max(1, st["slots"])
    coll = st["collisions"] / max(1, st["slots"])
    delivered = [int(getattr(g._src, "delivered", 0)) for g in agents]
    print(f"[exp] final: throughput={thru:.3f}/slot (ALOHA optimum={opt:.3f})  "
          f"collision-rate={coll:.3f}  per-agent delivered={delivered}")

    os.makedirs(a.out, exist_ok=True)

    # ── windowed rates between consecutive checkpoints (shows the trend, not the cumulative) ──
    slot = [r["slot"] for r in traj]
    mids, w_thru, w_coll = [], [], []
    prev = dict(slot=0, delivered=0, collisions=0)
    for r in traj:
        span = r["slot"] - prev["slot"]
        mids.append((r["slot"] + prev["slot"]) / 2.0)
        w_thru.append((r["delivered"] - prev["delivered"]) / span)
        w_coll.append((r["collisions"] - prev["collisions"]) / span)
        prev = r
    ptx = [[r["p_tx"][i] for r in traj] for i in range(n)]  # per-agent P(transmit) over time

    # ── raw trajectory CSV ──
    csv_path = os.path.join(a.out, "marl_multi_traj.csv")
    with open(csv_path, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["slot", "delivered", "collisions", "idle"] + [f"p_tx_agent{i}" for i in range(n)])
        for r in traj:
            wr.writerow([r["slot"], r["delivered"], r["collisions"], r["idle"]] + r["p_tx"])
    print(f"[exp] wrote {csv_path}")

    # ── plots ──
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.size": 10, "axes.grid": True, "grid.alpha": 0.3,
                         "axes.spines.top": False, "axes.spines.right": False})
    fig, ax = plt.subplots(1, 3, figsize=(14, 4.2))
    C_THRU, C_COLL, C_OPT = "#1b7f4b", "#c0392b", "#888888"

    # panel 1 — throughput & collision rate learning curves
    ax[0].plot(mids, w_thru, color=C_THRU, lw=2, label="throughput (delivered/slot)")
    ax[0].plot(mids, w_coll, color=C_COLL, lw=2, label="collision rate")
    ax[0].axhline(opt, color=C_OPT, ls="--", lw=1.4, label=f"ALOHA optimum = {opt:.2f}")
    ax[0].set_xlabel("slot"); ax[0].set_ylabel("rate per slot")
    ax[0].set_title(f"Learning curve ({n} TX → 1 RX)")
    ax[0].set_ylim(0, 1); ax[0].legend(fontsize=8, loc="center right")

    # panel 2 — per-agent P(transmit): the emergent role-differentiation
    for i in range(n):
        ax[1].plot(slot, ptx[i], lw=1.8, label=f"agent {i}")
    ax[1].axhline(1.0 / n, color=C_OPT, ls="--", lw=1.4, label=f"symmetric 1/N = {1.0/n:.2f}")
    ax[1].set_xlabel("slot"); ax[1].set_ylabel("P(transmit | queued)")
    ax[1].set_title("Per-agent policy (who backs off)")
    ax[1].set_ylim(0, 1); ax[1].legend(fontsize=8)

    # panel 3 — fairness: packets each agent actually delivered
    bars = ax[2].bar(range(n), delivered, color="#2c6fbb")
    ax[2].set_xlabel("agent"); ax[2].set_ylabel("packets delivered")
    ax[2].set_title(f"Fairness (total delivered = {sum(delivered)})")
    ax[2].set_xticks(range(n))
    for b, d in zip(bars, delivered):
        ax[2].text(b.get_x() + b.get_width() / 2, b.get_height(), str(d),
                   ha="center", va="bottom", fontsize=8)

    fig.suptitle(f"Multi-agent MARL random access  —  {n} transmitters, 1 access point, "
                 f"channel={ch.name}", fontsize=12, y=1.02)
    fig.tight_layout()
    png = os.path.join(a.out, "marl_multi_learning.png")
    fig.savefig(png, dpi=150, bbox_inches="tight")
    print(f"[exp] wrote {png}")


if __name__ == "__main__":
    main()
