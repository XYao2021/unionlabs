#!/usr/bin/env python3
"""
node_ports — which of this session's ports are reachable from another machine,
and on what address.

expose-my-ports.sh publishes a block of NodePorts at session start and records
the mapping in /workspace/experiments/settings/ports-<pod>.json. This reads it
back, so a run can TELL you what the other machine must dial instead of leaving
you to work it out from `kubectl get svc`.

Deliberately NOT part of the PHY profile. That file is keyed by radio serial and
survives every pod; this one is keyed by pod name and dies with it. Folding the
two together would mean either re-running a four-minute RF measurement every
session, or letting port numbers rot inside a file that still looks
authoritative -- and a stale mapping is worse than none, because it is the one
you would trust.

    python3 union/node_ports.py              # what is reachable, and where
    python3 union/node_ports.py --emit json
    python3 union/node_ports.py --port 5599  # just this one, as host:port
"""
import argparse
import glob
import json
import os
import socket
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEARCH = ("$UNION_SETTINGS_DIR", "/workspace/experiments/settings",
          os.path.join(REPO, "deploy", "workspace", "settings"))

# What each port is for, so the listing reads as something other than numbers.
ROLES = {5599: "ACK socket (--ack-port)",
         5700: "algorithm network (--net-port)",
         5701: "relay's network (--net-port + 1)"}


def _role(p):
    if p in ROLES:
        return ROLES[p]
    return f"peer {p - 5800} (--peer-port + {p - 5800})" if 5800 <= p <= 5899 else ""


def dirs():
    out = []
    for d in SEARCH:
        d = os.path.expandvars(os.path.expanduser(d))
        if d and "$" not in d and d not in out:
            out.append(d)
    return out


def find(pod=None):
    """-> (path, why) or (None, why-not)."""
    pod = pod or socket.gethostname()
    searched = dirs()
    for d in searched:
        p = os.path.join(d, f"ports-{pod}.json")
        if os.path.exists(p):
            return p, f"this session ({pod})"
    found = [p for d in searched for p in sorted(glob.glob(os.path.join(d, "ports-*.json")))]
    if len(found) == 1:
        return found[0], "the only record present (not this pod's)"
    if found:
        return None, (f"{len(found)} sessions have published ports, none named {pod} — "
                      f"name one with --pod")
    return None, ("no ports published — expose-my-ports.sh runs at session start, "
                  "so either this is not a session container or the cluster is "
                  "missing the one-time RBAC grant (see /tmp/expose-my-ports.log)")


def load(pod=None):
    """-> (mapping, path, why). mapping: container port -> (node_ip, node_port)."""
    path, why = find(pod)
    if not path:
        return {}, None, why
    try:
        with open(path) as fh:
            rec = json.load(fh)
    except Exception as e:
        return {}, None, f"{path}: {e}"
    ip = rec.get("node_ip") or "<node-ip>"
    return ({int(k): (ip, int(v)) for k, v in (rec.get("map") or {}).items()},
            path, why)


def external(port, pod=None):
    """'host:port' another machine should dial to reach `port` here, or None."""
    m, _, _ = load(pod)
    hit = m.get(int(port))
    return f"{hit[0]}:{hit[1]}" if hit else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--pod", default=None)
    ap.add_argument("--port", type=int, default=None,
                    help="print just this container port's external address")
    ap.add_argument("--emit", choices=["human", "json"], default="human")
    a = ap.parse_args()

    m, path, why = load(a.pod)
    if a.port is not None:
        hit = m.get(a.port)
        if not hit:
            print(f"port {a.port} is not published ({why})", file=sys.stderr)
            return 1
        print(f"{hit[0]}:{hit[1]}")
        return 0
    if a.emit == "json":
        print(json.dumps({"path": path, "why": why,
                          "map": {str(k): f"{v[0]}:{v[1]}" for k, v in m.items()}},
                         indent=2))
        return 0
    if not path:
        print(f"[ports] none: {why}")
        return 1
    print(f"[ports] {path}  ({why})")
    print("        another machine reaches this session at:")
    for p in sorted(m):
        ip, np_ = m[p]
        print(f"          {ip}:{np_}   ->  container {p}   {_role(p)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
