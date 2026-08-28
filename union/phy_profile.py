#!/usr/bin/env python3
"""
phy_profile — find and read the PHY profile prepare_phy measured for THIS node,
so a measured testbed configures itself instead of being retyped on every run.

prepare_phy publishes /workspace/experiments/settings/phy-<key>.json. This finds
the right one and hands it to radio.sh (as shell assignments) and to run.sh
(as a dict), where it sits BELOW anything the experimenter typed and below the
topology file:

    explicit flag  >  topology file  >  phy profile  >  built-in default

so a profile can never silently override an intent someone expressed. Every
consumer prints what it loaded and from where; defaults that arrive from a file
without saying so are worse than no defaults at all.

Identity: keyed by RADIO SERIAL, not hostname. Inside a session the hostname is
the pod id and changes every session, which would strand the profile the moment
the pod restarted; the radio is the thing that actually stays with the testbed.
$UNION_SITE overrides, for a site whose radios get swapped.

    python3 union/phy_profile.py                     # show the resolved profile
    python3 union/phy_profile.py --emit shell        # KEY=VALUE for radio.sh
    python3 union/phy_profile.py --emit json
    python3 union/phy_profile.py --node siteB
"""
import argparse
import glob
import json
import os
import socket
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# searching/ first: that is where a measurement belongs and where prepare_phy
# writes. settings/ stays in the list because profiles written before the split
# are still perfectly good, and a measurement that costs minutes on a radio
# should not be thrown away over a folder rename.
SEARCH = ("$UNION_SETTINGS_DIR",
          "/workspace/experiments/searching", "/workspace/experiments/settings",
          os.path.join(REPO, "deploy", "workspace", "searching"),
          os.path.join(REPO, "deploy", "workspace", "settings"))

# Only these reach the modem. Anything else in the file is provenance for a
# human (when it was measured, on what, how wide the quiet region was) and is
# deliberately NOT turned into a flag.
FLAGS = {
    "carrier_mhz":    "freq",
    "gain_db":        "gain",
    "det_mult":       "det_mult",
    "sync_threshold": "sync_threshold",
}


def dirs():
    out = []
    for d in SEARCH:
        d = os.path.expandvars(os.path.expanduser(d))
        if d and "$" not in d and d not in out:
            out.append(d)
    return out


def radio_serials():
    """Serials of the radios attached here. Same idea as discover-node.py's
    node_key: a serial is stable and physical, a hostname is neither."""
    try:
        p = subprocess.run(["uhd_find_devices"], capture_output=True, text=True,
                           timeout=15)
        out = p.stdout + p.stderr
    except Exception:
        return []
    return [ln.split(":", 1)[1].strip()
            for ln in out.splitlines() if ln.strip().startswith("serial:")]


def node_key():
    """The name a profile for THIS node is filed under."""
    site = os.environ.get("UNION_SITE", "").strip()
    if site:
        return site
    for s in radio_serials():
        if s:
            return s
    return "host-" + socket.gethostname()


def candidates(node=None):
    """Every key worth trying, most specific first."""
    keys = []
    if node:
        keys.append(node)
    else:
        site = os.environ.get("UNION_SITE", "").strip()
        if site:
            keys.append(site)
        keys.extend(s for s in radio_serials() if s)
        keys.append("host-" + socket.gethostname())
    return keys


def find(node=None):
    """-> (path, why) or (None, why-not). $UNION_PHY_PROFILE wins outright."""
    env = os.environ.get("UNION_PHY_PROFILE", "").strip()
    if env:
        return (env, "UNION_PHY_PROFILE") if os.path.exists(env) else \
               (None, f"UNION_PHY_PROFILE={env} does not exist")
    searched = dirs()
    for key in candidates(node):
        for d in searched:
            path = os.path.join(d, f"phy-{key}.json")
            if os.path.exists(path):
                return path, f"matched {key}"
    # Exactly one profile and nothing matched: use it, but say so — on a
    # single-radio bench that is what the user means, and staying silent about
    # a guess is how a wrong default becomes invisible.
    found = [p for d in searched for p in sorted(glob.glob(os.path.join(d, "phy-*.json")))]
    if len(found) == 1:
        return found[0], "the only profile present (no key matched this node)"
    if found:
        return None, (f"{len(found)} profiles present, none for this node "
                      f"({', '.join(candidates(node))}) — name one with --node "
                      f"or UNION_PHY_PROFILE")
    return None, "no profile measured yet (run prepare_phy --write)"


