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

--write publishes the profile to /workspace/experiments/settings/phy-<node>.json
(the same shared folder the node records live in), and every run prints the
ready-to-paste run.sh / radio.sh flags and the topology "defaults" snippet.
"""
import argparse
import json
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
    a = ap.parse_args()

    lo0, hi0, why = BANDS[a.band]
    step = a.step_mhz or (1.0 if hi0 - lo0 <= 200 else 10.0)
    subdev = a.subdev or {"b210": "A:A", "n210": "A:0", "x310": "A:0"}[a.device]
    radio = dict(rx_args=a.args, rx_gain=a.gain, rx_ant=a.rx_ant, rx_subdev=subdev)
    if a.binary:
        radio["binary"] = a.binary

    n_pts = int((hi0 - lo0) / step) + 1
    plan = (f"[prepare] {a.band} ({lo0:g}-{hi0:g} MHz @ {step:g}) -> dwell "
            f"{a.dwell_windows} windows -> ACQ listen {a.acq_seconds:g}s   "
            f"(~{n_pts * 2 + 10:.0f}s total, receive-only)")
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
