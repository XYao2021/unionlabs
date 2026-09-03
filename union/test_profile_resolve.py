#!/usr/bin/env python3
"""Does the PHY-profile resolver pick the right file?

prepare.sh now names each survey with its timestamp, so a re-survey of one
radio leaves TWO files for the same signal path in searching/. The resolver
must treat that as history (newest wins), not as an ambiguity to refuse -- while
still refusing a GENUINE ambiguity, a radio surveyed on two different bands with
no band named. Both failures are silent from a run that otherwise works: the
wrong survey just makes the radio a little wrong. So walk them here.
"""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "union"))
import phy_profile  # noqa: E402


def _profile(band, carrier, utc):
    return {"schema": 3, "node": "327D82F", "measured_utc": utc, "role": "rx",
            "radio": {"device": "n210", "args": "serial=327D82F", "ant": "RX2",
                      "subdev": "A:0", "gain_db": 25, "band": band},
            "noise": {"acq_p95": 6.2}, "det_mult": 30.0, "sync_threshold": 15.0,
            "use": 0,
            "options": [{"carrier_mhz": carrier,
                         "band_mhz": [carrier - 1, carrier + 1], "width_mhz": 2.0,
                         "floor_db": -93}]}


def _write(d, band, carrier, utc, stamp):
    p = os.path.join(d, f"phy-327D82F-{band}-A0-RX2-{stamp}.json")
    with open(p, "w") as fh:
        json.dump(_profile(band, carrier, utc), fh)
    return p


def main():
    checked = failures = 0

    def check(label, got, want):
        nonlocal checked, failures
        checked += 1
        if got != want:
            failures += 1
            print(f"  FAIL {label}: got={got!r} want={want!r}")

    orig = os.environ.get("UNION_SETTINGS_DIR")
    with tempfile.TemporaryDirectory() as d:
        os.environ["UNION_SETTINGS_DIR"] = d
        try:
            # two surveys of ONE path, different times -> newest wins
            _write(d, "vert2450", 2400.0, "2026-09-01T10:00:00Z", "20260901T100000Z")
            newer = _write(d, "vert2450", 2410.0, "2026-09-02T15:00:00Z",
                           "20260902T150000Z")
            vals, path, why = phy_profile.load(args="serial=327D82F")
            check("newest survey chosen", path, newer)
            check("newest carrier", vals.get("freq"), 2410.0)
            check("why says newest", "newest of 2 surveys" in (why or ""), True)

            # a THIRD, older still -> still the newest of three
            _write(d, "vert2450", 2405.0, "2026-08-20T09:00:00Z", "20260820T090000Z")
            _, path, _ = phy_profile.load(args="serial=327D82F")
            check("newest of three", path, newer)

            # a DIFFERENT band, no band named -> genuine ambiguity, must refuse
            _write(d, "vert900", 915.0, "2026-09-03T10:00:00Z", "20260903T100000Z")
            vals, path, why = phy_profile.load(args="serial=327D82F")
            check("cross-band refuses", path, None)
            check("refusal explains", "narrow it" in (why or ""), True)

            # naming the band collapses it to that band's newest
            vals, path, why = phy_profile.load(args="serial=327D82F", band="vert2450")
            check("band names it", path, newer)
            check("band's carrier", vals.get("freq"), 2410.0)
        finally:
            if orig is None:
                os.environ.pop("UNION_SETTINGS_DIR", None)
            else:
                os.environ["UNION_SETTINGS_DIR"] = orig

    if failures:
        print(f"  {failures} of {checked} profile-resolution paths FAILED")
        return 1
    print(f"  {checked} profile-resolution paths checked — newest survey wins, "
          f"real ambiguity still refused")
    return 0


if __name__ == "__main__":
    sys.exit(main())
