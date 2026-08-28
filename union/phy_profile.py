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


def radio_idents():
    """How each attached radio identifies itself: a serial, or an IP for the
    network-addressed ones. A host can have several, and each carries its own
    survey, so all of them are worth looking a profile up by."""
    try:
        p = subprocess.run(["uhd_find_devices"], capture_output=True, text=True,
                           timeout=15)
        out = p.stdout + p.stderr
    except Exception:
        return []
    idents, cur = [], {}
    for ln in out.splitlines():
        t = ln.strip()
        if t.startswith("--") or t.startswith("Device Address"):
            if cur:
                idents.append(cur.get("addr") or cur.get("serial") or "")
                cur = {}
            continue
        if ":" in t:
            k, v = t.split(":", 1)
            k, v = k.strip(), v.strip()
            if k in ("serial", "addr") and v:
                cur[k] = v
    if cur:
        idents.append(cur.get("addr") or cur.get("serial") or "")
    return [i for i in idents if i]


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
        for ident in radio_idents():
            keys.append(ident)
            keys.append(ident.replace(".", "-"))   # as prepare_phy files an addr
        keys.append("host-" + socket.gethostname())
    return keys


def _meta(path):
    """What physical setup a profile was measured with: band, RF channel, connector.

    Read from the record, not parsed out of the filename. The name only has to be
    unique; the file already states what it measured, so a profile written under
    an older naming scheme still answers correctly.
    """
    try:
        with open(path) as fh:
            prof = json.load(fh)
    except Exception:
        return {}
    r = prof.get("radio") or {}
    return {"band":   r.get("band")   or prof.get("band"),
            "subdev": r.get("subdev") or prof.get("rx_subdev"),
            "ant":    r.get("ant")    or prof.get("rx_ant"),
            "args":   r.get("args")   or prof.get("args"),
            "node":   prof.get("node")}


def _describe(path):
    m = _meta(path)
    bits = [m.get("band") or "unlabelled"]
    for k in ("subdev", "ant"):
        if m.get(k):
            bits.append(str(m[k]))
    return " ".join(bits)


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


def _ident_of(args):
    """The identifying value in a UHD args string: serial=X / addr=Y -> X / Y."""
    if not args:
        return None
    for part in str(args).split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() in ("serial", "addr") and v.strip():
                return v.strip()
    return str(args).strip() or None


