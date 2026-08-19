#!/usr/bin/env python3
"""discover-node.py — publish what THIS node has into the shared workspace.

Writes /workspace/experiments/settings/<node_id>.json describing this node: its radios,
addresses and PHYs. One file per live node, generated — not hand-authored. Because
/workspace is shared by every session reserved under the account, nodes on different
testbeds, which cannot reach each other over the network, still assemble one inventory
in the folder they share.

WHY A HEARTBEAT INSTEAD OF CLEANUP-ON-EXIT
A session dies by SIGKILL (pod eviction, `docker kill`, node reboot) far more often
than it dies politely, and the platform's contract leaves no shutdown hook to run. So
a file that must be deleted to stay correct is a file that will sometimes be wrong,
advertising a node that no longer exists. Instead every record carries a heartbeat this
process refreshes; a reader treats a record older than --stale as dead, and any live
session reaps records older than --gc. Deletion on SIGTERM is still done when we get
the chance, but nothing depends on it.

Re-probing on each beat also fixes a race for free: the radio NIC is attached to a pod
a few seconds AFTER it starts, so the first probe legitimately finds nothing, and the
next one picks it up.

    discover-node.py --once            # probe, write, exit
    discover-node.py --daemon          # probe every --interval, refresh, GC (session-long)
"""
import argparse, json, os, re, socket, subprocess, sys, tempfile, time

DEFAULT_ROOT = "/workspace/experiments"


def _run(cmd, timeout=15):
    """Best-effort command output. Never raises: a probe must not break a session."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout + p.stderr
    except Exception:
        return ""


def probe_radios():
    """USRPs visible now. uhd_find_devices prints 'Device Address:' blocks; there is no
    machine-readable mode and no uhd python module in this image, so parse the text."""
    out, radios, cur = _run(["uhd_find_devices"], timeout=30), [], None
    for line in out.splitlines():
        if line.startswith("-- UHD Device"):
            cur = {}
            radios.append(cur)
            continue
        m = re.match(r"\s+(serial|addr|type|name|product|resource):\s*(.*)$", line)
        if m and cur is not None and m.group(2).strip():
            cur[m.group(1)] = m.group(2).strip()
    return [r for r in radios if r]


def probe_interfaces():
    out, ifaces = _run(["ip", "-4", "-o", "addr"]), []
    for line in out.splitlines():
        m = re.search(r"^\d+:\s+(\S+)\s+inet\s+(\S+)", line)
        if m and m.group(1) != "lo":
            ifaces.append({"name": m.group(1), "cidr": m.group(2)})
    return ifaces


def probe_lora():
    try:
        return sorted(f"/dev/{d}" for d in os.listdir("/dev")
                      if d.startswith(("ttyUSB", "ttyACM")))
    except OSError:
        return []


def node_key(radios):
    """A radio serial is stable and physical. A hostname is neither — inside a session
    it is the pod/container id and changes every session — so it is only the fallback,
    and such records are expected to churn and be reaped."""
    for r in radios:
        if r.get("serial"):
            return r["serial"]
    return "host-" + socket.gethostname()


def snapshot(node_id=None, role=None):
    radios = probe_radios()
    key = node_id or node_key(radios)
    ifaces = probe_interfaces()
    return radios, {
        "schema": 1,
        "node_id": key,
        "serial": (radios[0].get("serial") if radios else None),
        "ip": (radios[0].get("addr") if radios else None),
        "role": role,
        "key": key,
        "host": socket.gethostname(),
        "heartbeat": time.time(),
        "heartbeat_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pid": os.getpid(),
        "radios": radios,
        "interfaces": probe_interfaces(),
        "lora_ports": probe_lora(),
        "uhd": (_run(["uhd_config_info", "--version"]).strip().splitlines() or [""])[0],
    }


def write_atomic(path, data):
    """Temp file + rename: a reader on the shared filesystem never sees half a record."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise


def gc(dirpath, gc_age, keep):
    """Reap records nobody has refreshed. Any live session may do this — no node has
    privileged standing, and the node that owns a dead record is by definition gone."""
    removed = []
    try:
        names = os.listdir(dirpath)
    except OSError:
        return removed
    now = time.time()
    for name in names:
        if not name.endswith(".json") or name == keep:
            continue
        p = os.path.join(dirpath, name)
        try:
            with open(p) as f:
                hb = json.load(f).get("heartbeat", 0)
        except Exception:
            hb = os.path.getmtime(p)          # unreadable/partial: fall back to mtime
        if now - hb > gc_age:
            try:
                os.unlink(p)
                removed.append(name)
            except OSError:
                pass
    return removed


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=os.environ.get("WS_ROOT", DEFAULT_ROOT))
    ap.add_argument("--once", action="store_true", help="probe, write, exit")
    ap.add_argument("--daemon", action="store_true", help="refresh until killed")
    ap.add_argument("--interval", type=int, default=30, help="seconds between beats")
    ap.add_argument("--gc", type=int, default=900,
                    help="reap records unrefreshed for this many seconds (30 missed beats)")
    # Neither of these can be probed — a radio cannot tell you what an experiment calls
    # it or what job it is doing — so they are inputs, empty until something sets them.
    ap.add_argument("--node-id", default=os.environ.get("WS_NODE_ID"),
                    help="logical name for this node (default: the radio serial)")
    ap.add_argument("--role", default=os.environ.get("WS_ROLE"),
                    help="role in the experiment, if already known")
    a = ap.parse_args()

    outdir = os.path.join(a.root, "settings")
    if not os.path.isdir(a.root):
        print(f"no {a.root} — is the workspace mounted?", file=sys.stderr)
        return 1

    mypath = [None]

    def beat():
        radios, snap = snapshot(a.node_id, a.role)
        name = snap["key"] + ".json"
        # A late-attached radio changes our key; drop the record written under the old one.
        if mypath[0] and os.path.basename(mypath[0]) != name:
            try: os.unlink(mypath[0])
            except OSError: pass
        mypath[0] = os.path.join(outdir, name)
        write_atomic(mypath[0], snap)
        gc(outdir, a.gc, keep=name)
        return snap

    snap = beat()
    print(f"discovered {snap['key']}: {len(snap['radios'])} radio(s), "
          f"{len(snap['interfaces'])} interface(s) -> {mypath[0]}")
    if not a.daemon:
        return 0

    # Best effort only — SIGKILL gets no say, which is exactly why the heartbeat exists.
    import signal
    def bye(*_):
        try: os.unlink(mypath[0])
        except OSError: pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)

    while True:
        time.sleep(a.interval)
        try:
            beat()
        except Exception:
            pass          # a transient probe failure must never end the heartbeat
    return 0


if __name__ == "__main__":
    sys.exit(main())
