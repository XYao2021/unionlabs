#!/usr/bin/env python3
"""
agent_node.py — ONE decentralized random-access agent NODE (the realistic setup).

A standalone process for a single agent: its own local state (queue, AoI), its own
policy (A2C), its own online learning. It observes ONLY locally —
    [ own AoI/60,  own queue/Q_max,  sensed channel-busy ]   (dim_O = 3, fixed)
— and reaches the shared medium + AP through a MediumClient:

    MockMediumClient : TCP to mock_medium.py (validate offline, no radio)
    RealMediumClient : WarmSource TX + carrier sensing + multi-agent AP (hardware)

Fully DECENTRALIZED: no shared step, no peer state. Coordination emerges from two
LOCAL signals only — the carrier-sense `busy` bit (learned CSMA: don't transmit when
the medium is busy) and a penalty on the agent's own WASTED transmits (it transmitted
but got no ACK). A node cannot see a collision directly; it only knows delivered vs
no-ACK, so we penalise "transmitted-and-not-delivered", which is fully observable.

Run N of these (one per node / USRP) plus the medium/AP. On one host they use
localhost; across hosts, point --ap-host at the AP. Offline demo (3 terminals):

    python3 mock_medium.py --agents 2 --slots 600
    python3 agent_node.py --mock --id 0 --slots 600
    python3 agent_node.py --mock --id 1 --slots 600

Hardware (tomorrow, 2 agents + 1 AP on one host) — see ap_multi.py for the AP:
    python3 ap_multi.py --agents 2 --scheme QPSK --rx-args serial=30CD3F7
    python3 agent_node.py --id 0 --tx-args serial=30CD424 --scheme QPSK
    python3 agent_node.py --id 1 --tx-args serial=<3RD>   --scheme QPSK
"""
import argparse
import json
import os
import socket
import sys

import numpy as np
import torch
from torch.distributions.categorical import Categorical

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "applications", "MARL_RA_Union"))
from MARL_learning_Union import Actor_A2C, Critic          # noqa: E402
from MARL_setting_Union import LearningSettings             # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
#  Medium clients — same .step(transmit)->{delivered,busy} interface
# ─────────────────────────────────────────────────────────────────────────────
class MockMediumClient:
    """TCP client to mock_medium.py. Barrier-synced slots for offline validation."""

    def __init__(self, agent_id, host="127.0.0.1", port=5600):
        self.id = agent_id
        self._s = socket.create_connection((host, port))
        self._f = self._s.makefile("rwb")

    def step(self, transmit):
        self._f.write((json.dumps({"id": self.id, "transmit": int(transmit)}) + "\n").encode())
        self._f.flush()
        line = self._f.readline()
        if not line:
            return {"delivered": False, "busy": False, "closed": True}
        return json.loads(line)

    def close(self):
        try:
            self._s.close()
        except Exception:
            pass


class RealMediumClient:
    """Hardware medium: a warm source fires one ID-tagged burst per transmit, carrier
    sensing supplies the `busy` bit, and the multi-agent AP (ap_multi.py) routes the
    ACK back to this agent by its ID. Half-duplex on one B210: sense, then transmit.

    NOTE: requires ap_multi.py (concurrent agents + agent-ID ACK routing). Wired here
    against the existing WarmSource / SenseStream so it drops in once the AP is up."""

    def __init__(self, agent_id, tx_args, sense_rx_args=None, ap_host="127.0.0.1",
                 ap_port=5599, tx_gain=89, scheme="QPSK", threshold_db=None, **opts):
        from marl_phy import WarmSource, SenseStream          # noqa
        self.id = agent_id
        # ID-tagged payload: byte 0 = agent id, so the AP can ACK the right node
        from marl_phy import known_payload, PACKET_BYTES
        pkt = bytearray(known_payload(PACKET_BYTES))
        pkt[0] = agent_id & 0xFF
        self._tx = WarmSource(payload=bytes(pkt), tx_args=tx_args, tx_gain=tx_gain,
                              ack_host=ap_host, ack_port=ap_port, scheme=scheme, **opts)
        self._sense = SenseStream(rx_args=sense_rx_args) if sense_rx_args else None
        self._thr = threshold_db

    def step(self, transmit):
        busy = False
        if self._sense is not None:
            go, r = self._sense.should_transmit(p=1.0, threshold_db=self._thr or -30.0)
            busy = bool(r.get("busy", False))
        delivered = bool(self._tx.fire()) if transmit else False
        return {"delivered": delivered, "busy": busy}

    def close(self):
        try:
            self._tx.close()
        finally:
            if self._sense is not None:
                self._sense.close()


