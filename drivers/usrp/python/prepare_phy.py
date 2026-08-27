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

--write publishes the profile to /workspace/experiments/settings/phy-<node>.json
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


# ── 1 · the usable band: widest contiguous quiet region of the sweep ─────────
def quiet_region(rows, margin_db=6.0):
    """-> (lo_mhz, hi_mhz, floor_db). Quiet = within margin of the sweep's
    lower-quartile floor; the widest contiguous run wins."""
    if not rows:
        sys.exit("[prepare] survey produced nothing — is the radio reachable?")
    p = sorted(r["power_db"] for r in rows)
    floor = p[len(p) // 4]
    quiet = [r["power_db"] <= floor + margin_db for r in rows]
    best, cur = (0, -1), None
    for i, q in enumerate(quiet + [False]):
        if q and cur is None:
            cur = i
        elif not q and cur is not None:
            if i - cur > best[1] - best[0]:
                best = (cur, i - 1)
            cur = None
    lo, hi = rows[best[0]]["freq_mhz"], rows[best[1]]["freq_mhz"]
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
    ap.add_argument("--dwell-windows", type=int, default=100)
    ap.add_argument("--acq-seconds", type=float, default=8.0)
    ap.add_argument("--node", default=os.uname().nodename,
                    help="name for the profile (default: hostname)")
    ap.add_argument("--write", action="store_true",
                    help="publish the profile to /workspace/experiments/settings/")
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
    lo, hi, floor = quiet_region(rows)
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

    profile = dict(
        schema=1, node=a.node, band=a.band, gain_db=a.gain,
        device=a.device, rx_ant=a.rx_ant, rx_subdev=subdev,
        usable_mhz=[lo, hi], carrier_mhz=carrier, floor_db=round(med, 1),
        detector_floor_db=(round(det_floor_db, 1) if det_floor_db is not None else None),
        detector_threshold_db=(round(det_thr_db, 1) if det_thr_db is not None else None),
        sense_minus_detector_db=(round(offset, 1) if offset is not None else None),
        det_mult=det_mult, sync_threshold=sync_thr,
        default_link_fits=fits,
        measured_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    print("\n── the hands-free settings ──────────────────────────────────")
    print(f"  radio.sh:  --freq {carrier:g}e6 --gain {a.gain:g} "
          f"--det-mult {det_mult:g} --sync-threshold {sync_thr:g}")
    print(f"  run.sh:    --freq {carrier:g} --usrp-set det_mult={det_mult:g} "
          f"--usrp-set sync_threshold={sync_thr:g}")
    print(f'  topology:  "defaults": {{ "freq_mhz": {carrier:g} }}')

    if a.write:
        d = "/workspace/experiments/settings"
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"phy-{a.node}.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(profile, fh, indent=2)
        os.replace(tmp, path)
        print(f"\n[prepare] profile published: {path} — visible to every "
              f"session of the account, on every testbed")
    else:
        print("\n[prepare] add --write to publish this profile to the shared workspace")


if __name__ == "__main__":
    main()
