#!/usr/bin/env python3
"""
slot_sync.py — a tiny TCP slot clock for the slotted MARL random-access system.

Slotted random access needs every agent to act in the SAME time slot, so that two
agents that both "transmit in slot k" actually contend on the air. This is a global
clock over TCP (the same transport as the ACK path), decoupled from ap_multi:

    SlotClock  (server, run on the AP host or any coordinator): every `slot_ms` it
               broadcasts 'SLOT <k>' to all connected agents. It first waits for N
               agents to connect (a START BARRIER), so slot 0 begins only once
               everyone is present and aligned.
    SlotClient (agent side): wait_slot() blocks until the next 'SLOT <k>' tick and
               returns k. The agent does exactly one decision (defer / transmit) per
               tick, so all agents step in lockstep.

The AP (ap_multi.py) needs no changes: it decodes bursts and ACKs by id as before;
alignment is enforced entirely by the clock + the agents.

Radio-free self-test (no devices, no ap_multi):
    python3 slot_sync.py --self-test

Run for real (on the AP host, say):
    python3 slot_sync.py --agents 2 --slot-ms 150
"""
import socket
import sys
import threading
import time


class SlotClock:
    """Server: broadcast 'SLOT <k>' to all agents every slot_ms after a start barrier."""

    def __init__(self, num_agents, port=5600, slot_ms=150):
        self.N = num_agents
        self.port = port
        self.slot_ms = slot_ms
        self._conns = []
        self._lock = threading.Lock()
        self._srv = None
        self._stop = False
        self.slot = -1

    def start(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("0.0.0.0", self.port))
        self._srv.listen(max(1, self.N))
        threading.Thread(target=self._accept_loop, daemon=True).start()
        print("[slot-sync] clock on :%d  slot_ms=%d — waiting for %d agents"
              % (self.port, self.slot_ms, self.N), flush=True)

    def _accept_loop(self):
        while not self._stop:
            try:
                c, addr = self._srv.accept()
            except OSError:
                break
            with self._lock:
                self._conns.append(c)
            print("[slot-sync] agent connected (%d/%d)" % (len(self._conns), self.N), flush=True)

    def wait_all(self, timeout=120):
        t = 0.0
        while len(self._conns) < self.N and t < timeout and not self._stop:
            time.sleep(0.1); t += 0.1
        return len(self._conns) >= self.N

    def run(self):
        """Blocking tick loop: broadcast 'SLOT k' every slot_ms, drift-corrected.
        Drops an agent whose socket breaks (keeps the others ticking)."""
        k = 0
        next_t = time.monotonic()
        while not self._stop:
            next_t += self.slot_ms / 1000.0
            dt = next_t - time.monotonic()
            if dt > 0:
                time.sleep(dt)
            msg = ("SLOT %d\n" % k).encode()
            with self._lock:
                dead = []
                for c in self._conns:
                    try:
                        c.sendall(msg)
                    except OSError:
                        dead.append(c)
                for c in dead:
                    self._conns.remove(c)
                    print("[slot-sync] an agent dropped (%d left)" % len(self._conns), flush=True)
            self.slot = k
            k += 1

    def start_ticking(self):
        """Run the tick loop in a background thread (for embedding / tests)."""
        threading.Thread(target=self.run, daemon=True).start()

    def close(self):
        self._stop = True
        with self._lock:
            for c in self._conns:
                try:
                    c.close()
                except Exception:
                    pass
        if self._srv:
            try:
                self._srv.close()
            except Exception:
                pass


class SlotClient:
    """Agent side: connect to the SlotClock and block on each tick."""

    def __init__(self, host, port=5600, connect_timeout=60):
        self._s = socket.create_connection((host, port), timeout=connect_timeout)
        self._s.settimeout(None)
        self._buf = b""

    def wait_slot(self, timeout=None):
        """Block until the next 'SLOT <k>' tick; return k (int), or None on timeout /
        closed clock. Non-SLOT lines are ignored."""
        self._s.settimeout(timeout)
        try:
            while True:
                while b"\n" not in self._buf:
                    data = self._s.recv(4096)
                    if not data:
                        return None                 # clock closed
                    self._buf += data
                line, self._buf = self._buf.split(b"\n", 1)
                txt = line.decode("utf-8", "replace").strip()
                if txt.startswith("SLOT"):
                    try:
                        return int(txt.split()[1])
                    except (IndexError, ValueError):
                        return -1
                # else: ignore and read the next line
        except socket.timeout:
            return None
        finally:
            try:
                self._s.settimeout(None)
            except OSError:
                pass

    def close(self):
        try:
            self._s.close()
        except Exception:
            pass


def _self_test():
    """Radio-free: a SlotClock + N SlotClients; verify every client sees the SAME
    slot sequence [0..4] (aligned), driven entirely over TCP."""
    N = 3
    clock = SlotClock(N, port=5711, slot_ms=40)
    clock.start()
    got = {i: [] for i in range(N)}

    def run_client(i):
        c = SlotClient("127.0.0.1", 5711)
        for _ in range(5):
            got[i].append(c.wait_slot(timeout=5))
        c.close()

    threads = [threading.Thread(target=run_client, args=(i,), daemon=True) for i in range(N)]
    for t in threads:
        t.start()
    if not clock.wait_all(timeout=5):
        print("[slot-sync] clients did not all connect -> FAIL"); clock.close(); return False
    clock.start_ticking()
    for t in threads:
        t.join(timeout=5)
    ok = all(got[i] == [0, 1, 2, 3, 4] for i in range(N))
    print("[slot-sync] per-agent slot sequence: %s  ->  %s"
          % (got, "PASS" if ok else "FAIL"))
    clock.close()
    return ok


def main(argv):
    import argparse
    a = argparse.ArgumentParser(description="TCP slot clock for the slotted MARL system")
    a.add_argument("--self-test", action="store_true", help="radio-free clock test")
    a.add_argument("--agents", type=int, default=2)
    a.add_argument("--port", type=int, default=5600)
    a.add_argument("--slot-ms", type=int, default=150)
    args = a.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    clock = SlotClock(args.agents, port=args.port, slot_ms=args.slot_ms)
    clock.start()
    if not clock.wait_all():
        print("[slot-sync] not all agents connected — aborting"); return
    print("[slot-sync] all %d agents connected — ticking every %d ms (Ctrl-C to stop)"
          % (args.agents, args.slot_ms), flush=True)
    try:
        clock.run()
    except KeyboardInterrupt:
        pass
    finally:
        clock.close()


if __name__ == "__main__":
    main(sys.argv[1:])
