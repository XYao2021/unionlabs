#!/usr/bin/env python3
"""
node_ports — which of this session's ports are reachable from another machine,
and under what NAME.

deploy/testbed/expose-session-ports.sh runs on the node, publishes each session's
NodePort block, and records the mapping in
/workspace/experiments/settings/ports-<pod>.json. This reads it back, so a run can
TELL you what the other machine must dial instead of leaving you to work it out
from `kubectl get svc` on a host you cannot log into.

WHAT YOU GET IS A SITE NAME, NOT AN ADDRESS. The record says "siteA", and the same
publisher puts "siteA" in this pod's /etc/hosts, so the name resolves through libc
wherever a host is accepted -- `--ack-host siteA` needs no special handling from
anything downstream. The lab's addressing therefore never reaches an experimenter's
screen, and never enters the shared workspace, which both testbeds mount and which
keeps whatever it is given. Names are cheap to leak; addresses are not.

Deliberately NOT part of the PHY profile. That file is keyed by radio serial and
survives every pod; this one is keyed by pod name and dies with it. Folding the
two together would mean either re-running a four-minute RF measurement every
session, or letting port numbers rot inside a file that still looks
authoritative -- and a stale mapping is worse than none, because it is the one
you would trust.

    python3 union/node_ports.py              # what is reachable, and where
    python3 union/node_ports.py --emit json
    python3 union/node_ports.py --port 5599  # just this one, as site:port
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
    return None, ("no ports published — the node publishes these within ~15s of a "
                  "session starting, so either this is not a session container or "
                  "the node timer is not armed (deploy/testbed/install-expose-ports.sh; "
                  "`journalctl -t expose-session-ports` on the node says which)")


def load(pod=None):
    """-> (mapping, path, why). mapping: container port -> (site, node_port)."""
    path, why = find(pod)
    if not path:
        return {}, None, why
    try:
        with open(path) as fh:
            rec = json.load(fh)
    except Exception as e:
        return {}, None, f"{path}: {e}"
    site = rec.get("site") or "<site>"
    return ({int(k): (site, int(v)) for k, v in (rec.get("map") or {}).items()},
            path, why)


def sessions():
    """Every session that has published ports, newest record last.

    The two testbeds are separate clusters, so neither can reach the other's pod
    IPs and neither can query the other's API. What they DO share is /workspace,
    which the platform mounts for every session under the account across
    testbeds -- so a session publishes the NAME and the port block the outside
    world must dial, and the other side reads it here. That shared folder is the
    only thing the two sites have in common, which is exactly why the record
    lives in it -- and exactly why it holds no address: it is readable by every
    session on both testbeds, and it outlives all of them.
    """
    out = []
    for d in dirs():
        for path in sorted(glob.glob(os.path.join(d, "ports-*.json"))):
            try:
                with open(path) as fh:
                    rec = json.load(fh)
            except Exception:
                continue
            rec["_path"] = path
            rec.setdefault("pod", os.path.basename(path)[6:-5])
            out.append(rec)
    return out


def resolve(name, port):
    """'site:port' to dial to reach `port` on the session called `name`.

    Matches a pod name exactly, else any session whose name contains it, so
    'gnuradio-0' finds '4-gnuradio-0' without anyone memorising the prefix.
    Returns (address, note) or (None, why-not).
    """
    recs = sessions()
    if not recs:
        return None, "no session has published ports into the shared workspace"
    hit = [r for r in recs if r.get("pod") == name] or \
          [r for r in recs if name in str(r.get("pod", ""))]
    if not hit:
        return None, (f"no published session matches {name!r} — have: "
                      + ", ".join(sorted(str(r.get("pod")) for r in recs)))
    if len(hit) > 1:
        return None, (f"{name!r} matches several: "
                      + ", ".join(sorted(str(r.get("pod")) for r in hit)))
    rec = hit[0]
    if not rec.get("site"):
        return None, (f"session {rec.get('pod')} has a record from before site "
                      f"aliases; re-run the node publisher to replace it")
    np_ = (rec.get("map") or {}).get(str(int(port)))
    if np_ is None:
        return None, (f"session {rec.get('pod')} did not publish container port {port} "
                      f"(it published: " + ", ".join(sorted((rec.get("map") or {}))) + ")")
    return f"{rec['site']}:{np_}", f"{rec.get('pod')} via the shared workspace"


def external(port, pod=None):
    """'site:port' another machine should dial to reach `port` here, or None."""
    m, _, _ = load(pod)
    hit = m.get(int(port))
    return f"{hit[0]}:{hit[1]}" if hit else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--pod", default=None)
    ap.add_argument("--port", type=int, default=None,
                    help="print just this container port's external site:port")
    ap.add_argument("--emit", choices=["human", "json"], default="human")
    ap.add_argument("--list", action="store_true",
                    help="every session that has published ports, across testbeds")
    ap.add_argument("--peer", metavar="NAME",
                    help="address to dial to reach ANOTHER session (with --port)")
    a = ap.parse_args()

    if a.list:
        recs = sessions()
        if not recs:
            print("[ports] no session has published ports into the shared workspace")
            return 1
        for r in recs:
            ports = ", ".join(f"{k}->{v}" for k, v in sorted(
                (r.get("map") or {}).items(), key=lambda kv: int(kv[0])))
            print(f"  {r.get('pod')}   {r.get('site') or '<site>'}   {ports}")
            print(f"      updated {r.get('updated_utc', '?')}   {r.get('_path')}")
        return 0

    if a.peer:
        if a.port is None:
            print("--peer needs --port (which container port do you want?)", file=sys.stderr)
            return 2
        addr, note = resolve(a.peer, a.port)
        if not addr:
            print(f"[ports] {note}", file=sys.stderr)
            return 1
        print(addr)
        return 0

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
        site, np_ = m[p]
        print(f"          {site}:{np_}   ->  container {p}   {_role(p)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
