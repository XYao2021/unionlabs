#!/usr/bin/env python3
"""Does discover-node's reaper delete only ITS OWN records?

settings/ is shared. Alongside the node inventory records that discover-node
heartbeats and reaps, it holds files that are AUTHORED and never heartbeated:
link.json (the link both machines read), link-state.json (where the two ends
exchange survey-driven carriers), reservation.json, and the *.template.json seeds.

The reaper used to read `heartbeat` with a default of 0, so any file without one
looked infinitely stale and was deleted on the first pass. The symptom was not an
error: init-workspace reported the templates "written" on every single run, because
every run they had been deleted again, and auto_link could never find a link.json.
A shared folder means the reaper must be able to tell its own records from someone
else's files, so walk that boundary here — no radio, no pod, no shared mount needed.
"""
import importlib.util
import json
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "discover_node", os.path.join(REPO, "deploy", "workspace", "discover-node.py"))
discover_node = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(discover_node)


def main():
    checked = failures = 0

    def check(label, got, want):
        nonlocal checked, failures
        checked += 1
        if got != want:
            failures += 1
            print(f"  FAIL {label}: got={got!r} want={want!r}")

    now = time.time()
    GC_AGE = 300.0

    with tempfile.TemporaryDirectory() as d:
        def put(name, obj):
            with open(os.path.join(d, name), "w") as fh:
                json.dump(obj, fh)

        # ── discover-node's own records ──
        put("MYNODE.json", {"heartbeat": now, "radios": []})            # mine, fresh
        put("LIVE.json", {"heartbeat": now - 10, "radios": []})         # someone's, fresh
        put("DEAD.json", {"heartbeat": now - 9999, "radios": []})       # owner is gone
        # ── authored files that share the folder and carry no heartbeat ──
        put("link.json", {"schema": 1, "roles": {"source": {"serial": "315F2FB"}}})
        put("link-state.json", {"data_freq_hz": 5.33e9})
        put("reservation.json", {"schema": 1, "devices": []})
        put("link.template.json", {"_README": ["..."], "schema": 1})
        put("reservation.template.json", {"schema": 1})
        # a list, not a dict — still not a node record
        put("something-else.json", [1, 2, 3])
        # unparseable: a half-written record of ours, judged by mtime
        with open(os.path.join(d, "partial.json"), "w") as fh:
            fh.write('{"heartbeat": 17')
        os.utime(os.path.join(d, "partial.json"), (now - 9999, now - 9999))
        # not JSON at all
        with open(os.path.join(d, "README.md"), "w") as fh:
            fh.write("# settings\n")

        removed = sorted(discover_node.gc(d, GC_AGE, keep="MYNODE.json"))
        left = sorted(os.listdir(d))

        # only dead records of OUR kind are reaped
        check("dead node record reaped", "DEAD.json" in removed, True)
        check("stale half-written record reaped", "partial.json" in removed, True)
        check("exactly those two reaped", removed, ["DEAD.json", "partial.json"])

        # every authored file survives — this is the regression that broke auto_link
        for name in ("link.json", "link-state.json", "reservation.json",
                     "link.template.json", "reservation.template.json",
                     "something-else.json"):
            check(f"{name} survives the reaper", name in left, True)

        # and the live records + non-json are untouched
        check("own record kept", "MYNODE.json" in left, True)
        check("fresh record of another node kept", "LIVE.json" in left, True)
        check("non-json untouched", "README.md" in left, True)

        # a record that goes stale later IS reaped, so the reaper still works
        put("LIVE.json", {"heartbeat": now - 9999, "radios": []})
        removed2 = discover_node.gc(d, GC_AGE, keep="MYNODE.json")
        check("record reaped once its owner stops", "LIVE.json" in removed2, True)
        check("authored files still spared on a later pass",
              os.path.exists(os.path.join(d, "link.json")), True)

    if failures:
        print(f"  {failures} of {checked} node-record GC paths FAILED")
        return 1
    print(f"  {checked} node-record GC paths checked — reaps dead records, "
          f"never authored files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
