#!/usr/bin/env python3
"""
prepare_phy.py — measure a testbed's RF environment ONCE and derive the PHY
parameters an experiment there should use. The goal is hands-free: a user on a
new testbed runs this, and the platform knows the usable band, the carrier,
the noise floor, and the detector thresholds — instead of the user tuning
--det-mult and --sync-threshold by folklore.

    python3 prepare_phy.py --device x310 --args addr=192.168.40.2 --band vert2450-5g
    python3 prepare_phy.py --device n210 --args addr=192.168.10.2   # ism915
    python3 prepare_phy.py ... --node siteB --write                 # publish profile
    python3 prepare_phy.py --dry-run

Four measurements, all receive-only:

  1. SURVEY the band the antenna serves (freq_survey's sweep) and find the
     widest contiguous quiet region -> the USABLE BAND, and its center ->
     the CARRIER (plateau center beats the literal minimum: the minimum is
     measurement noise, the center has margin on both sides).
  2. DWELL at that carrier (many sense windows) -> the NOISE FLOOR and its
     burstiness -> a DET-MULT with the measured headroom, clamped to the
     modem's own guidance (5..30).
  3. LISTEN with the detector forced open for a few seconds -> the noise's
     ACQ correlation distribution -> a SYNC-THRESHOLD between the noise peaks
     and a real preamble's peak (~the preamble length, 31).
  4. The quiet region's WIDTH -> the usable bandwidth -> whether the default
     2 MS/s link fits (it needs ~2 MHz + guard).

Between 2 and 3 it also reads the RECEIVER's own calibrated floor and reports
it against the survey's. Both measure 10*log10(mean|x|^2), but the detector
prints its floor linear while channel_sense prints dB, so the two have looked
incomparable -- a survey floor of -31 dB next to an apparent detect threshold
near -50 dB reads like a contradiction when it is mostly a difference in RX
gain. Measured here at one --gain, the remainder is a number in the profile
(sense_minus_detector_db). det-mult is a RATIO on the detector's own floor and
so is immune to the offset; an absolute --energy_threshold is not, and must be
quoted on the detector's scale.

--write publishes the profile to /workspace/experiments/searching/phy-<key>-<band>.json
(the same shared folder the node records live in), and every run prints the
ready-to-paste run.sh / radio.sh flags and the topology "defaults" snippet.
"""
import argparse
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time

from channel_sense import _run_sense
from freq_survey import BANDS, survey as band_survey

PREAMBLE_PEAK = 31.0            # a real preamble's ACQ correlation after AGC
DEFAULT_LINK_BW_MHZ = 2.4      # 2 MS/s + RRC roll-off + CFO guard