def find(node=None, band=None, near_mhz=None, ant=None, subdev=None, args=None):
    """-> (path, why) or (None, why-not). $UNION_PHY_PROFILE wins outright.

    A radio is not one measurable thing. It can carry two antennas, on two RF
    channels and two connectors, and each combination has its own noise floor and
    its own usable spectrum -- so a profile identifies the whole setup, and any
    part of it can narrow the search. When what is given still matches more than
    one, the ambiguity is reported rather than resolved by guessing.
    """
    env = os.environ.get("UNION_PHY_PROFILE", "").strip()
    if env:
        return (env, "UNION_PHY_PROFILE") if os.path.exists(env) else \
               (None, f"UNION_PHY_PROFILE={env} does not exist")
    band = band or os.environ.get("UNION_BAND", "").strip() or None
    searched = dirs()

    # Several radios on one host each carry their own survey, and each is filed
    # under its own identity. When the run names the radio it is using, look that
    # one up FIRST -- otherwise whichever radio happens to enumerate first wins,
    # and a two-radio host silently reads the wrong profile.
    keys = candidates(node)
    ident = _ident_of(args)
    if ident:
        keys = [ident, ident.replace(".", "-")] + [k for k in keys if k not in
                                                   (ident, ident.replace(".", "-"))]

    for key in keys:
        hits = []
        for d in searched:
            hits.append(os.path.join(d, f"phy-{key}.json"))       # pre-band layout
            hits += sorted(glob.glob(os.path.join(d, f"phy-{key}-*.json")))
        hits = [h for h in dict.fromkeys(hits) if os.path.exists(h)]
        if not hits:
            continue

        narrowed, applied = hits, []
        for field, value in (("band", band), ("subdev", subdev), ("ant", ant),
                             ("args", args)):
            if not value:
                continue
            if field == "args":
                want = _ident_of(value)
                sel = [h for h in narrowed
                       if want and _ident_of(_meta(h).get("args")) == want]
            else:
                sel = [h for h in narrowed if str(_meta(h).get(field)) == str(value)]
            if not sel:
                return None, (f"no profile for {key} with {field}={value} — measured: "
                              + "; ".join(sorted(_describe(h) for h in narrowed)))
            narrowed = sel
            applied.append(f"{field}={value}")

        if len(narrowed) == 1:
            return narrowed[0], (f"matched {key}"
                                 + (f" ({', '.join(applied)})" if applied else ""))
        if near_mhz is not None:
            covering = [h for h in narrowed if _covers(h, near_mhz)]
            if len(covering) == 1:
                return covering[0], (f"matched {key}, the only setup measured that "
                                     f"covers {float(near_mhz):g} MHz")
        return None, (f"{key} has {len(narrowed)} profiles ("
                      + "; ".join(sorted(_describe(h) for h in narrowed))
                      + ") — narrow it with --band / --ant / --subdev, or a --freq "
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


def load(node=None, pick=None, band=None, near_mhz=None, ant=None,
         subdev=None, args=None):
    """-> (values, path, why). values maps modem option -> value.

    A value is looked up in the chosen option first, then in the shared parts of
    the profile: the carrier belongs to the option, while the detector thresholds
    and the receive gain were measured once and apply to all of them.
    """
    path, why = find(node, band, near_mhz, ant, subdev, args)
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
    # A threshold nothing triggered is a floor, not a measurement. Say so where it
    # is applied, rather than letting a placeholder read as a measured value.
    if prof.get("sync_threshold_measured") is False:
        why = f"{why}; sync_threshold is a placeholder until a link is run"
    return vals, path, why


def _spans(path):
    """The usable frequency ranges a profile reports, as [(lo, hi), ...]."""
    try:
        with open(path) as fh:
            prof = json.load(fh)
    except Exception:
        return []
    out = []
    for o in (prof.get("options") or prof.get("candidates") or [prof]):
        b = o.get("band_mhz") or o.get("usable_mhz")
        if b and len(b) == 2:
            out.append((float(b[0]), float(b[1])))
    return sorted(out)


def all_profiles():
    """Every profile published into the shared workspace, from every node."""
    seen = {}
    for d in dirs():
        for path in sorted(glob.glob(os.path.join(d, "phy-*.json"))):
            seen.setdefault(os.path.basename(path), path)
    return sorted(seen.values())


def common_spectrum(paths=None):
    """Frequency ranges usable by EVERY surveyed node, widest first.

    Two ends of a link must sit on the same carrier, and each node's profile
    recommends the widest region *it* measured -- which on different testbeds is
    a different frequency. Left to their own profiles the two ends tune apart and
    never hear each other, with nothing in the logs to say why. The shared
    workspace holds every site's survey, so the overlap can simply be computed.
    """
    paths = paths or all_profiles()
    if not paths:
        return [], []
    cur = None
    used = []
    for p in paths:
        sp = _spans(p)
        if not sp:
            continue
        used.append(p)
        if cur is None:
            cur = sp
            continue
        merged = []
        for a_lo, a_hi in cur:
            for b_lo, b_hi in sp:
                lo, hi = max(a_lo, b_lo), min(a_hi, b_hi)
                if hi > lo:
                    merged.append((lo, hi))
        cur = sorted(merged)
    cur = cur or []
    return sorted(cur, key=lambda r: -(r[1] - r[0])), used


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--node", default=None, help="profile key (default: this node's)")
    ap.add_argument("--emit", choices=["human", "shell", "json"], default="human")
    ap.add_argument("--band", default=None,
                    help="which antenna's survey (vert900 / ism915 / vert2450 / "
                         "vert2450-5g). Needed when one radio carries two.")
    ap.add_argument("--args", default=None,
                    help="the radio this is about, e.g. addr=192.168.40.2 or "
                         "serial=30CD424 — needed when a host has several")
    ap.add_argument("--ant", default=None,
                    help="which connector the antenna is on (TX/RX, RX2)")
    ap.add_argument("--subdev", default=None,
                    help="which RF channel (A:0, A:A, A:B)")
    ap.add_argument("--near", type=float, default=None, metavar="MHz",
                    help="pick the band whose measured spectrum covers this")
    ap.add_argument("--pick", default=None, metavar="RANK|MHz",
                    help="use a saved candidate other than the recommended one: "
                         "a 1-based rank, or a carrier in MHz (nearest wins)")
    ap.add_argument("--common", action="store_true",
                    help="the spectrum every surveyed node can use — both ends of "
                         "a link must sit on the same carrier")
    ap.add_argument("--list", action="store_true",
                    help="show every candidate saved for this node")
    a = ap.parse_args()

    if a.common:
        spans, used = common_spectrum()
        if not used:
            print("[phy-profile] no surveys published yet")
            return 1
        print(f"[phy-profile] spectrum usable by ALL {len(used)} surveyed setups:")
        for p in used:
            print(f"    {os.path.basename(p)}")
        if not spans:
            print("\n  NONE — the surveys do not overlap. Both ends of a link must "
                  "sit on the same carrier, so one of these nodes has to use a band "
                  "it did not measure, or be re-surveyed on a band the other can "
                  "reach.")
            return 1
        print()
        for lo, hi in spans:
            print(f"    {lo:g}-{hi:g} MHz  ({hi - lo:g} MHz wide)  "
                  f"-> carrier {(lo + hi) / 2:g} MHz")
        lo, hi = spans[0]
        print(f"\n  Pin it for BOTH ends, so neither drifts onto its own favourite:")
        print(f'    topology "defaults": {{ "freq_mhz": {(lo + hi) / 2:g} }}')
        print(f"    or --freq {(lo + hi) / 2:g}   (run.sh) / --freq {(lo + hi) / 2:g}e6   (radio.sh)")
        return 0

    if a.list:
        path, why = find(a.node, a.band, a.near, a.ant, a.subdev, a.args)
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

    vals, path, why = load(a.node, a.pick, a.band, a.near, a.ant,
                           a.subdev, a.args)
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
