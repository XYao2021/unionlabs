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
        # .jsonc, because the file carries a // header and an editor judges comment
        # legality by extension -- VS Code flags every one in a .json as an error
        check("draft is named by radio and time, as .jsonc",
              os.path.basename(dpath),
              "draft-327D82F-2026-10-08_00-00-00.jsonc")
        # the name INSIDE must match the filename, or the lister shows two entries
        # with one label and the shadowing report becomes nonsense
        check("inner name matches the filename",
              draft["name"],
              os.path.splitext(os.path.basename(dpath))[0])

        snk = [n for n in draft["nodes"] if n["id"] == "snk"][0]
        src = [n for n in draft["nodes"] if n["id"] == "src"][0]
        # the receive side is the SURVEY's, verbatim — this is the whole point: these
        # are the four fields people were transcribing by hand
        check("receiver keeps the surveyed antenna", snk["radio"]["rx"]["ant"], "RX2")
        check("receiver keeps the surveyed subdev", snk["radio"]["rx"]["subdev"], "A:0")
        check("receiver keeps the surveyed gain", snk["radio"]["rx"]["gain"], 25)
        check("receiver named by serial, not address",
              snk["radio"].get("serial"), "327D82F")
        # THE SURVEY ALREADY CHOSE, so leaving its own recommendation as a blank
        # asked someone to retype a number the file was holding. `use` was 1, so
        # 2422.5 is the pick and 2404.5 the runner-up for the reply.
        check("the data carrier is the survey's pick",
              snk["radio"]["rx"]["freq_mhz"], 2422.5)
        check("both ends get the SAME data carrier",
              src["radio"]["tx"]["freq_mhz"], snk["radio"]["rx"]["freq_mhz"])
        check("the reply carrier is the runner-up, and differs",
              (snk["radio"]["tx"]["freq_mhz"], src["radio"]["rx"]["freq_mhz"]),
              (2404.5, 2404.5))
        check("...which a wireless ACK requires",
              snk["radio"]["tx"]["freq_mhz"] != snk["radio"]["rx"]["freq_mhz"], True)
        check("the alternatives are still offered beside the field",
              "Others measured usable: 2422.5 | 2404.5 | 2440.5" in header, True)

        # BOTH directions on BOTH nodes, with their own gain: with a wireless ACK
        # every node transmits and receives, so one `gain` per node cannot express it
        check("source has both directions", sorted(src["radio"].keys()),
              ["device", "rx", "serial", "tx"])
        check("sink has both directions",
              sorted(k for k in snk["radio"] if k in ("tx", "rx")), ["rx", "tx"])
        # The second RF path is the second ANTENNA PORT, not a second daughterboard:
        # one X310 board is full duplex and brings out TX/RX and RX2. Defaulting to
        # B:0 asked for a board that is usually not fitted, and on a radio with
        # antennas only on the first port it cannot work at all.
        check("both directions share the surveyed subdev",
              (src["radio"]["tx"]["subdev"], src["radio"]["rx"]["subdev"]),
              ("A:0", "A:0"))
        check("...and are separated by the antenna port",
              (src["radio"]["tx"]["ant"], src["radio"]["rx"]["ant"]),
              ("TX/RX", "RX2"))
        # a B210 is the exception: its two channels are the natural split
        b210 = dict(full, radio=dict(full["radio"], device="b210", subdev="A:A"))
        _, db = read_draft(prepare_phy.publish_topology_draft(
            b210, d, "2026-10-08_03-00-00"))
        bsrc = [n for n in db["nodes"] if n["id"] == "src"][0]
        check("a B210 splits across its two channels",
              bsrc["radio"]["rx"]["subdev"], "A:B")

        # the one switch that chooses how the reply travels
        check("ack_wireless is offered, defaulting to TCP",
              draft["defaults"]["ack_wireless"], False)
        check("...and is explained beside itself",
              "true = ACK over the air" in header, True)
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
              "sync_threshold is a PLACEHOLDER" in header, True)
        check("...and says what too low actually does",
              "locks" in header and "garbage" in header, True)
        check("the body carries no prose notes",
              [k for k in ("note",) if k in draft]
              + [k for nd in draft["nodes"] for k in ("note",) if k in nd], [])
        check("the header is comments, so the body is plain JSON",
              header.lstrip().startswith("//"), True)
        # ONLY the far end is left to a person
        check("the transmitter is the one placeholder",
              src["radio"].get("serial"), "REPLACE_ME_SOURCE_ID")
        # ...and it is the ONLY one: count placeholder FIELDS, not prose mentions, since
        # the note and description both name it on purpose so a reader knows what to fix
        def placeholders(obj, path=""):
            if isinstance(obj, dict):
                return [x for k, v in obj.items() if k not in ("note", "description")
                        for x in placeholders(v, f"{path}.{k}")]
            if isinstance(obj, list):
                return [x for i, v in enumerate(obj) for x in placeholders(v, path)]
            # the REAL predicate, not a second guess at it: a helper that knew only
            # about REPLACE_ME silently ignored every FILL_ blank
            return [path] if tp.is_placeholder(obj) else []
        # every blank is NAMED, and they are the only things left to a person
        # The far end and the carriers, and nothing else. No host blank: an unset
        # host has a working default (dial 127.0.0.1, bind 0.0.0.0) which is right
        # when both radios share a machine, so the header explains when to add one
        # rather than the body carrying a blank that is usually correct to delete.
        check("the far radio is the ONLY blank in the file",
              sorted(set(placeholders(draft))), [".nodes.radio.serial"])
        check("no host field is written at all",
              [n for n in draft["nodes"] if "host" in n], [])
        check("...and the header says when to add one",
              "ADD snk.host IF THE TWO RADIOS ARE ON DIFFERENT MACHINES" in header,
              True)
        check("...and that the reply leg is always a socket",
              "THE APPLICATION REPLY IS ALWAYS TCP" in header, True)

        # ── every tunable says what it is and which way to move it ───────────
        # A number with no note is a number nobody dares change, and det_mult and
        # sync_threshold are precisely the two that have to be iterated on real
        # hardware. The wording comes from the modem's own registry so the file
        # cannot drift from what the binary does.
        body_text = "\n".join(l for l in open(dpath).read().splitlines()
                              if not l.lstrip().startswith("//"))
        for key, must in (("det_mult", ("TUNE", "noise_floor x this", "too LOW")),
                          ("sync_threshold", ("TUNE", "31 after AGC", "garbage")),
                          ("bytes_length", ("BIGGER", "64 x this")),
                          ("max_attempts", ("0 = never give up",)),
                          ("scheme", ("bits per symbol", "MUST match both ends")),
                          ("fec", ("MUST match both ends",)),
                          ("steps", ("--algo runs only",))):
            line = [l for l in body_text.splitlines() if f'"{key}"' in l]
            check(f"{key} carries a note", bool(line), True)
            if line:
                check(f"...{key} says which way to move it",
                      [m for m in must if m not in line[0]], [])
        # gain means a different thing per direction, so one note for both would
        # make the reader pick -- which is the job the note is doing
        gains = [l for l in body_text.splitlines() if '"gain"' in l]
        check("gain notes exist on every block", len(gains), 4)
        check("...and are per direction",
              sorted({("tx" if "transmit power" in g else
                       "rx" if "receive gain" in g else "?") for g in gains}),
              ["rx", "tx"])
        # the header names the order to tune them in, which is not guessable
        check("the header says to tune det_mult first",
              "Tune det_mult first" in header, True)

        # IT MUST LOAD. Round-trip through the real loader with the placeholder filled.
        ready = os.path.join(d, "ready.json")
        with open(ready, "w") as fh:
            fh.write(json.dumps(draft)
                     .replace("REPLACE_ME_SOURCE_ID", "F5B2C30")
                     .replace('"REPLACE_ME_WITH_FREQ_OPTION"', "2422.5")
                     .replace('"REPLACE_ME_WITH_ACK_FREQ_OPTION"', "2440.5")
                     .replace('"FILL_SINK_IP_HERE"', '"10.0.0.40"')
                     .replace('"FILL_SOURCE_HOST_OR_DELETE"', '"10.0.0.30"'))
        try:
            t = tp.load(ready)
            check("the draft is a loadable topology", (len(t.nodes), len(t.links)), (2, 1))
        except tp.TopologyError as e:
            check("the draft is a loadable topology", f"refused: {e}", (2, 1))

        # IT MUST BE RECOGNISABLE AS A DRAFT. Living in topologies/ beside runnable
        # files, that is the only thing keeping it from being started: a placeholder
        # reaching UHD reads as "no device found" and blames the radio.
        # loaded with ack_wireless false, the reply blocks are dropped -- so the
        # blanks that remain are the ones that actually matter for a TCP-ACK run
        # ONE blank, named: everything else came from the survey
        check("the draft is a draft for exactly one reason",
              tp.placeholders(tp.load(dpath)),
              ["node src: radio.args = serial=REPLACE_ME_SOURCE_ID"])
        # SCOPED: the sink never has to know the source's serial. That radio is on
        # another machine, and asking for it here stops anyone bringing a link up one
        # end at a time -- start the receiver, watch it listen, then start the sender.
        check("the sink is not asked for the source's radio",
              [t for t in tp.placeholders(tp.load(dpath), "snk") if "src" in t], [])
        check("...and ack_wireless false drops the unused RF path",
              (tp.load(dpath).node("src").radio["rx"],
               tp.load(dpath).node("snk").radio["tx"]), (None, None))
        check("...and the filled-in copy is not",
              tp.placeholders(tp.load(ready)), [])

        # ── RADIOS ARE NAMED BY SERIAL, even when surveyed by address ────────
        # An address identifies a radio only within one host: 192.168.40.2 is UHD's
        # default for an X310, so two machines answer to it and both containers claim
        # the same node. The survey runs ON the box holding the radio, so UHD can be
        # asked what it is called.
        byaddr = dict(full, radio=dict(full["radio"], args="addr=192.168.40.2"))
        fake = os.path.join(d, "bin")
        os.makedirs(fake, exist_ok=True)
        with open(os.path.join(fake, "uhd_find_devices"), "w") as fh:
            fh.write('#!/bin/sh\necho "    serial: F5B2C30"\necho "    addr: 192.168.40.2"\n')
        os.chmod(os.path.join(fake, "uhd_find_devices"), 0o755)
        _saved_path = os.environ["PATH"]
        os.environ["PATH"] = fake + os.pathsep + _saved_path
        try:
            dp2 = prepare_phy.publish_topology_draft(byaddr, d, "2026-10-08_01-00-00")
            hdr2, da = read_draft(dp2)
            sa = [n for n in da["nodes"] if n["id"] == "snk"][0]
            check("an addressed radio is recorded by serial",
                  sa["radio"].get("serial"), "F5B2C30")
            check("...and the address is not kept as a second name",
                  "addr" in sa["radio"], False)
            check("...the file is named by serial too",
                  os.path.basename(dp2), "draft-F5B2C30-2026-10-08_01-00-00.jsonc")
            check("...and the header says where the serial came from",
                  "resolved from 192.168.40.2" in hdr2, True)
        finally:
            os.environ["PATH"] = _saved_path

        # with nothing answering -- --topology-only on a box that no longer holds the
        # radio -- the address it was surveyed with is kept rather than invented away
        _, da3 = read_draft(prepare_phy.publish_topology_draft(
            byaddr, d, "2026-10-08_04-00-00"))
        sa3 = [n for n in da3["nodes"] if n["id"] == "snk"][0]
        check("no radio answering -> keep the address",
              sa3["radio"].get("addr"), "192.168.40.2")
        check("...and do not fake a serial", "serial" in sa3["radio"], False)

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
              "draft-3620E8D-2026-10-07_23-37-23.jsonc")
        # unfiltered, the newest of ALL surveys wins
        _, any_stamp = prepare_phy.newest_profile(sd)
        check("unfiltered takes the newest of all", any_stamp, "2026-10-08_09-00-00")
        try:
            prepare_phy.newest_profile(os.path.join(d, "nothing-here"))
            check("no survey -> told to survey", "returned", "raised")
        except FileNotFoundError as e:
            check("no survey -> told to survey", "prepare.sh" in str(e), True)

        # ── never clobber a draft someone has been editing ──────────────────
        # --topology-only names the file by the SURVEY's stamp, so regenerating lands
        # on exactly the file being worked in: carriers chosen, ack_wireless flipped,
        # det_mult and sync_threshold tuned against the rig. A survey can be repeated
        # in minutes; a tuned link cannot.
        with open(dpath) as fh:
            before = fh.read()
        with open(dpath, "a") as fh:
            fh.write("\n// a human edited this\n")
        with open(dpath) as fh:
            edited = fh.read()
        try:
            prepare_phy.publish_topology_draft(full, d, "2026-10-08_00-00-00")
            check("an existing draft is not overwritten", "wrote over it", "refused")
        except FileExistsError as e:
            check("an existing draft is not overwritten", "refused", "refused")
            check("...and says why it matters",
                  "the file you have been editing" in str(e), True)
            check("...and how to proceed anyway", "--force" in str(e), True)
        with open(dpath) as fh:
            check("...the edit survived", fh.read(), edited)
        # --force is the deliberate way through
        prepare_phy.publish_topology_draft(full, d, "2026-10-08_00-00-00", force=True)
        with open(dpath) as fh:
            check("--force does overwrite", fh.read() == edited, False)
        with open(dpath, "w") as fh:
            fh.write(before)

        # ── a convenience must not die on a modem that predates it ───────────
        # Python reaches a node by git pull; a new C++ option reaches it only when
        # something recompiles. Boost refuses the WHOLE run over an option it does not
        # know -- "Error: unrecognised option '--quiet-phy'" -- so a file asking for
        # quieter logs cost the link, and the error named an option nobody typed.
        import modem_opts
        stub = os.path.join(d, "bin")
        os.makedirs(stub, exist_ok=True)

        def modem(help_text):
            q = os.path.join(stub, "sdr_system")
            with open(q, "w") as fh:
                fh.write(f'#!/bin/sh\nprintf "%s\\n" "{help_text}"\n')
            os.chmod(q, 0o755)
            modem_opts._CACHE.clear()
            return q

        old = modem("  --det-mult arg   auto-threshold noise multiplier")
        check("an old modem does not claim --quiet-phy",
              modem_opts.supports("quiet_phy", old), False)
        new = modem("  --quiet-phy [=arg(=1)] (=0)  silence the chatter")
        check("a current one does", modem_opts.supports("quiet_phy", new), True)
        # BOTH SPELLINGS, whichever way the caller writes it and whichever way --help
        # prints it. The C++ names are inconsistent (--fec-type hyphenated, --fec_soft
        # not), and the first version transformed the whole token -- turning
        # "--fec-soft" into "__fec_soft", because str.replace does not know which
        # hyphens are the option marker. Every underscore-named option then looked
        # absent, so a binary advertising --fec_soft was reported as predating it and
        # the remedy offered was a rebuild it had just had.
        und = modem("  --fec_soft [=arg(=1)] (=0)  soft-decision decode")
        for written in ("--fec_soft", "--fec-soft", "fec_soft", "fec-soft"):
            check(f"{written} is found in an underscore-spelled help",
                  modem_opts.supports(written, und), True)
        hyp = modem("  --fec-type arg   FEC code family")
        for written in ("--fec-type", "--fec_type", "fec_type"):
            check(f"{written} is found in a hyphen-spelled help",
                  modem_opts.supports(written, hyp), True)
        check("...and a flag in neither spelling is still absent",
              modem_opts.supports("--no-such-flag", hyp), False)
        check("...and neither invents an option that exists nowhere",
              modem_opts.supports("no-such-flag", new), False)
        # UNKNOWN COUNTS AS YES: nothing built, or a binary that will not answer, is
        # not evidence the option is missing -- and dropping a requested setting on a
        # guess is worse than letting the modem speak for itself
        modem_opts._CACHE.clear()
        check("an absent binary is not evidence",
              modem_opts.supports("quiet_phy", os.path.join(d, "nope")), True)

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