# ── 1 · the usable bands: EVERY contiguous quiet region of the sweep ─────────
def quiet_regions(rows, margin_db=6.0):
    """-> (sweep_floor_db, [region, ...]) ranked best-first.

    Quiet = within margin of the sweep's lower-quartile floor. Every contiguous
    run is kept, not just the winner: a testbed usually has several usable
    stretches, and which one is *best* depends on things this measurement cannot
    see -- a neighbouring experiment, a regulatory limit, an antenna that is
    only nominally in band. Recording all of them lets a topology choose a
    different one per node without re-measuring the site.

    Ranked by width first (bandwidth is the scarce resource), then by how quiet
    the region actually is."""
    if not rows:
        sys.exit("[prepare] survey produced nothing — is the radio reachable?")
    p = sorted(r["power_db"] for r in rows)
    floor = p[len(p) // 4]
    quiet = [r["power_db"] <= floor + margin_db for r in rows]

    runs, cur = [], None
    for i, q in enumerate(quiet + [False]):
        if q and cur is None:
            cur = i
        elif not q and cur is not None:
            runs.append((cur, i - 1))
            cur = None

    regions = []
    for a, b in runs:
        lo, hi = rows[a]["freq_mhz"], rows[b]["freq_mhz"]
        band = [r["power_db"] for r in rows[a:b + 1]]
        regions.append({
            "usable_mhz": [lo, hi],
            "width_mhz": round(hi - lo, 3),
            "carrier_mhz": round((lo + hi) / 2.0, 3),   # plateau centre, not the
                                                        # literal minimum: the
                                                        # minimum is measurement
                                                        # noise, the centre has
                                                        # margin on both sides
            "region_floor_db": round(sum(band) / len(band), 1),
        })
    regions.sort(key=lambda r: (-r["width_mhz"], r["region_floor_db"]))
    return floor, regions


def quiet_region(rows, margin_db=6.0):
    """The single best region, as (lo, hi, sweep_floor_db). Kept so existing
    callers and the older profile schema keep working."""
    floor, regions = quiet_regions(rows, margin_db)
    if not regions:
        sys.exit("[prepare] no quiet region found — the whole band is occupied?")
    lo, hi = regions[0]["usable_mhz"]
    return lo, hi, floor


# ── 2 · floor + det-mult from a dwell at the carrier ─────────────────────────
def dwell(freq_mhz, windows, window_ms, radio):
    rows = _run_sense(window_ms, windows, -999.0, rx_freq=freq_mhz * 1e6, **radio)
    powers = sorted(r["power_db"] for r in rows)
    med = powers[len(powers) // 2]
    p99 = powers[min(len(powers) - 1, int(len(powers) * 0.99))]
    # threshold must clear the p99 burstiness with 3 dB to spare; the modem's
    # own guidance bounds it (5 = default, 10-30 = over-the-air)
    mult = 10 ** ((p99 - med + 3.0) / 10.0)
    det_mult = round(min(30.0, max(5.0, mult)), 1)
    return med, p99, det_mult


# ── 3 · sync threshold from the noise's own ACQ correlations ─────────────────
_ACQ = re.compile(r"\[ACQ\]\s+Peak correlation = ([0-9.]+)")
# The detector reports its own floor in LINEAR units (only its threshold gets a
# dB line), so it has to be converted before it can be compared with anything.
_DET_FLOOR = re.compile(r"\[DETECTOR CALIBRATION\][^\n]*\n\s*Noise floor:\s*([0-9.eE+-]+)")
_DET_THR_DB = re.compile(r"Threshold \(dB\):\s*(-?[0-9.]+)")

def detector_floor(freq_mhz, seconds, radio, binary=None):
    """The floor the RECEIVER measures for itself, in dB.

    The survey and the dwell both use channel_sense, which reports
    10*log10(mean|x|^2). The detector computes exactly the same quantity
    (calculate_window_energy averages |x|^2 over its window) but prints the
    result LINEAR, so the two were never directly comparable by eye -- which is
    how a survey floor of -31 dB and an apparent RX detect threshold near
    -50 dB came to look like a contradiction.

    They are the same scale, but only at the same RX GAIN: gain shifts the
    floor by a constant number of dB. Measuring both here at --gain makes the
    remaining difference a real, reportable number instead of a suspicion.

    Returns (floor_db, threshold_db) or None if the receiver printed no
    calibration block.
    """
    import sdr
    cmd = sdr.SDR(role="rx", rx_freq=freq_mhz * 1e6, viz=False,
                  binary=binary, **radio).command()
    try:
        p = subprocess.run(shlex.split(cmd), capture_output=True, text=True,
                           timeout=seconds)
        out = p.stdout
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) \
              else (e.stdout or "")
    m = _DET_FLOOR.search(out)
    if not m:
        return None
    lin = float(m.group(1))
    floor_db = 10.0 * math.log10(lin + 1e-20)
    t = _DET_THR_DB.search(out)
    return floor_db, (float(t.group(1)) if t else None)


def noise_acq(freq_mhz, seconds, radio, binary=None):
    """Force the energy detector open (det-mult ~1) so noise triggers ACQ, and
    read the correlation peaks noise achieves. A threshold halfway (in dB
    terms) between that and a real preamble's ~31 rejects noise without
    risking real bursts. Returns (noise_p95, sync_threshold) or None if the
    RX printed no ACQ lines (a very quiet band may never trigger)."""
    import sdr
    cmd = sdr.SDR(role="rx", rx_freq=freq_mhz * 1e6, det_mult=1.05,
                  viz=False, binary=binary, **radio).command()
    try:
        p = subprocess.run(shlex.split(cmd), capture_output=True, text=True,
                           timeout=seconds)
        out = p.stdout
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) \
              else (e.stdout or "")
    peaks = sorted(float(m.group(1)) for m in _ACQ.finditer(out))
    if not peaks:
        return None
    p95 = peaks[min(len(peaks) - 1, int(len(peaks) * 0.95))]
    thr = round(min(PREAMBLE_PEAK * 0.8, (p95 * PREAMBLE_PEAK) ** 0.5), 1)
    thr = max(thr, round(p95 * 1.3, 1))          # never inside the noise cloud
    return p95, thr


