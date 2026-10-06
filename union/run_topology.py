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
import re
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


def local_radio_ids():
    """Every radio identifier THIS container can actually see — serials and addresses.

    Host matching cannot answer "which nodes are mine" when /workspace is shared
    across containers on different machines, because a pod's hostname is a new string
    every session: one topology file would need editing per container, which is the
    opposite of sharing it. A RADIO can answer it. The serial is physically attached
    to one machine, it outlives the container, and a node already names the radio it
    owns -- so the file stays identical everywhere and each container works out its
    own part of it. Same rule calibration_plan and link_setup use to pick a role.

    Returns an empty set when no radio is visible (no UHD, none attached), which
    simply means radio matching contributes nothing and host matching decides.
    """
    out = set()
    try:
        txt = subprocess.run(["uhd_find_devices"], capture_output=True,
                             text=True, timeout=30).stdout or ""
    except Exception:
        return out
    for m in re.finditer(r"^\s*(?:serial|addr):\s*(\S+)", txt, re.M):
        out.add(m.group(1).strip())
    return out


def node_radio_id(nd):
    """-> (identifier, kind) for the radio a node claims, or (None, None) for a node
    with no radio (a pure-TCP node, which only host matching can place).

    SERIAL IS PREFERRED, and the preference is not cosmetic. An address identifies a
    radio only WITHIN one host: 192.168.40.2 is UHD's default for an X310, so two
    machines each with an X310 on its own isolated subnet both have one at exactly
    that address. Across containers sharing a topology, matching on it makes both
    claim the same node -- the duplicate-node failure this whole mechanism exists to
    prevent, re-entering by another door. A serial is burned into the hardware and is
    unique across the testbed, so it is the only identifier that means the same thing
    in every container. UHD accepts serial= for networked radios too, so a node can
    always be named that way.
    """
    args = (getattr(nd, "radio", None) or {}).get("args") or ""
    m = re.search(r"serial=([^,\s]+)", args)
    if m:
        return m.group(1).strip(), "serial"
    m = re.search(r"addr=([^,\s]+)", args)
    if m:
        return m.group(1).strip(), "addr"
    return None, None


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
    radios = local_radio_ids()
    want = [n.strip() for n in a.only.split(",") if n.strip()]
    nodes, skipped, why, by_addr = [], [], {}, []
    for nd in topo.nodes:
        if want and nd.id not in want:
            continue
        rid, kind = node_radio_id(nd)
        # The RADIO decides first. A shared /workspace means every container reads the
        # same file, and the only thing that distinguishes them is the hardware each
        # one can actually see -- not the hostname, which is a fresh pod id per session.
        if rid and rid in radios:
            why[nd.id] = f"its radio {rid} is attached here"
            if kind == "addr":
                by_addr.append((nd.id, rid))
            nodes.append(nd)
        elif a.all:
            why[nd.id] = "--all"
            nodes.append(nd)
        elif rid and radios:
            # it names a radio, we can see radios, and it is not one of ours: this node
            # belongs to another container, and saying so beats a bare "not this machine"
            skipped.append((nd, f"radio {rid} is not attached here"))
        elif nd.is_local() or nd.host in mine:
            why[nd.id] = f"host {nd.host or '127.0.0.1'} is this machine"
            nodes.append(nd)
        else:
            skipped.append((nd, f"host {nd.host} is another machine"))
    if not nodes:
        seen = ", ".join(sorted(radios)) if radios else "none"
        sys.exit(f"\nno node of {topo.name} belongs to this container."
                 f"\n  radios attached here: {seen}"
                 f"\n  nodes want: " + ", ".join(
                     f"{nd.id}->{node_radio_id(nd)[0] or ('host ' + str(nd.host))}"
                     for nd in topo.nodes) +
                 f"\nRun each node where its radio is, or pass --all to start them here.")
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
        print(f"{YEL}skipping{OFF} (another container owns these): "
              + ", ".join(f"{nd.id} ({r})" for nd, r in skipped))
    if by_addr:
        # Claiming a node by address is right on one machine and ambiguous across
        # several, and nothing at run time can tell which situation this is -- so say
        # it once rather than either staying silent or refusing a setup that is fine.
        print(f"{YEL}note{OFF} claimed by ADDRESS, not serial: "
              + ", ".join(f"{n} ({a})" for n, a in by_addr)
              + "\n     An address is unique only within one host — 192.168.40.2 is "
                "UHD's default\n     for an X310, so another container's radio may "
                "answer to it too, and both\n     would claim this node. Name these "
                "nodes by serial= in the topology if the\n     file is shared across "
                "machines.")

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