def pick_candidate(prof, pick=None):
    """Which of the saved recommendations to use.

    prepare_phy stores every quiet region it found, ranked, with the widest as
    `recommended`. That is the default because bandwidth is the scarce resource,
    but which region is genuinely best depends on things the sweep cannot see --
    a neighbouring experiment, a regulatory limit, an antenna only nominally in
    band -- so any of them can be named instead: by 1-based rank, or by a
    carrier in MHz (nearest wins).
    """
    cands = prof.get("candidates") or []
    if not cands:                                  # schema 1: the file IS the choice
        return prof, "schema-1 profile"
    if pick in (None, "", "best"):
        return prof.get("recommended") or cands[0], "recommended"
    txt = str(pick).strip()
    # A rank and a carrier are both digits, so size decides: 1..N is a rank,
    # anything larger can only be MHz (no testbed has 5680 candidates).
    if txt.isdigit() and 1 <= int(txt) <= len(cands):
        return cands[int(txt) - 1], f"candidate {txt}"
    try:
        want = float(txt)
    except ValueError:
        return None, f"--pick {txt!r}: give a rank (1..{len(cands)}) or a carrier in MHz"
    best = min(cands, key=lambda c: abs(float(c.get("carrier_mhz", 1e12)) - want))
    return best, f"carrier nearest {want:g} MHz"


def load(node=None, pick=None):
    """-> (values, path, why). values maps modem option -> value."""
    path, why = find(node)
    if not path:
        return {}, None, why
    try:
        with open(path) as fh:
            prof = json.load(fh)
    except Exception as e:
        return {}, None, f"{path}: {e}"
    chosen, how = pick_candidate(prof, pick)
    if chosen is None:
        return {}, None, how
    vals = {}
    for src, dst in FLAGS.items():
        # a candidate carries its own carrier/thresholds; anything it does not
        # carry (gain is a property of the whole measurement) comes from the top
        v = chosen.get(src, prof.get(src))
        if v is not None:
            vals[dst] = v
    n = len(prof.get("candidates") or [])
    if n > 1:
        why = f"{why}, {how} of {n} saved"
    return vals, path, why


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--node", default=None, help="profile key (default: this node's)")
    ap.add_argument("--emit", choices=["human", "shell", "json"], default="human")
    ap.add_argument("--pick", default=None, metavar="RANK|MHz",
                    help="use a saved candidate other than the recommended one: "
                         "a 1-based rank, or a carrier in MHz (nearest wins)")
    ap.add_argument("--list", action="store_true",
                    help="show every candidate saved for this node")
    a = ap.parse_args()

    if a.list:
        path, why = find(a.node)
        if not path:
            print(f"[phy-profile] none: {why}")
            return 1
        with open(path) as fh:
            prof = json.load(fh)
        cands = prof.get("candidates") or [prof]
        print(f"[phy-profile] {path}  ({why})")
        for i, c in enumerate(cands):
            band = c.get("usable_mhz") or ["?", "?"]
            print(f"  {i + 1}. carrier {c.get('carrier_mhz', '?')} MHz  "
                  f"band {band[0]}-{band[1]} ({c.get('width_mhz', '?')} MHz)  "
                  f"floor {c.get('region_floor_db', c.get('floor_db', '?'))} dB"
                  f"{'   [measured here]' if c.get('measured') else ''}"
                  f"{'   <- recommended' if i == 0 else ''}")
        return 0

    vals, path, why = load(a.node, a.pick)
    if a.emit == "shell":
        # Consumed by `eval` in radio.sh: plain assignments only, no commands,
        # every value shell-quoted. Paths routinely contain spaces, and an
        # unquoted assignment then runs the tail of the path as a command.
        import shlex
        if path:
            print(f"PHY_PROFILE_PATH={shlex.quote(path)}")
            print(f"PHY_PROFILE_WHY={shlex.quote(why)}")
            for k, v in vals.items():
                print(f"PHY_{k.upper()}={shlex.quote(str(v))}")
        else:
            print(f"PHY_PROFILE_WHY={shlex.quote(why)}")
        return 0
    if a.emit == "json":
        print(json.dumps({"path": path, "why": why, "values": vals}, indent=2))
        return 0
    if not path:
        print(f"[phy-profile] none: {why}")
        print(f"[phy-profile] this node's key would be: {node_key()}")
        return 1
    print(f"[phy-profile] {path}  ({why})")
    for k, v in vals.items():
        print(f"    {k:16s} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