# ── device discovery: the devices are automatic, the antenna is not ─────────
def find_devices():
    """Every USRP visible now, as [{type, product?, serial?, addr?}] — the same
    text-parse discover-node.py uses (uhd_find_devices has no machine mode)."""
    try:
        out = subprocess.run(["uhd_find_devices"], capture_output=True,
                             text=True, timeout=30).stdout
    except Exception:
        return []
    return parse_find_devices(out)


def parse_find_devices(out):
    radios, cur = [], None
    for line in out.splitlines():
        if line.startswith("-- UHD Device"):
            cur = {}
            radios.append(cur)
            continue
        m = re.match(r"\s+(serial|addr|type|name|product|resource):\s*(.*)$", line)
        if m and cur is not None and m.group(2).strip():
            cur[m.group(1)] = m.group(2).strip()
    return [r for r in radios if r]


def classify(dev):
    """-> (device_class, uhd_args, identity) for one discovered radio."""
    t = (dev.get("type") or "").lower()
    prod = (dev.get("product") or "").lower()
    if t == "b200" or "b21" in prod or "b20" in prod:
        ident = dev.get("serial", "")
        return "b210", f"serial={ident}", ident
    if t == "x300" or "x31" in prod or "x30" in prod:
        ident = dev.get("addr", "")
        return "x310", f"addr={ident}", ident
    # usrp2 family (N200/N210) and anything else network-addressed
    ident = dev.get("addr") or dev.get("serial", "")
    return "n210", (f"addr={ident}" if dev.get("addr") else f"serial={ident}"), ident


