#!/usr/bin/env python3
"""Which layer decides the carrier -- and does a topology still outrank a survey?

It stopped doing so, silently. run_algo documents, in three separate comments, the
order

    what you type  >  --topology FILE  >  phy profile (searching/)  >  built-in

and applies the profile FIRST so that the topology's own _set can overwrite it. But
_set defers to _typed(), and _typed() has a fallback that asks "does this value differ
from the parser default?" -- which is exactly true of anything the profile just wrote.
So the profile's carrier read as a flag the experimenter had typed, the topology
declined to overwrite it, and the real order became

    what you type  >  phy profile  >  --topology FILE

The carrier is the one setting where that inversion is unrecoverable and invisible. A
topology pins one frequency for BOTH ends; a profile is per-radio and recommends the
widest quiet region THAT radio measured. Invert the two and the surveyed end tunes to
its own recommendation while the other end tunes to the file's -- and two radios on
different frequencies do not report an error. There is no failed connection, no
timeout that names a cause: the receiver just prints nothing, which is the most
expensive way for a link to break and is indistinguishable from a dead antenna, a
wrong gain, or a detector threshold set too high.

Worse, the guard against precisely this failure (warn_unpinned_carrier) stays quiet
when a topology is present, on the reasoning that the file speaks for both ends. The
bug made that reasoning false, so the one warning that would have named the problem
was switched off by the condition that caused it.

This drives the REAL path -- run_algo end to end, with a profile on disk and a
topology file -- because the bug lived in the seam between two layers that every
unit-level test of either layer stubs out. test_topology exercises apply_topology
without a profile; test_profile_backend exercises apply_phy_profile without a
topology; both passed throughout.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE_MHZ = 2437.0            # what the survey recommends
TOPOLOGY_MHZ = 2400            # what the file pins for both ends
TYPED_MHZ = 2450               # what the experimenter asks for outright


def write_profile(d):
    """A schema-3 survey of the X310 the echo-pair topology's rx node names."""
    os.makedirs(d, exist_ok=True)
    prof = {
        "schema": 3, "node": "precedence-test", "role": "rx",
        "measured_utc": "2026-10-07T00:00:00Z",
        "radio": {"device": "x310", "args": "addr=192.168.40.2", "ant": "RX2",
                  "subdev": "A:0", "gain_db": 17, "band": "vert2450"},
        "noise": {"floor_db": -70.0},
        "det_mult": 7, "sync_threshold": 33, "sync_threshold_measured": False,
        "options": [{"carrier_mhz": PROFILE_MHZ, "band_mhz": [2430, 2444],
                     "width_mhz": 14, "floor_db": -70.0,
                     "fits_default_link": True}],
        "use": 0,
    }
    p = os.path.join(d, "phy-precedence-test-vert2450-A0-RX2-20261007T000000Z.json")
    with open(p, "w") as fh:
        json.dump(prof, fh)
    return p


def strip_carrier(src, dst):
    """The same topology with no freq_mhz anywhere: the survey should fill the gap."""
    with open(src) as fh:
        topo = json.load(fh)
    for nd in topo["nodes"]:
        for side in ("tx", "rx"):
            if side in (nd.get("radio") or {}):
                nd["radio"][side].pop("freq_mhz", None)
    (topo.get("defaults") or {}).pop("freq_mhz", None)
    topo["name"] = os.path.basename(dst)[:-5]
    with open(dst, "w") as fh:
        json.dump(topo, fh)
    return dst


def plan(settings_dir, *extra):
    """run_algo's resolved plan, without running anything. sys.executable rather than
    run.sh's `python3`, so the check runs under whichever interpreter started it."""
    env = dict(os.environ, UNION_SETTINGS_DIR=settings_dir, PYTHONUNBUFFERED="1")
    for k in [k for k in env if k.startswith("UNION_") and k != "UNION_SETTINGS_DIR"]:
        del env[k]
    r = subprocess.run([sys.executable, os.path.join(REPO, "union", "run_algo.py"),
                        "--algo", "echo", "--print-plan", *extra],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
    return (r.stdout or "") + (r.stderr or "")


def carrier(out):
    m = re.search(r"freq-MHz\s+([0-9.]+)", out)
    return float(m.group(1)) if m else None


def main():
    checked = failures = 0

    def check(label, got, want):
        nonlocal checked, failures
        checked += 1
        if got != want:
            failures += 1
            print(f"  FAIL {label}: got={got!r} want={want!r}")

    tmp = tempfile.mkdtemp(prefix="union-carrier-")
    try:
        surveyed = os.path.join(tmp, "searching")
        write_profile(surveyed)
        bare = os.path.join(tmp, "empty")           # exists, holds no survey
        os.makedirs(bare, exist_ok=True)
        pinned = os.path.join(REPO, "deploy", "workspace", "topologies",
                              "echo-pair-x310.json")
        unpinned = strip_carrier(pinned, os.path.join(tmp, "echo-pair-nofreq.json"))
        node = ("--topology", pinned, "--node", "rx")

        # 1 | THE REGRESSION. Both layers have an opinion; the file must win, because
        #     it is the only one that speaks for the other end of the link too.
        out = plan(surveyed, *node)
        check("topology outranks the survey", carrier(out), float(TOPOLOGY_MHZ))
        check("...and the survey says what it offered", "[phy-profile]" in out, True)

        # 2 | the same file with nothing surveyed: unchanged, so the fix cannot be
        #     "the topology always wins because the profile never arrives"
        check("no survey -> topology", carrier(plan(bare, *node)),
              float(TOPOLOGY_MHZ))

        # 3 | a typed flag still beats both -- the part that was never broken, and
        #     the part a careless fix would break
        check("typed flag outranks both",
              carrier(plan(surveyed, *node, "--freq", str(TYPED_MHZ))),
              float(TYPED_MHZ))

        # 4 | and the survey is still USED where the file is silent. This is the
        #     reason the profile is applied first at all: a topology that omits the
        #     carrier is how a run picks up a fresh measurement without being edited.
        check("survey fills a gap the file leaves",
              carrier(plan(surveyed, "--topology", unpinned, "--node", "rx")),
              PROFILE_MHZ)

        # 5 | with no file at all the survey decides, and must SAY that the other end
        #     never agreed to it -- the warning whose condition the bug disabled
        out = plan(surveyed, "--channel", "usrp", "--role", "rx",
                   "--rx-args", "addr=192.168.40.2")
        check("survey decides when nothing else does", carrier(out), PROFILE_MHZ)
        check("...and warns nobody agreed on it", "NOTE" in out and "tune apart" in out,
              True)

        # 6 | a carrier the topology pinned is NOT an unagreed one, so the warning
        #     must not fire for it -- otherwise every topology run grows a scary note
        out = plan(surveyed, *node)
        check("pinned carrier draws no warning", "tune apart" in out, False)

        # 7 | --no-phy-profile leaves the file in sole charge
        check("--no-phy-profile -> topology",
              carrier(plan(surveyed, *node, "--no-phy-profile")),
              float(TOPOLOGY_MHZ))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f"  {failures} of {checked} carrier-precedence paths FAILED")
        return 1
    print(f"  {checked} carrier-precedence paths checked — typed > topology > survey, "
          f"and a survey still fills what the file leaves out")
    return 0


if __name__ == "__main__":
    sys.exit(main())
