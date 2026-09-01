#!/usr/bin/env python3
"""
calibrate_sync — turn a receiving modem's own output into a measured
--sync-threshold, the number a receive-only survey cannot produce.

prepare_phy measures what NOISE scores on the ACQ correlator (the profile's
noise.acq_p95). What a REAL preamble scores on this link can only be seen while
a link runs — so calibration_rx.sh runs the receiver with the gate lowered to
just above the noise, calibration_tx.sh sends known bursts from the other
radio, and this follows the receiver's log:

    [ACQ]   Peak correlation = 27.4      <- what each burst scored
    [RX] chunk 3/5  [CRC OK, new]        <- proof the burst was real

Only peaks whose burst then passed CRC count. That filter is what makes the
manual TX step safe: a neighbour's burst or a noise trigger that sneaks over
the lowered gate decodes to garbage, fails CRC, and never pollutes the result.

The threshold is the same recipe prepare_phy applies on the noise side, now
with both ends measured: the geometric mean of noise p95 (N) and the weakest
CRC-passing peak (T), clamped to [1.3*N, 0.8*T] — never inside the noise
cloud, never so high a weak-but-real burst is refused.

    calibrate_sync.py --follow rx.log --noise-p95 6.2 --target 40 --timeout 300
    calibrate_sync.py --from-log rx.log --noise-p95 6.2      # offline
    calibrate_sync.py --self-test                            # selftest hook
"""
import argparse
import json
import math
import os
import re
import sys
import time

PEAK = re.compile(r"\[ACQ\]\s+Peak correlation = ([0-9.eE+-]+)")
CRC_OK = "[CRC OK"           # ", new]" and ", dup]" both count: both decoded
REJECT = "REJECTED"


def parse(text):
    """-> (good_peaks, rejected_peaks, unresolved).

    The modem prints one ACQ block per detected burst, then either a CRC-OK
    chunk line or a REJECTED line for that same burst. So association is a
    two-state machine: remember the last peak, resolve it on the next verdict.
    A peak that never gets a verdict (stream cut mid-burst) stays unresolved
    and counts for neither side.
    """
    good, rejected, pending = [], [], None
    for line in text.splitlines():
        m = PEAK.search(line)
        if m:
            if pending is not None:
                rejected.append(pending)   # verdict never printed: not proven real
            pending = float(m.group(1))
            continue
        if pending is None:
            continue
        if CRC_OK in line:
            good.append(pending)
            pending = None
        elif REJECT in line:
            rejected.append(pending)
            pending = None
    return good, rejected, (1 if pending is not None else 0)