def band_for(ident, band_map, default_band):
    """The antenna is the one fact discovery cannot give: no radio can report
    what is screwed onto its connector. --band-map carries that knowledge."""
    for key, band in band_map.items():
        if key and key in ident:
            return band, True
    return default_band, False


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--band", choices=sorted(BANDS), default="ism915")
    ap.add_argument("--step-mhz", type=float, default=None,
                    help="survey step (default: 1 for <200 MHz bands, else 10)")
    ap.add_argument("--device", choices=["b210", "n210", "x310"], default="b210")
    ap.add_argument("--args", default="", help="UHD device args")
    ap.add_argument("--rx-ant", default="RX2")
    ap.add_argument("--subdev", default=None)
    ap.add_argument("--gain", type=float, default=25.0,
                    help="RX gain — use the gain the EXPERIMENT will use")
    ap.add_argument("--max-options", type=int, default=3, metavar="N",
                    help="how many usable carriers to save (default 3). Each is a "
                         "complete parameter combination; the widest is recommended.")
    ap.add_argument("--dwell-windows", type=int, default=100)
    ap.add_argument("--acq-seconds", type=float, default=8.0)
    ap.add_argument("--node", default=None,
                    help="name for the profile. Default: this node's stable key "
                         "-- $UNION_SITE, else the radio's serial, else "
                         "host-<hostname>. NOT the bare hostname: inside a "
                         "session that is the pod id and changes every session, "
                         "so a profile filed under it is lost on the next pod.")
    ap.add_argument("--write", action="store_true",
                    help="publish the profile to /workspace/experiments/searching/ "
                         "(override the directory with $UNION_SETTINGS_DIR)")
    ap.add_argument("--binary", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true",
                    help="discover every connected USRP and prepare each in "
                         "turn (devices are auto-detected; give each its "
                         "antenna with --band-map)")
    ap.add_argument("--band-map", default="",
                    help="--all: which antenna hangs on which radio, e.g. "
                         "'30CD424:vert900,192.168.40.2:vert2450-5g'. A device "
                         "not named here falls back to --band, with a warning "
                         "— the antenna cannot be probed.")
    a = ap.parse_args()
    if a.node is None:
        # Same identity rule as discover-node.py and union/phy_profile.py, so what
        # prepare_phy writes is what radio.sh and run.sh later look for.
        #
        # This file is drivers/usrp/python/prepare_phy.py, so the repo root is FOUR
        # levels up, not three. Three landed on drivers/, the import always failed,
        # and the except below quietly filed every profile under the hostname --
        # which inside a session is the pod id and changes with the pod. The
        # profile was written, reported as published, and then orphaned on the next
        # session: precisely the failure the stable key exists to prevent, hidden
        # by the fallback that was supposed to be the safety net.
        repo = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))
        sys.path.insert(0, repo)
        try:
            from union.phy_profile import node_key
            a.node = node_key()
        except Exception as e:
            a.node = "host-" + os.uname().nodename
            print(f"[prepare] could not read this node's stable key "
                  f"({e.__class__.__name__}: {e}) — filing under {a.node!r}, which "
                  f"is this session's pod name and will not be found by the next "
                  f"session. Pass --node, or set $UNION_SITE.", file=sys.stderr)

    if a.all:
        band_map = {}
        for pair in a.band_map.split(","):
            if ":" in pair:
                k, v = pair.split(":", 1)
                if v.strip() not in BANDS:
                    sys.exit(f"[prepare] --band-map: unknown band {v.strip()!r} "
                             f"(use {', '.join(sorted(BANDS))})")
                band_map[k.strip()] = v.strip()
        devs = find_devices()
        if not devs:
            sys.exit("[prepare] --all: no USRPs visible (uhd_find_devices "
                     "found nothing)")
        print(f"[prepare] {len(devs)} device(s) visible:")
        plans = []
        for d in devs:
            cls, args_str, ident = classify(d)
            band, mapped = band_for(ident, band_map, a.band)
            note = "" if mapped else "  (no --band-map entry: assuming " + band + ")"
            print(f"  {cls:>5}  {args_str:<28} -> band {band}{note}")
            plans.append((cls, args_str, ident, band))
        if a.dry_run:
            return
        for cls, args_str, ident, band in plans:
            print(f"\n════ preparing {cls} {args_str} ({band}) ════")
            argv = ["--device", cls, "--args", args_str, "--band", band,
                    "--gain", str(a.gain), "--rx-ant", a.rx_ant,
                    "--node", f"{a.node}-{ident.replace('.', '-')}"]
            if a.subdev:
                argv += ["--subdev", a.subdev]
            if a.write:
                argv += ["--write"]
            if a.binary:
                argv += ["--binary", a.binary]
            r = subprocess.run([sys.executable, os.path.abspath(__file__)] + argv)
            if r.returncode != 0:
                print(f"[prepare] {args_str} FAILED (exit {r.returncode}) — "
                      f"continuing with the rest", file=sys.stderr)
        return

    lo0, hi0, why = BANDS[a.band]
    step = a.step_mhz or (1.0 if hi0 - lo0 <= 200 else 10.0)
    subdev = a.subdev or {"b210": "A:A", "n210": "A:0", "x310": "A:0"}[a.device]
    radio = dict(rx_args=a.args, rx_gain=a.gain, rx_ant=a.rx_ant, rx_subdev=subdev)
    if a.binary:
        radio["binary"] = a.binary

    n_pts = int((hi0 - lo0) / step) + 1
    plan = (f"[prepare] {a.band} ({lo0:g}-{hi0:g} MHz @ {step:g}) -> dwell "
            f"{a.dwell_windows} windows -> detector cross-check 6s -> "
            f"ACQ listen {a.acq_seconds:g}s   "
            f"(~{n_pts * 2 + a.acq_seconds + 16:.0f}s total, receive-only)")
    print(plan)
    if a.dry_run:
        return

    # 1 · survey
    freqs = [round(lo0 + i * step, 6) for i in range(n_pts)]
    rows = band_survey(freqs, 10.0, a.gain, a.args, a.rx_ant, subdev, a.binary)
    sweep_floor, regions = quiet_regions(rows)
    if not regions:
        sys.exit("[prepare] no quiet region found — the whole band is occupied?")
    lo, hi = regions[0]["usable_mhz"]
    floor = sweep_floor
    if len(regions) > 1:
        print(f"[prepare] {len(regions)} quiet regions found; all are saved, the "
              f"widest is the recommendation:")
        for i, r in enumerate(regions[:6]):
            mark = "  <- recommended" if i == 0 else ""
            print(f"           {i + 1}. {r['usable_mhz'][0]:g}-{r['usable_mhz'][1]:g} MHz "
                  f"({r['width_mhz']:g} MHz, {r['region_floor_db']:.1f} dB) "
                  f"carrier {r['carrier_mhz']:g}{mark}")
    carrier = round((lo + hi) / 2.0, 3)
    width = hi - lo
    print(f"\n[prepare] usable band: {lo:g}-{hi:g} MHz ({width:g} MHz wide), "
          f"floor ~{floor:.1f} dB -> carrier {carrier:g} MHz (plateau center)")

    # 2 · dwell
    med, p99, det_mult = dwell(carrier, a.dwell_windows, 10.0, radio)
    print(f"[prepare] dwell at {carrier:g}: floor {med:.1f} dB, p99 {p99:.1f} dB "
          f"-> det-mult {det_mult:g}")

    # 2b · the receiver's OWN floor, on the same gain, so the two scales can be
    #      compared instead of guessed at
    det = detector_floor(carrier, 6.0, radio, a.binary)
    if det:
        det_floor_db, det_thr_db = det
        offset = med - det_floor_db
        print(f"[prepare] receiver's own floor {det_floor_db:.1f} dB"
              + (f" (its threshold {det_thr_db:.1f} dB)" if det_thr_db is not None else "")
              + f" -> sense is {offset:+.1f} dB from the detector at gain {a.gain:g}")
        if abs(offset) > 3.0:
            print(f"           the two disagree by {abs(offset):.1f} dB. det-mult is a "
                  f"RATIO on the detector's own floor, so it is unaffected; but an "
                  f"absolute --energy_threshold must be quoted on the detector's scale, "
                  f"not the survey's.")
    else:
        det_floor_db = det_thr_db = offset = None
        print("[prepare] receiver printed no calibration block — cannot cross-check "
              "the survey's floor against the detector's")

    # 3 · ACQ noise distribution
    acq = noise_acq(carrier, a.acq_seconds, radio, a.binary)
    if acq:
        noise_p95, sync_thr = acq
        print(f"[prepare] noise ACQ p95 {noise_p95:.1f} (real preamble ~{PREAMBLE_PEAK:g}) "
              f"-> sync-threshold {sync_thr:g}")
    else:
        sync_thr = 15.0
        print("[prepare] band too quiet to trigger noise ACQ — keeping the "
              "default sync-threshold 15")

    # 4 · does the default link fit?
    fits = width >= DEFAULT_LINK_BW_MHZ
    if not fits:
        print(f"[prepare] WARNING: quiet region ({width:g} MHz) is narrower than "
              f"the default 2 MS/s link needs (~{DEFAULT_LINK_BW_MHZ} MHz) — "
              f"lower the rates or find another band")

    # ONE place per value. The previous layout stored `recommended` as a full
    # copy of candidates[0] and then repeated the whole lot again as flat keys
    # for an older reader, so a four-region survey wrote 102 lines in which every
    # number appeared five or six times -- long enough that nobody read it, and
    # ambiguous about which copy was authoritative.
    #
    # Here: what was measured once (the noise, the thresholds derived from it)
    # sits once at the top. `options` holds the usable carriers, each a complete
    # parameter combination on its own, and `use` says which one is recommended.
    def option(r, measured):
        o = {"carrier_mhz": r["carrier_mhz"],
             "band_mhz":    r["usable_mhz"],
             "width_mhz":   r["width_mhz"],
             "floor_db":    r["region_floor_db"],
             "fits_default_link": r["width_mhz"] >= DEFAULT_LINK_BW_MHZ}
        if measured:
            # only the recommended carrier is dwelt on; the rest inherit the
            # thresholds, and this says so rather than implying every option was
            # measured with equal care
            o["measured_here"] = True
        return o

    keep = regions[:a.max_options]
    dropped = len(regions) - len(keep)
    options = [option(r, i == 0) for i, r in enumerate(keep)]
    if dropped:
        print(f"[prepare] saving the top {len(keep)} of {len(regions)} regions "
              f"(--max-options to keep more); dropped: "
              + ", ".join(f"{r['carrier_mhz']:g} MHz" for r in regions[len(keep):]))

    profile = {
        "schema": 3,
        "node": a.node,
        "measured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "radio": {"device": a.device, "args": a.args, "ant": a.rx_ant,
                  "subdev": subdev, "gain_db": a.gain, "band": a.band},
        # measured once, at the recommended carrier, and shared by every option
        "noise": {"sweep_floor_db": round(sweep_floor, 1),
                  "floor_db": round(med, 1),
                  "p99_db": round(p99, 1),
                  "detector_floor_db": (round(det_floor_db, 1)
                                        if det_floor_db is not None else None),
                  "detector_threshold_db": (round(det_thr_db, 1)
                                            if det_thr_db is not None else None),
                  "sense_minus_detector_db": (round(offset, 1)
                                              if offset is not None else None),
                  "acq_p95": (round(acq[0], 1) if acq else None)},
        "det_mult": det_mult,
        "sync_threshold": sync_thr,
        "use": 0,
        "options": options,
    }

    print("\n── the hands-free settings ──────────────────────────────────")
    print(f"  radio.sh:  --freq {carrier:g}e6 --gain {a.gain:g} "
          f"--det-mult {det_mult:g} --sync-threshold {sync_thr:g}")
    print(f"  run.sh:    --freq {carrier:g} --usrp-set det_mult={det_mult:g} "
          f"--usrp-set sync_threshold={sync_thr:g}")
    print(f'  topology:  "defaults": {{ "freq_mhz": {carrier:g} }}')

    if a.write:
        # searching/, not settings/: settings holds per-session records that are
        # rewritten every session start and reaped when the pod goes. A band
        # survey costs minutes and a radio and stays true for as long as the
        # antenna and the room do, so it does not belong in a folder whose
        # contents are expected to churn.
        d = os.environ.get("UNION_SETTINGS_DIR") or "/workspace/experiments/searching"
        os.makedirs(d, exist_ok=True)
        # The band is part of the name: one radio can carry two antennas -- a
        # VERT900 on one port and a VERT2450 on another -- and each needs its own
        # survey. Keyed on the serial alone, the second measurement silently
        # replaced the first, and the survivor was whichever ran last.
        path = os.path.join(d, f"phy-{a.node}-{a.band}.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(profile, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())     # a network share can lose a buffered write
        os.replace(tmp, path)

        # Prove it landed. os.replace returning is not evidence the bytes are on
        # the share: report what is actually readable at the path, and where that
        # path really is, because "published" against a directory that is not the
        # mount everyone else sees looks identical to success.
        try:
            size = os.path.getsize(path)
            import subprocess as _sp
            mnt = _sp.run(["df", "-h", d], capture_output=True, text=True).stdout
            mnt = (mnt.strip().splitlines() or [""])[-1]
            print(f"[prepare] on disk: {size} bytes at {path}")
            print(f"[prepare] that path lives on: {mnt}")
        except Exception as e:
            print(f"[prepare] WARNING: wrote {path} but cannot stat it back "
                  f"({e.__class__.__name__}: {e}) — it may not have persisted",
                  file=sys.stderr)
        print(f"\n[prepare] profile published: {path} — visible to every "
              f"session of the account, on every testbed")

        # Read it back the way run.sh and radio.sh will. Writing a file and
        # announcing success proves only that a write succeeded; it does not
        # prove the thing that matters, which is that the resolver finds it under
        # the key it was filed with. Saying "published" without checking is how a
        # profile came to be written, reported, and then never picked up.
        try:
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))))
            from union import phy_profile as pp
            vals, found, why = pp.load()
            if found and os.path.samefile(found, path):
                print(f"[prepare] verified: run.sh and radio.sh resolve this "
                      f"profile ({why}) — "
                      + ", ".join(f"{k}={v}" for k, v in sorted(vals.items())))
            elif found:
                print(f"[prepare] WARNING: written, but the resolver picks a "
                      f"DIFFERENT profile first: {found} ({why}). This one will be "
                      f"ignored until that is removed or --phy-profile-node names "
                      f"it.", file=sys.stderr)
            else:
                print(f"[prepare] WARNING: written, but nothing resolves it back "
                      f"({why}). It will not be used. Check that $UNION_SETTINGS_DIR "
                      f"or /workspace/experiments/settings is the same path both "
                      f"sides see.", file=sys.stderr)
        except Exception as e:
            print(f"[prepare] could not verify the profile is readable "
                  f"({e.__class__.__name__}: {e})", file=sys.stderr)
    else:
        print("\n[prepare] add --write to publish this profile to the shared workspace")


if __name__ == "__main__":
    main()
