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

    # ── the available-frequency list: flat, timestamped, and never superseded ──
    with tempfile.TemporaryDirectory() as d:
        rich = dict(prof)
        rich["node"] = "327D82F"
        rich["measured_local"] = "2026-09-02 11:00:00 EDT"
        rich["noise"] = {"acq_p95": 6.2, "floor_db": -95.4}
        rich["use"] = 1                      # the SECOND option is recommended
        rich["options"] = [
            {"carrier_mhz": 2412.0, "band_mhz": [2402.0, 2422.0], "width_mhz": 20.0,
             "floor_db": -93.1, "fits_default_link": True},
            {"carrier_mhz": 2437.0, "band_mhz": [2427.0, 2447.0], "width_mhz": 20.0,
             "floor_db": -96.8, "fits_default_link": True},
        ]
        f1 = prepare_phy.publish_frequencies(rich, d, "20260902T150000Z")
        check("freq file created", os.path.exists(f1), True)
        check("named by timestamp", os.path.basename(f1), "freqs-20260902T150000Z.json")
        rec = json.load(open(f1))
        check("plain carrier list", rec["available_mhz"], [2412.0, 2437.0])
        check("recommended follows 'use'", rec["recommended_mhz"], 2437.0)
        check("per-carrier detail kept", rec["frequencies"][1]["floor_db"], -96.8)
        check("radio identified", rec["radio"]["band"], "vert2450")
        check("noise floor carried", rec["noise_floor_db"], -95.4)

        # a later survey ACCUMULATES: the history is the point, so nothing is removed
        f2 = prepare_phy.publish_frequencies(rich, d, "20260903T150000Z")
        check("earlier survey kept", os.path.exists(f1) and os.path.exists(f2), True)
        check("both surveys on disk",
              len([x for x in os.listdir(d) if x.startswith("freqs-")]), 2)

        # a survey that found nothing usable still writes a readable file
        empty = dict(rich)
        empty["options"] = []
        empty["use"] = 0
        r3 = json.load(open(prepare_phy.publish_frequencies(empty, d, "20260904T150000Z")))
        check("empty survey still readable", r3["available_mhz"], [])
        check("empty survey has no recommendation", r3["recommended_mhz"], None)

        # ── the TOPOLOGY DRAFT ───────────────────────────────────────────────
        # A survey ends holding every number a topology needs except the far end's
        # identity, so prepare writes the file with the receiver complete. The draft
        # has to be a VALID topology, not merely valid JSON: it is handed to someone
        # as a starting point, and a scaffold that topology.py refuses is worse than
        # no scaffold, because the error arrives after the survey rather than here.
        full = dict(prof, options=[
            {"carrier_mhz": 2404.5, "band_mhz": [2402, 2407]},
            {"carrier_mhz": 2422.5, "band_mhz": [2421, 2424]},
            {"carrier_mhz": 2440.5, "band_mhz": [2439, 2442]}], use=1,
            sync_threshold=15, sync_threshold_measured=False)
        # PIN the topology directory. The draft now goes where topologies live, and
        # the resolver's last fallback is this checkout -- so without this the test
        # writes drafts into deploy/workspace/topologies/ and they ship.
        tdir = os.path.join(d, "topologies")
        os.makedirs(tdir, exist_ok=True)
        os.environ["UNION_TOPOLOGY_DIR"] = tdir
        # topology.py's stripper, because the draft carries a // header now: the
        # explanation lives above the JSON so the body reads like a topology, and a
        # test that parses it with plain json.load is testing the wrong contract.
        sys.path.insert(0, os.path.join(REPO, "union"))
        import topology as tp

        def read_draft(path):
            with open(path) as fh:
                text = fh.read()
            return text, json.loads(tp.strip_comments(text))

        dpath = prepare_phy.publish_topology_draft(full, d, "2026-10-08_00-00-00")
        header, draft = read_draft(dpath)
        check("draft lands in topologies/, with every other topology",
              os.path.dirname(dpath), tdir)
        check("draft is named by radio and time",
              os.path.basename(dpath),
              "draft-327D82F-2026-10-08_00-00-00.json")
        # the name INSIDE must match the filename, or the lister shows two entries
        # with one label and the shadowing report becomes nonsense
        check("inner name matches the filename",
              draft["name"],
              os.path.basename(dpath)[:-len(".json")])

        snk = [n for n in draft["nodes"] if n["id"] == "snk"][0]
        src = [n for n in draft["nodes"] if n["id"] == "src"][0]
        # the receive side is the SURVEY's, verbatim — this is the whole point: these
        # are the four fields people were transcribing by hand
        check("receiver keeps the surveyed antenna", snk["radio"]["rx"]["ant"], "RX2")
        check("receiver keeps the surveyed subdev", snk["radio"]["rx"]["subdev"], "A:0")
        check("receiver keeps the surveyed gain", snk["radio"]["rx"]["gain"], 25)
        check("receiver named by serial, not address",
              snk["radio"].get("serial"), "327D82F")
        # RECOMMENDED FIRST: `use` was 1, so 2422.5 must lead. The resolver takes the
        # first candidate inside a measured window, so order is the survey's own pick.
        check("candidates lead with the recommendation",
              snk["radio"]["rx"]["freq_mhz"], [2422.5, 2404.5, 2440.5])
        check("both ends offered the same candidates",
              src["radio"]["tx"]["freq_mhz"], snk["radio"]["rx"]["freq_mhz"])
        # the detector values, where a topology can now state them
        check("det_mult reaches defaults", draft["defaults"].get("det_mult"), 30.0)
        check("sync_threshold reaches defaults",
              draft["defaults"].get("sync_threshold"), 15)
        # the header carries the survey's findings, which is what someone opening
        # the file reads first -- and the body stays free of prose
        check("header lists every measured carrier",
              all(f"{c:g} MHz" in header for c in (2404.5, 2422.5, 2440.5)), True)
        check("header shows the window each sits in",
              "clear 2421-2424 MHz" in header, True)
        check("an unmeasured threshold says so in the header",
              "PLACEHOLDER" in header, True)
        check("the body carries no prose notes",
              [k for k in ("note",) if k in draft]
              + [k for nd in draft["nodes"] for k in ("note",) if k in nd], [])
        check("the header is comments, so the body is plain JSON",
              header.lstrip().startswith("//"), True)
        # only the far end is left to a person
        check("the transmitter is a placeholder",
              src["radio"].get("serial"), "REPLACE_ME_SOURCE_ID")
        # ...and it is the ONLY one: count placeholder FIELDS, not prose mentions, since
        # the note and description both name it on purpose so a reader knows what to fix
        def placeholders(obj, path=""):
            if isinstance(obj, dict):
                return [x for k, v in obj.items() if k not in ("note", "description")
                        for x in placeholders(v, f"{path}.{k}")]
            if isinstance(obj, list):
                return [x for i, v in enumerate(obj) for x in placeholders(v, path)]
            return [path] if isinstance(obj, str) and "REPLACE_ME" in obj else []
        check("...and it is the ONLY field left to a person",
              placeholders(draft), [".nodes.radio.serial"])

        # IT MUST LOAD. Round-trip through the real loader with the placeholder filled.
        ready = os.path.join(d, "ready.json")
        with open(ready, "w") as fh:
            fh.write(json.dumps(draft).replace("REPLACE_ME_SOURCE_ID", "F5B2C30"))
        try:
            t = tp.load(ready)
            check("the draft is a loadable topology", (len(t.nodes), len(t.links)), (2, 1))
        except tp.TopologyError as e:
            check("the draft is a loadable topology", f"refused: {e}", (2, 1))

        # IT MUST BE RECOGNISABLE AS A DRAFT. Living in topologies/ beside runnable
        # files, that is the only thing keeping it from being started: a placeholder
        # reaching UHD reads as "no device found" and blames the radio.
        check("the draft is detected as a draft",
              tp.placeholders(tp.load(dpath)),
              ["node src: radio.args = serial=REPLACE_ME_SOURCE_ID"])
        check("...and the filled-in copy is not",
              tp.placeholders(tp.load(ready)), [])

        # an addr-only radio keeps its address rather than inventing a serial
        byaddr = dict(full, radio=dict(full["radio"], args="addr=192.168.40.2"))
        _, da = read_draft(prepare_phy.publish_topology_draft(
            byaddr, d, "2026-10-08_01-00-00"))
        sa = [n for n in da["nodes"] if n["id"] == "snk"][0]
        check("addr radio keeps addr", sa["radio"].get("addr"), "192.168.40.2")
        check("...and no serial key is faked", "serial" in sa["radio"], False)

        # ── regenerating the draft from a survey already on disk ────────────
        # The draft is written at the end of a sweep. Anyone whose code predated it,
        # or whose write failed, would otherwise have to spend the band again on a
        # file built entirely from numbers already saved -- minutes and a radio for
        # a convenience. --topology-only reads what is there.
        sd = os.path.join(d, "surveys")
        os.makedirs(sd, exist_ok=True)
        for stamp, utc, args in (
                ("2026-10-07_23-37-23", "2026-10-07T23:37:23Z", "serial=3620E8D"),
                ("2026-10-06_10-00-00", "2026-10-06T10:00:00Z", "serial=3620E8D"),
                ("2026-10-08_09-00-00", "2026-10-08T09:00:00Z", "serial=OTHER")):
            q = dict(full, measured_utc=utc,
                     radio=dict(full["radio"], args=args))
            with open(os.path.join(
                    sd, f"phy-x-vert2450-A0-RX2-{stamp}.json"), "w") as fh:
                json.dump(q, fh)

        got, stamp = prepare_phy.newest_profile(sd, "serial=3620E8D")
        check("newest survey for THAT radio wins", stamp, "2026-10-07_23-37-23")
        check("...and it is that radio's", (got["radio"] or {})["args"],
              "serial=3620E8D")
        # the stamp comes from the SURVEY, so the draft is traceable to the
        # measurement rather than to when someone happened to regenerate it
        d2 = prepare_phy.publish_topology_draft(got, sd, stamp)
        check("draft carries the survey's stamp", os.path.basename(d2),
              "draft-3620E8D-2026-10-07_23-37-23.json")
        # unfiltered, the newest of ALL surveys wins
        _, any_stamp = prepare_phy.newest_profile(sd)
        check("unfiltered takes the newest of all", any_stamp, "2026-10-08_09-00-00")
        try:
            prepare_phy.newest_profile(os.path.join(d, "nothing-here"))
            check("no survey -> told to survey", "returned", "raised")
        except FileNotFoundError as e:
            check("no survey -> told to survey", "prepare.sh" in str(e), True)

        # a survey with nothing usable must REFUSE to write a draft, not emit a file
        # whose candidate list is empty -- that would resolve to no carrier at all
        try:
            prepare_phy.publish_topology_draft(prof, d, "2026-10-08_02-00-00")
            check("no usable carrier -> no draft", "wrote one anyway", "refused")
        except ValueError:
            check("no usable carrier -> no draft", "refused", "refused")

    if failures:
        print(f"  {failures} of {checked} prepare-publish paths FAILED")
        return 1
    print(f"  {checked} prepare-publish paths checked — write runs, name is "
          f"timestamped, supersede is path-scoped, frequency list accumulates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
