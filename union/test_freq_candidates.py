#!/usr/bin/env python3
"""A topology that names CANDIDATE carriers and lets the survey choose among them.

A topology used to pin one number per side, and that number was copied by hand out of
a survey -- so the file held a measurement with no provenance, which went stale in
silence every time the band moved or the rig was re-cabled. Nothing detected it: a
stale carrier is a legal carrier, so the run came up, tuned, and heard whatever was
actually there.

    "rx": { "freq_mhz": [915, 925] }

says instead what the experiment will ACCEPT, in order of preference, and lets the
measurement pick. The author states intent, the survey states fact, and neither has to
be re-typed when the other changes.

THE PART THAT IS EASY TO GET WRONG is which survey decides. A transmit carrier belongs
to the RECEIVER: only the receiver measured the air it has to hear in, which is why
prepare.sh refuses to survey a transmitter at all. Resolve a tx-side list against the
transmitting box's own survey and the two ends rank the same list by different
measurements -- the author wrote one list, both boxes obeyed it, and they still cannot
hear each other. Since /workspace is shared, either box can read the other's survey, so
the data carrier is resolved from the sink's measurement and the ACK carrier from the
source's, and the two ends agree by construction rather than by coincidence.

The invariant, as everywhere else in this codebase: source tx == sink rx, and sink tx
== source rx.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAND = [915, 925]


def survey(d, node, args, ant, subdev, lo, hi, carrier):
    """One receive-only survey: this path measured lo-hi MHz as usable."""
    prof = {"schema": 3, "node": node, "role": "rx",
            "radio": {"device": "b210", "args": args, "ant": ant, "subdev": subdev,
                      "gain_db": 40, "band": "vert900"},
            "noise": {"floor_db": -70.0}, "det_mult": 7, "sync_threshold": 30,
            "options": [{"carrier_mhz": carrier, "band_mhz": [lo, hi],
                         "width_mhz": hi - lo, "floor_db": -70.0}], "use": 0}
    safe = f"{subdev}-{ant}".replace(":", "").replace("/", "")
    p = os.path.join(d, f"phy-{node}-vert900-{safe}-20261007T000000Z.json")
    with open(p, "w") as fh:
        json.dump(prof, fh)
    return p


def rf_ack_topology(path, candidates=CAND):
    """The RF-ACK pair: every side offered the SAME list, so only the surveys differ."""
    topo = {"schema": 1, "name": os.path.basename(path)[:-5], "algo": "echo",
            "defaults": {"channel": "usrp", "steps": 4, "scheme": "QPSK"},
            "nodes": [
              {"id": "src", "role": "tx", "host": "127.0.0.1",
               "radio": {"device": "b210", "serial": "30CD424",
                         "tx": {"ant": "TX/RX", "subdev": "A:A", "gain": 70,
                                "freq_mhz": candidates},
                         "rx": {"ant": "RX2", "subdev": "A:B", "gain": 40,
                                "freq_mhz": candidates}}},
              {"id": "snk", "role": "rx", "host": "127.0.0.1", "ports": {"ack": 5599},
               "radio": {"device": "b210", "serial": "30CD3F7",
                         "rx": {"ant": "RX2", "subdev": "A:A", "gain": 40,
                                "freq_mhz": candidates},
                         "tx": {"ant": "TX/RX", "subdev": "A:B", "gain": 70,
                                "freq_mhz": candidates}}}],
            "links": [{"from": "src", "to": "snk",
                       "medium": {"up": "wireless", "down": "wireless"}}]}
    with open(path, "w") as fh:
        json.dump(topo, fh)
    return path


def plan(settings_dir, *extra):
    env = dict(os.environ, UNION_SETTINGS_DIR=settings_dir, PYTHONUNBUFFERED="1")
    for k in [k for k in env if k.startswith("UNION_") and k != "UNION_SETTINGS_DIR"]:
        del env[k]
    r = subprocess.run([sys.executable, os.path.join(REPO, "union", "run_algo.py"),
                        "--algo", "echo", "--print-plan", *extra],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def sides(out):
    got = {}
    for side in ("tx", "rx"):
        m = re.search(rf"^    {side}\s+.*freq=([0-9.]+)MHz", out, re.M)
        if m:
            got[side] = float(m.group(1))
    return got


def main():
    checked = failures = 0

    def check(label, got, want):
        nonlocal checked, failures
        checked += 1
        if got != want:
            failures += 1
            print(f"  FAIL {label}: got={got!r} want={want!r}")

    tmp = tempfile.mkdtemp(prefix="union-cand-")
    try:
        sd = os.path.join(tmp, "searching")
        os.makedirs(sd)
        # the sink hears 910-918; the source's ACK path hears 920-930. Neither box
        # measured what the other did, which is the normal cross-testbed situation.
        survey(sd, "sinkbox", "serial=30CD3F7", "RX2", "A:A", 910, 918, 915.0)
        survey(sd, "srcbox", "serial=30CD424", "RX2", "A:B", 920, 930, 925.0)
        topo = rf_ack_topology(os.path.join(tmp, "rfack-cand.json"))

        _, src = plan(sd, "--topology", topo, "--node", "src")
        _, snk = plan(sd, "--topology", topo, "--node", "snk")
        s, k = sides(src), sides(snk)

        # 1 | THE INVARIANT, reached with no carrier written in the file at all
        check("data: source tx == sink rx", s.get("tx"), k.get("rx"))
        check("ACK:  sink tx == source rx", k.get("tx"), s.get("rx"))

        # 2 | and each landed on what the END THAT LISTENS measured, not the other
        check("data carrier is the sink's 915", s.get("tx"), 915.0)
        check("ACK carrier is the source's 925", s.get("rx"), 925.0)
        check("two distinct carriers", s.get("tx") != s.get("rx"), True)

        # 3 | the transmitter must IGNORE its own survey. A decoy makes the source's
        #     own box measure 913-917 clear: resolved against itself the source would
        #     transmit at 915 by luck here, so the decoy instead moves the sink's
        #     window -- what matters is that the tx side follows the PEER.
        check("tx side names the peer's survey",
              "what snk measured" in src, True)
        check("...and the sink's survey is the file it read",
              "sinkbox" in src.split("transmit carrier")[1][:200], True)

        # 4 | a list the surveys rule out is REFUSED, not quietly resolved to the
        #     first entry -- that would tune the link into measured interference
        bad = rf_ack_topology(os.path.join(tmp, "rfack-bad.json"), [868, 433])
        code, out = plan(sd, "--topology", bad, "--node", "snk")
        check("impossible list refused", code, 1)
        check("...names what IS usable", "910-918" in out, True)
        check("...and what it rejected", "868" in out and "433" in out, True)

        # 5 | no survey at all: take the author's first preference, and SAY so. This
        #     is the ordinary state before anyone has run prepare.sh, and refusing
        #     would make a candidate list unusable exactly when it is handiest.
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty)
        code, out = plan(empty, "--topology", topo, "--node", "snk")
        check("no survey -> first candidate", sides(out).get("rx"), 915.0)
        check("...and says nothing confirmed it", "no survey for this radio" in out, True)
        check("...without failing the run", code, 0)

        # 6 | --no-phy-profile must not consult surveys at all
        code, out = plan(sd, "--topology", topo, "--node", "snk", "--no-phy-profile")
        check("--no-phy-profile -> first candidate", sides(out).get("rx"), 915.0)

        # 7 | a TYPED carrier still beats a candidate list, like every other source
        code, out = plan(sd, "--topology", topo, "--node", "snk", "--freq", "905")
        d = sides(out)
        check("typed --freq beats the list", (d.get("rx"), d.get("tx")), (905.0, 905.0))

        # 8 | a plain NUMBER still behaves exactly as before: the whole point is that
        #     every existing topology is untouched by this
        one = rf_ack_topology(os.path.join(tmp, "rfack-num.json"), 915)
        _, out = plan(sd, "--topology", one, "--node", "snk")
        check("a bare number is unchanged", sides(out).get("rx"), 915.0)
        check("...and consults no survey for it",
              "from candidates" in out, False)

        # 9 | ONE POOL SHARED BY BOTH DIRECTIONS is the accident this feature makes
        #     reachable: with no survey to separate them each side takes the pool's
        #     first entry, and an RF ACK on a single carrier cannot work -- the radio
        #     would have to hear the far end through its own transmission.
        _, out = plan(empty, "--topology", topo, "--node", "src")
        d = sides(out)
        check("shared pool + no survey collapses", d.get("tx"), d.get("rx"))
        check("...and that is warned about", "the ACK returns over RF" in out, True)
        check("...naming the fix", "candidate pool per side" in out, True)
        # with the surveys present the same file resolves apart, so no warning
        _, out = plan(sd, "--topology", topo, "--node", "src")
        check("surveys separate them, no warning",
              "the ACK returns over RF" in out, False)

        # 10 | the SHIPPED RF-ACK topology: separate pools per direction, so the
        #      no-survey fallback still splits and matches what it used to pin
        for node, want in (("tx", (915.0, 925.0)), ("rx", (925.0, 915.0))):
            _, out = plan(empty, "--topology", "echo-pair-wireless", "--node", node)
            d = sides(out)
            check(f"echo-pair-wireless {node} without surveys",
                  (d.get("tx"), d.get("rx")), want)
        _, out = plan(empty, "--topology", "echo-pair-wireless", "--node", "tx")
        check("...and draws no RF-ACK warning",
              "the ACK returns over RF" in out, False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f"  {failures} of {checked} candidate paths FAILED")
        return 1
    print(f"  {checked} candidate paths checked — the file offers carriers, the survey "
          f"picks, and the end that must LISTEN decides")
    return 0


if __name__ == "__main__":
    sys.exit(main())
