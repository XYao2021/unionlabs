#!/usr/bin/env python3
"""
mock_medium.py — offline stand-in for the shared wireless channel + Access Point, so
the DECENTRALIZED multi-agent code (independent `agent_node.py` processes) can be
validated with NO radios.

It is a tiny TCP server that N agent nodes connect to. Each slot it collects one
{id, transmit} message per agent (a barrier), resolves the shared medium exactly like
the real AP would, and replies to each agent:

    0 transmitters   -> idle
    1 transmitter    -> delivered with prob `deliver_p` (the real per-burst rate)
    >=2 transmitters -> COLLISION, nobody delivered

Reply to each agent = {delivered: bool, busy: bool}. `busy` is what that agent would
sense on the RF medium (someone transmitted this slot) — the local carrier-sense
signal, the only cross-agent information a decentralized node gets.

This mirrors the interface the real AP must expose to agent_node.py, so the SAME
agent code runs against the mock today and the radios tomorrow (swap the endpoint).

    python3 mock_medium.py --agents 2 --slots 600 --deliver-p 0.85
"""
import argparse
import json
import socket
import sys
import threading


class MockMedium:
    def __init__(self, num_agents, slots, deliver_p=0.85, port=5600, seed=0):
        self.N = num_agents
        self.slots = slots
        self.deliver_p = deliver_p
        self.port = port
        # deterministic per-slot delivery draw (no Math.random dependence on wall clock)
        self._draw = [((i * 2654435761) % 1000) / 1000.0 for i in range(slots + 2)]
        self._slot = 0
        self._acts = {}                       # agent_id -> transmit(0/1) for this slot
        self._result = {}                     # agent_id -> {delivered, busy}
        self._lock = threading.Lock()
        self._barrier = threading.Barrier(num_agents, action=self._resolve)
        self._last_busy = False
        self.collisions = 0
        self.delivered = {i: 0 for i in range(num_agents)}

    def _resolve(self):
        """Called once per slot when all N agents have reported (barrier action)."""
        txers = [i for i, a in self._acts.items() if a]
        # each agent's `busy` = did ANOTHER agent transmit (what it would carrier-sense
        # when listening; a node can't sense its own transmission). This is the clean
        # cross-agent coordination signal.
        result = {i: {"delivered": False,
                      "busy": any(j != i for j in txers)} for i in range(self.N)}
        if len(txers) == 1:
            i = txers[0]
            if self._draw[self._slot % len(self._draw)] < self.deliver_p:
                result[i]["delivered"] = True
                self.delivered[i] += 1
        elif len(txers) >= 2:
            self.collisions += 1
        self._result = result
        self._last_busy = len(txers) > 0
        self._slot += 1
        self._acts = {}

    def _client(self, conn, addr):
        f = conn.makefile("rwb")
        try:
            while True:
                line = f.readline()
                if not line:
                    break
                msg = json.loads(line)
                aid = int(msg["id"])
                with self._lock:
                    self._acts[aid] = int(msg.get("transmit", 0))
                try:
                    self._barrier.wait(timeout=60)
                except threading.BrokenBarrierError:
                    break
                res = self._result.get(aid, {"delivered": False, "busy": False})
                f.write((json.dumps(res) + "\n").encode())
                f.flush()
        except Exception:
            pass
        finally:
            conn.close()

    def serve(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", self.port))
        s.listen(self.N)
        print("[medium] listening on 127.0.0.1:%d for %d agents (%d slots)"
              % (self.port, self.N, self.slots))
        threads = []
        for _ in range(self.N):
            conn, addr = s.accept()
            t = threading.Thread(target=self._client, args=(conn, addr), daemon=True)
            t.start()
            threads.append(t)
        print("[medium] all %d agents connected — running" % self.N)
        for t in threads:
            t.join()
        tot = sum(self.delivered.values())
        print("[medium] done. delivered=%s (total %d)  collisions=%d"
              % (self.delivered, tot, self.collisions))


def main(argv):
    a = argparse.ArgumentParser(description="Mock shared medium + AP for decentralized agents")
    a.add_argument("--agents", type=int, required=True)
    a.add_argument("--slots", type=int, default=600)
    a.add_argument("--deliver-p", type=float, default=0.85)
    a.add_argument("--port", type=int, default=5600)
    args = a.parse_args(argv)
    MockMedium(args.agents, args.slots, deliver_p=args.deliver_p, port=args.port).serve()


if __name__ == "__main__":
    main(sys.argv[1:])
