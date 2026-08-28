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


def _band_of(path):
    """The band a profile file is for, from its name: phy-<key>-<band>.json."""
    base = os.path.basename(path)[4:-5] if os.path.basename(path).startswith("phy-") else ""
    for b in ("vert2450-5g", "vert2450", "vert900", "ism915"):
        if base.endswith("-" + b):
            return b
    return None


def _covers(path, mhz):
    """Does this profile's measured spectrum include `mhz`?"""
    try:
        with open(path) as fh:
            prof = json.load(fh)
    except Exception:
        return False
    opts = prof.get("options") or prof.get("candidates") or [prof]
    for o in opts:
        b = o.get("band_mhz") or o.get("usable_mhz")
        if b and len(b) == 2 and float(b[0]) <= float(mhz) <= float(b[1]):
            return True
    return False


def find(node=None, band=None, near_mhz=None):
    """-> (path, why) or (None, why-not). $UNION_PHY_PROFILE wins outright.

    One radio can carry two antennas -- a VERT900 on one port and a VERT2450 on
    another -- and each needs its own survey, so a profile is filed as
    phy-<key>-<band>.json and the serial alone no longer identifies one. When
    several exist for this radio, the band names which, or a frequency picks the
    profile whose measured spectrum covers it.
    """
    env = os.environ.get("UNION_PHY_PROFILE", "").strip()
    if env:
        return (env, "UNION_PHY_PROFILE") if os.path.exists(env) else \
               (None, f"UNION_PHY_PROFILE={env} does not exist")
    band = band or os.environ.get("UNION_BAND", "").strip() or None
    searched = dirs()

    for key in candidates(node):
        hits = []
        for d in searched:
            if band:                                  # exactly this antenna
                p = os.path.join(d, f"phy-{key}-{band}.json")
                if os.path.exists(p):
                    return p, f"matched {key} on {band}"
            p = os.path.join(d, f"phy-{key}.json")     # pre-band layout
            if os.path.exists(p):
                hits.append(p)
            hits += sorted(glob.glob(os.path.join(d, f"phy-{key}-*.json")))
        hits = list(dict.fromkeys(hits))
        if band and hits:
            return None, (f"no profile for {key} on {band} — measured bands: "
                          + ", ".join(sorted(str(_band_of(h) or "unlabelled") for h in hits)))
        if len(hits) == 1:
            return hits[0], f"matched {key}"
        if hits:
            if near_mhz is not None:
                covering = [h for h in hits if _covers(h, near_mhz)]
                if len(covering) == 1:
                    return covering[0], (f"matched {key}, the only band measured "
                                         f"that covers {float(near_mhz):g} MHz")
            return None, (f"{key} has {len(hits)} profiles (bands: "
                          + ", ".join(sorted(str(_band_of(h) or "unlabelled") for h in hits))
                          + ") — name one with --band, $UNION_BAND, or a --freq "
                            "inside the band you want")

    found = [p for d in searched for p in sorted(glob.glob(os.path.join(d, "phy-*.json")))]
    if len(found) == 1:
        return found[0], "the only profile present (no key matched this node)"
    if found:
        return None, (f"{len(found)} profiles present, none for this node "
                      f"({', '.join(candidates(node))}) — name one with --node "
                      f"or UNION_PHY_PROFILE")
    return None, "no profile measured yet (run ./prepare.sh)"


def _options(prof):
    """The usable carriers, whatever schema the file is in.

    schema 3 : {"options": [...], "use": i}   -- one place per value
    schema 2 : {"candidates": [...], "recommended": {...}}
    schema 1 : the file itself is the single choice
    """
    if prof.get("options"):
        return prof["options"], int(prof.get("use", 0) or 0)
    if prof.get("candidates"):
        return prof["candidates"], 0
    return [prof], 0


def pick_candidate(prof, pick=None):
    """Which saved option to use.

    The widest is recommended, because bandwidth is the scarce resource, but
    which one is genuinely best depends on things a sweep cannot see -- a
    neighbouring experiment, a regulatory limit, an antenna only nominally in
    band -- so any of them can be named: by 1-based rank, or by a carrier in MHz
    (nearest wins).
    """
    opts, use = _options(prof)
    if not opts:
        return None, "the profile lists no usable carrier"
    if pick in (None, "", "best"):
        return opts[min(use, len(opts) - 1)], "recommended"
    txt = str(pick).strip()
    # a rank and a carrier are both digits, so size decides: 1..N is a rank,
    # anything larger can only be MHz
    if txt.isdigit() and 1 <= int(txt) <= len(opts):
        return opts[int(txt) - 1], f"option {txt}"
    try:
        want = float(txt)
    except ValueError:
        return None, f"--pick {txt!r}: give a rank (1..{len(opts)}) or a carrier in MHz"
    best = min(opts, key=lambda c: abs(float(c.get("carrier_mhz", 1e12)) - want))
    return best, f"carrier nearest {want:g} MHz"


def load(node=None, pick=None, band=None, near_mhz=None):
    """-> (values, path, why). values maps modem option -> value.

    A value is looked up in the chosen option first, then in the shared parts of
    the profile: the carrier belongs to the option, while the detector thresholds
    and the receive gain were measured once and apply to all of them.
    """
    path, why = find(node, band, near_mhz)
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
    shared = dict(prof)
    shared.update(prof.get("radio") or {})       # gain_db lives here in schema 3
    vals = {}
    for src, dst in FLAGS.items():
        v = chosen.get(src, shared.get(src))
        if v is not None:
            vals[dst] = v
    opts, _ = _options(prof)
    if len(opts) > 1:
        why = f"{why}, {how} of {len(opts)} saved"
    return vals, path, why


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--node", default=None, help="profile key (default: this node's)")
    ap.add_argument("--emit", choices=["human", "shell", "json"], default="human")
    ap.add_argument("--band", default=None,
                    help="which antenna's survey (vert900 / ism915 / vert2450 / "
                         "vert2450-5g). Needed when one radio carries two.")
    ap.add_argument("--near", type=float, default=None, metavar="MHz",
                    help="pick the band whose measured spectrum covers this")
    ap.add_argument("--pick", default=None, metavar="RANK|MHz",
                    help="use a saved candidate other than the recommended one: "
                         "a 1-based rank, or a carrier in MHz (nearest wins)")
    ap.add_argument("--list", action="store_true",
                    help="show every candidate saved for this node")
    a = ap.parse_args()

    if a.list:
        path, why = find(a.node, a.band, a.near)
        if not path:
            print(f"[phy-profile] none: {why}")
            return 1
        with open(path) as fh:
            prof = json.load(fh)
        cands, _use = _options(prof)
        print(f"[phy-profile] {path}  ({why})")
        for i, c in enumerate(cands):
            band = c.get("band_mhz") or c.get("usable_mhz") or ["?", "?"]
            print(f"  {i + 1}. carrier {c.get('carrier_mhz', '?')} MHz  "
                  f"band {band[0]}-{band[1]} ({c.get('width_mhz', '?')} MHz)  "
                  f"floor {c.get('floor_db', c.get('region_floor_db', '?'))} dB"
                  f"{'   [measured here]' if c.get('measured_here') or c.get('measured') else ''}"
                  f"{'   <- recommended' if i == 0 else ''}")
        return 0

    vals, path, why = load(a.node, a.pick, a.band, a.near)
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
