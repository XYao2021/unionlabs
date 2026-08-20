#!/usr/bin/env python3
"""
run_topology.py — start every node of a topology that lives on THIS machine.

    ./run.sh topology fl-star-tcp                 # all local nodes, in the right order
    ./run.sh topology fl-star-tcp --steps 30      # any run.sh flag is passed to each node
    ./run.sh topology dl-ring3-tcp --only n0,n1   # just these
    ./run.sh topology fl-star-tcp --dry-run       # print the commands, run nothing

WHY. A topology is normally typed once per node, on the machine that node runs on — one
terminal per pod. But several nodes often share a machine (three peers on one laptop,
two clients beside a server in one session), and then the interesting part is the
ORDER: the node that listens has to be up before the node that dials it, or the dialler
spends its connect timeout on a hub that does not exist yet. This starts them in that
order, keeps each node's log apart, and reports every exit code — so a failure names the
node it happened in.

A node whose `host` is not this machine is SKIPPED and listed, because starting it here
would silently produce a second copy of a node that is supposed to be somewhere else.
"""
import argparse
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import topology as tp                                       # noqa: E402

GREEN, RED, YEL, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YEL = DIM = OFF = ""


def local_addresses():
    """Every address this machine answers to, so a topology written with real IPs still
    starts its own nodes here instead of skipping all of them."""
    import socket
    out = set(tp.LOCAL_HOSTS)
    try:
        host = socket.gethostname()
        out.add(host)
        out |= {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except OSError:
        pass
    return out


def pump(prefix, stream, log, colour):
    with open(log, "w") as fh:
        for line in iter(stream.readline, ""):
            fh.write(line)
            fh.flush()
            sys.stdout.write(f"{colour}{prefix:>8}{OFF} | {line}")
            sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser(description="run every local node of a topology")
    ap.add_argument("name", help="topology file name (or path)")
    ap.add_argument("--only", default="", help="comma-separated node ids to start")
    ap.add_argument("--all", action="store_true",
                    help="start every node, even ones whose host is another machine")
    ap.add_argument("--stagger", type=float, default=1.5,
                    help="seconds between starts (default 1.5) — the listener needs a "
                         "moment before the dialler tries")
    ap.add_argument("--dry-run", action="store_true", help="print the commands only")
    ap.add_argument("--logs", default=None, help="where node logs go "
                    "(default results/topology/<name>)")
    a, extra = ap.parse_known_args()

    try:
        topo = tp.load(a.name)
    except tp.TopologyError as e:
        sys.exit(f"topology: {e}")
    print(topo.summary())

    mine = local_addresses()
    want = [n.strip() for n in a.only.split(",") if n.strip()]
    nodes, skipped = [], []
    for nd in topo.nodes:
        if want and nd.id not in want:
            continue
        if a.all or nd.is_local() or nd.host in mine:
            nodes.append(nd)
        else:
            skipped.append(nd)
    if not nodes:
        sys.exit(f"\nno node of {topo.name} runs on this machine "
                 f"(hosts: {', '.join(sorted({n.host or '127.0.0.1' for n in topo.nodes}))})"
                 f"\nrun each node on its own machine, or pass --all to start them here.")
    # Downstream first. A node is only reachable once the node it dials is listening, so
    # start the sinks (nothing downstream), then the relays, then the pure sources. Every
    # transport retries its connect for a while, so a wrong order costs seconds rather
    # than the run — but a chain started backwards spends them on every hop.
    def stage(nd):
        out = any(ln.a.id == nd.id for ln in topo.links_of(nd))
        inc = any(ln.b.id == nd.id for ln in topo.links_of(nd))
        return (0 if not out else (1 if inc else 2), nd.index)
    nodes.sort(key=stage)

    logs = a.logs or os.path.join(REPO, "results", "topology", topo.name)
    os.makedirs(logs, exist_ok=True)
    algo = topo.algo or "echo"
    if "--algo" in extra:
        algo = extra[extra.index("--algo") + 1]
        extra = [t for i, t in enumerate(extra)
                 if i not in (extra.index("--algo"), extra.index("--algo") + 1)]

    print(f"\nstarting {len(nodes)} node(s) of {topo.name} here: "
          f"{', '.join(nd.id for nd in nodes)}")
    if skipped:
        print(f"{YEL}skipping{OFF} (not this machine): "
              + ", ".join(f"{nd.id}@{nd.host}" for nd in skipped))

    procs, pumps = [], []
    colours = (GREEN, YEL, DIM, RED)
    for i, nd in enumerate(nodes):
        cmd = [os.path.join(REPO, "run.sh"), "--algo", algo,
               "--topology", topo.path, "--node", nd.id] + extra
        print(f"  {nd.id:>8} | {' '.join(cmd[1:])}")
        if a.dry_run:
            continue
        p = subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1,
                             env=dict(os.environ, PYTHONUNBUFFERED="1"))
        t = threading.Thread(target=pump, args=(nd.id, p.stdout,
                                                os.path.join(logs, nd.id + ".log"),
                                                colours[i % len(colours)]), daemon=True)
        t.start()
        procs.append((nd, p))
        pumps.append(t)
        if i + 1 < len(nodes):
            time.sleep(a.stagger)
    if a.dry_run:
        return 0

    fails = []
    try:
        for nd, p in procs:
            p.wait()
            if p.returncode != 0:
                fails.append((nd.id, p.returncode))
    except KeyboardInterrupt:
        print("\ninterrupted — stopping every node")
        for _, p in procs:
            p.terminate()
        for nd, p in procs:
            p.wait()
        return 130
    for t in pumps:
        t.join(timeout=2)
    print(f"\nlogs in {logs}")
    if fails:
        print(RED + "FAILED" + OFF + ": " +
              ", ".join(f"{n} (exit {c})" for n, c in fails))
        return 1
    print(GREEN + "all nodes finished" + OFF +
          f": {', '.join(nd.id for nd, _ in procs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