def compute(noise_p95, good_peaks):
    """The threshold, from both measured ends. Returns a dict; 'ok' is False
    when the numbers do not support a safe choice, with 'why' saying so."""
    if not good_peaks:
        return {"ok": False, "why": "no CRC-passing burst was heard"}
    t_min = min(good_peaks)
    peaks = sorted(good_peaks)
    med = peaks[len(peaks) // 2]
    out = {"ok": True, "peak_min": round(t_min, 1), "peak_median": round(med, 1),
           "peak_max": round(peaks[-1], 1), "n_good": len(peaks)}
    if noise_p95 is None:
        # No noise measurement to anchor the floor. 0.6*T keeps headroom for a
        # weak burst but is a one-sided guess — say so rather than hide it.
        out.update(threshold=round(0.6 * t_min, 1), noise_p95=None,
                   note="no noise measurement (run prepare.sh) — threshold is "
                        "0.6 x the weakest real peak, one-sided")
        return out
    floor = 1.3 * noise_p95
    cap = 0.8 * t_min
    if floor > cap:
        out.update(ok=False, noise_p95=round(noise_p95, 1),
                   why=(f"noise p95 {noise_p95:.1f} and weakest real peak "
                        f"{t_min:.1f} leave no safe gap (floor {floor:.1f} > "
                        f"cap {cap:.1f}) — the link is too weak to threshold "
                        f"reliably; raise TX gain or move the radios and rerun"))
        return out
    thr = math.sqrt(noise_p95 * t_min)
    out.update(threshold=round(min(max(thr, floor), cap), 1),
               noise_p95=round(noise_p95, 1),
               floor=round(floor, 1), cap=round(cap, 1))
    return out


def follow(path, noise_p95, target, timeout, result_path=None):
    """Tail the receiver's log until `target` CRC-passing bursts or `timeout`
    seconds, printing one line per resolved burst; then compute and report."""
    t0 = time.time()
    seen = 0            # bytes consumed
    text = ""
    last_good = 0
    print(f"[calibrate] following {path} — need {target} CRC-passing bursts, "
          f"timeout {timeout:g}s", flush=True)
    while time.time() - t0 < timeout:
        try:
            with open(path, errors="replace") as fh:
                fh.seek(seen)
                chunk = fh.read()
                seen = fh.tell()
        except FileNotFoundError:
            chunk = ""
        if chunk:
            text += chunk
            good, rejected, _ = parse(text)
            for p in good[last_good:]:
                print(f"[calibrate]   burst {len(good):>3}: peak {p:g}  [CRC OK]",
                      flush=True)
            last_good = len(good)
            if len(good) >= target:
                break
        time.sleep(0.3)
    good, rejected, unresolved = parse(text)
    res = compute(noise_p95, good)
    res["n_rejected"] = len(rejected)
    res["seconds"] = round(time.time() - t0, 1)
    report(res)
    if result_path:
        with open(result_path, "w") as fh:
            json.dump(res, fh, indent=2)
    return 0 if res.get("ok") else 1


def report(res):
    if not res.get("ok"):
        print(f"[calibrate] FAILED: {res.get('why', '?')}", flush=True)
        if res.get("n_rejected"):
            print(f"[calibrate]   ({res['n_rejected']} burst(s) heard but "
                  f"rejected — RF arrives; decode does not)", flush=True)
        return
    print(f"[calibrate] {res['n_good']} CRC-passing bursts "
          f"({res.get('n_rejected', 0)} rejected) — peaks "
          f"{res['peak_min']:g} / {res['peak_median']:g} / {res['peak_max']:g} "
          f"(min/med/max)", flush=True)
    if res.get("noise_p95") is not None:
        print(f"[calibrate] sync-threshold {res['threshold']:g}   "
              f"(geometric mean of noise {res['noise_p95']:g} and weakest real "
              f"peak {res['peak_min']:g}, clamped to "
              f"[{res['floor']:g}, {res['cap']:g}])", flush=True)
    else:
        print(f"[calibrate] sync-threshold {res['threshold']:g}   "
              f"({res['note']})", flush=True)


def self_test():
    """The parser and the formula, against a synthetic log. Run by selftest so
    neither can rot silently when the modem's output format moves."""
    log = "\n".join(
        # two noise triggers over the lowered gate: detected, then rejected
        ["[ACQ]   Peak correlation = 7.1",
         "[RX] burst 1 REJECTED: CRC=FAIL",
         "[ACQ]   Peak correlation = 6.8",
         "[RX] burst 2 REJECTED: header implausible"] +
        # forty real bursts, weakest 24.1
        sum([[f"[ACQ]   Peak correlation = {24.1 + 0.2 * i:.1f}",
              f"[RX] chunk {i % 5 + 1}/5  [CRC OK, new]"] for i in range(40)], []) +
        # one burst cut off mid-decode: no verdict, must count for neither side
        ["[ACQ]   Peak correlation = 25.0"])
    good, rejected, unresolved = parse(log)
    assert len(good) == 40, f"good: {len(good)} != 40"
    assert len(rejected) == 2, f"rejected: {len(rejected)} != 2"
    assert unresolved == 1, f"unresolved: {unresolved} != 1"
    assert min(good) == 24.1, f"weakest: {min(good)}"

    r = compute(6.2, good)
    #   sqrt(6.2 * 24.1) = 12.22..., inside [8.06, 19.28] -> 12.2
    assert r["ok"] and r["threshold"] == 12.2, r
    r = compute(20.0, good)          # floor 26 > cap 19.3: no safe gap
    assert not r["ok"], r
    r = compute(None, good)          # no survey: one-sided 0.6*T
    assert r["ok"] and r["threshold"] == round(0.6 * 24.1, 1), r
    r = compute(6.2, [])             # silence is a failure, not a threshold
    assert not r["ok"], r
    print("calibrate_sync self-test: 4 scenarios checked")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--follow", metavar="LOG",
                    help="tail this receiver log until --target or --timeout")
    ap.add_argument("--from-log", metavar="LOG", help="parse a finished log")
    ap.add_argument("--noise-p95", type=float, default=None,
                    help="noise ACQ p95 from prepare.sh (the profile's noise.acq_p95)")
    ap.add_argument("--target", type=int, default=40,
                    help="stop after this many CRC-passing bursts")
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--result", metavar="FILE", help="also write the result JSON here")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if a.follow:
        return follow(a.follow, a.noise_p95, a.target, a.timeout, a.result)
    if a.from_log:
        with open(a.from_log, errors="replace") as fh:
            good, rejected, _ = parse(fh.read())
        res = compute(a.noise_p95, good)
        res["n_rejected"] = len(rejected)
        report(res)
        if a.result:
            with open(a.result, "w") as fh:
                json.dump(res, fh, indent=2)
        return 0 if res.get("ok") else 1
    ap.error("one of --follow / --from-log / --self-test is required")


if __name__ == "__main__":
    sys.exit(main())