# ─────────────────────────────────────────────────────────────────────────────
#  The decentralized agent: local obs, own A2C, online learning
# ─────────────────────────────────────────────────────────────────────────────
def run_agent(client, agent_id, slots, objective=0, pkt_int=3, Q_max=50, lr=4e-4,
              coll_penalty=0.5, out=None, seed=None, log_every=50):
    cf = LearningSettings()
    cf.dim_W, cf.dim_D, cf.net_type = 64, 2, 0
    cf.GAMMA, cf.actor_clip, cf.critic_clip = 0.9, 10, 10
    dim_O, dim_A = 3, 2                        # LOCAL obs: [AoI, queue, busy]
    torch.manual_seed(agent_id if seed is None else seed)
    actor = Actor_A2C(dim_O, cf.dim_W, cf.dim_D, dim_A, cf.net_type, 0, cf)
    critic = Critic(dim_O, cf.dim_W, cf.dim_D, cf.net_type, cf)
    opt_a = torch.optim.Adam(actor.parameters(), lr=lr, eps=1e-5)
    opt_c = torch.optim.Adam(critic.parameters(), lr=lr, eps=1e-5)

    rng = np.random.RandomState(agent_id if seed is None else seed)
    queue, aoi, busy = 1, 0, 0.0
    delivered = wasted = tx = 0
    rewards, ptx_hist = [], []
    # local Poisson arrivals
    arr = np.zeros(slots + 2, dtype=int)
    i = rng.poisson(pkt_int)
    while i < slots:
        arr[i] = 1
        i += max(1, rng.poisson(pkt_int))

    def obs_vec():
        return torch.tensor([aoi / 60.0, queue / Q_max, busy], dtype=torch.float32)

    print("[agent %d] start  slots=%d objective=%d pkt_int=%d coll_penalty=%.2f"
          % (agent_id, slots, objective, pkt_int, coll_penalty))
    for t in range(slots):
        if arr[t] and queue < Q_max:
            queue += 1
        o = obs_vec()
        probs = actor(o)
        action = int(Categorical(probs=probs).sample().item())
        do_tx = action == 1 and queue > 0

        res = client.step(do_tx)
        if res.get("closed"):
            print("[agent %d] medium closed" % agent_id); break
        got = bool(res.get("delivered"))
        busy = 1.0 if res.get("busy") else 0.0

        if do_tx:
            tx += 1
            if got:
                delivered += 1
                queue -= 1
                aoi = 0
            else:
                wasted += 1                    # transmitted but no ACK (locally observable)
        # local reward: objective term minus a penalty for a WASTED transmit. The node
        # can't see a collision, only its own no-ACK, so this is what it can learn from.
        aoi += 1
        pen = coll_penalty if (do_tx and not got) else 0.0
        if objective == 0:
            reward = -1.0 * aoi / 15.0 - pen
        else:
            reward = (1.0 if got else 0.0) - pen
        rewards.append(reward)

        next_o = obs_vec()
        v = critic(o)
        with torch.no_grad():
            v_next = critic(next_o)
        done = (t == slots - 1)
        td = reward + cf.GAMMA * (1.0 - float(done)) * v_next - v
        opt_c.zero_grad(); td.pow(2).backward()
        torch.nn.utils.clip_grad_norm_(critic.parameters(), cf.critic_clip); opt_c.step()
        logp = torch.log(probs[action].clamp_min(1e-6))
        opt_a.zero_grad(); (-logp * td.detach()).backward()
        torch.nn.utils.clip_grad_norm_(actor.parameters(), cf.actor_clip); opt_a.step()

        with torch.no_grad():
            ptx_hist.append(actor(torch.tensor([0.1, 0.1, 0.0]))[1].item())
            ptx_busy = actor(torch.tensor([0.1, 0.1, 1.0]))[1].item()
        if (t + 1) % log_every == 0:
            print("[agent %d %4d/%d] deliv=%d wasted=%d tx=%d  P(tx|idle)=%.2f P(tx|busy)=%.2f"
                  % (agent_id, t + 1, slots, delivered, wasted, tx, ptx_hist[-1], ptx_busy))

    with torch.no_grad():
        p_idle = actor(torch.tensor([0.1, 0.1, 0.0]))[1].item()
        p_busy = actor(torch.tensor([0.1, 0.1, 1.0]))[1].item()
    print("[agent %d] done. delivered=%d wasted=%d tx=%d  learned P(tx|idle)=%.2f "
          "P(tx|busy)=%.2f  (CSMA: idle>busy is coordination)"
          % (agent_id, delivered, wasted, tx, p_idle, p_busy))
    if out:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        torch.save(actor.state_dict(), out)
        np.savetxt(os.path.splitext(out)[0] + "_rewards.txt", np.array(rewards),
                   header="agent %d per-slot reward" % agent_id, comments="# ")
        print("[agent %d] saved -> %s" % (agent_id, os.path.abspath(out)))
    return dict(delivered=delivered, wasted=wasted, tx=tx,
                p_idle=p_idle, p_busy=p_busy)


