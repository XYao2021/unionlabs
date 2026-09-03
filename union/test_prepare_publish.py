#!/usr/bin/env python3
"""Does prepare.sh's profile WRITE path actually run?

prepare_phy's write path executes only on real hardware, at the end of a
several-minute survey — so a bug in it (a missing `import glob`, which happened)
slips past every hardware-free test and first appears in a session, after the
survey, with the measurement already thrown away. This calls the same function
main() calls, with a synthetic profile in a temp dir, so the write path is
exercised with no radio: it catches a NameError, a bad filename, or a supersede
that fails to remove the old survey.
"""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "drivers", "usrp", "python"))
import prepare_phy  # noqa: E402


def main():
    checked = failures = 0

    def check(label, got, want):
        nonlocal checked, failures
        checked += 1
        if got != want:
            failures += 1
            print(f"  FAIL {label}: got={got!r} want={want!r}")

    prof = {"schema": 3, "role": "rx", "measured_utc": "2026-09-02T15:00:00Z",
            "radio": {"device": "n210", "args": "serial=327D82F", "ant": "RX2",
                      "subdev": "A:0", "gain_db": 25, "band": "vert2450"},
            "noise": {"acq_p95": 6.2}, "det_mult": 30.0, "options": []}

    # the timestamp helper: one instant, readable local name, ISO-UTC record.
    # Pin TZ=UTC so the assertion is deterministic wherever this runs.
    _tz = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    try:
        import time as _time
        _time.tzset()
        utc, local, stamp = prepare_phy.survey_timestamps(epoch=1788138300)  # 2026-... fixed
        check("utc is ISO-Z", utc.endswith("Z") and "T" in utc, True)
        check("stamp has no colon/space", (":" not in stamp) and (" " not in stamp), True)
        check("stamp is readable date", stamp[:10], utc[:10])   # same Y-M-D at UTC
        check("local carries a zone or time", len(local) >= 19, True)
    finally:
        if _tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = _tz
        import time as _t2; _t2.tzset()

    with tempfile.TemporaryDirectory() as d:
        # first survey: file created, timestamped name, nothing superseded
        p1, rm1 = prepare_phy.publish_profile(prof, d, "327D82F", "vert2450",
                                              "A:0", "RX2", "20260901T100000Z")
        check("file created", os.path.exists(p1), True)
        check("timestamp in name", p1.endswith("20260901T100000Z.json"), True)
        check("path tags sanitised", os.path.basename(p1),
              "phy-327D82F-vert2450-A0-RX2-20260901T100000Z.json")
        check("nothing superseded yet", rm1, [])
        check("content round-trips", json.load(open(p1))["radio"]["band"], "vert2450")

        # an old un-stamped file of the same path is superseded by a new write
        legacy = os.path.join(d, "phy-327D82F-vert2450-A0-RX2.json")
        with open(legacy, "w") as fh:
            json.dump(prof, fh)
        p2, rm2 = prepare_phy.publish_profile(prof, d, "327D82F", "vert2450",
                                              "A:0", "RX2", "20260902T150000Z")
        check("legacy superseded", legacy in rm2, True)
        check("prior stamped superseded", p1 in rm2, True)
        check("old files gone", os.path.exists(p1) or os.path.exists(legacy), False)
        check("only the new one remains",
              sorted(os.path.basename(x) for x in os.listdir(d)),
              ["phy-327D82F-vert2450-A0-RX2-20260902T150000Z.json"])

        # a DIFFERENT signal path (other band) is NOT superseded
        p3, _ = prepare_phy.publish_profile(prof, d, "327D82F", "vert900",
                                            "A:0", "RX2", "20260902T160000Z")
        p4, rm4 = prepare_phy.publish_profile(prof, d, "327D82F", "vert2450",
                                              "A:0", "RX2", "20260902T170000Z")
        check("other band left alone", os.path.exists(p3), True)
        check("supersede is path-scoped", p3 in rm4, False)

    if failures:
        print(f"  {failures} of {checked} prepare-publish paths FAILED")
        return 1
    print(f"  {checked} prepare-publish paths checked — write runs, name is "
          f"timestamped, supersede is path-scoped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