def main(argv):
    a = argparse.ArgumentParser(description="Decentralized random-access agent node")
    a.add_argument("--id", type=int, required=True, help="this agent's id (0..N-1)")
    a.add_argument("--slots", type=int, default=600)
    a.add_argument("--objective", type=int, default=0)
    a.add_argument("--pkt-int", type=int, default=3)
    a.add_argument("--lr", type=float, default=4e-4)
    a.add_argument("--coll-penalty", type=float, default=0.5)
    a.add_argument("--mock", action="store_true", help="connect to mock_medium.py (no radio)")
    a.add_argument("--medium-host", default="127.0.0.1")
    a.add_argument("--medium-port", type=int, default=5600)
    # real-mode radio args
    a.add_argument("--tx-args", default="serial=30CD424")
    a.add_argument("--sense-rx-args", default=None, help="radio for carrier sensing (busy)")
    a.add_argument("--ap-host", default="127.0.0.1")
    a.add_argument("--ap-port", type=int, default=5599)
    a.add_argument("--tx-gain", type=float, default=89)
    a.add_argument("--scheme", default="QPSK")
    a.add_argument("--out", default=None, help="save this agent's actor here")
    args = a.parse_args(argv)

    if args.mock:
        client = MockMediumClient(args.id, host=args.medium_host, port=args.medium_port)
    else:
        client = RealMediumClient(args.id, tx_args=args.tx_args,
                                  sense_rx_args=args.sense_rx_args, ap_host=args.ap_host,
                                  ap_port=args.ap_port, tx_gain=args.tx_gain, scheme=args.scheme)
    out = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                   "applications", "MARL_RA_Union", "results",
                                   "agent%d.pt" % args.id)
    try:
        run_agent(client, args.id, args.slots, objective=args.objective,
                  pkt_int=args.pkt_int, lr=args.lr, coll_penalty=args.coll_penalty, out=out)
    finally:
        client.close()


if __name__ == "__main__":
    main(sys.argv[1:])
