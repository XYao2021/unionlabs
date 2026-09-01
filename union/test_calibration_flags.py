#!/usr/bin/env python3
"""Does calibration_rx.sh actually ADOPT what prepare.sh measured — and does a
typed flag still win?

prepare.sh publishes a PHY profile (carrier, receive gain, det-mult, noise ACQ
p95, timestamped). calibration_rx.sh claims to read it through the same
resolver radio.sh uses and to sit at the same rung of the chain:

    explicit flag  >  phy profile  >  built-in default

Nothing about that claim is visible from a run that works — a value that
quietly failed to arrive just makes the calibration a little wrong — so this
walks each adopted value through a synthetic profile and asserts it lands in
the composed modem command, then types each one and asserts the flag wins.
Companion to test_radio_flags.py, which holds radio.sh to the same standard.
"""
import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "calibration_rx.sh")

PROFILE = {
    "schema": 3, "measured_utc": "2026-09-01T15:00:00Z",
    "radio": {"device": "n210", "args": "addr=192.168.20.2", "ant": "RX2",
              "subdev": "A:0", "gain_db": 27.5, "band": "ism915"},
    "noise": {"acq_p95": 6.2, "floor_db": -93.0},
    "det_mult": 3.7, "sync_threshold": 9.9, "sync_threshold_measured": False,
    "use": 0,
    "options": [{"carrier_mhz": 902.5, "band_mhz": [902, 903],
                 "width_mhz": 1.0, "floor_db": -93.0}],
}


def compose(args, profile_path):
    """The modem command calibration_rx.sh would run (--dry-run needs nothing)."""
    env = dict(os.environ, UNION_PHY_PROFILE=profile_path)
    p = subprocess.run([SCRIPT] + args + ["--dry-run"], cwd=REPO,
                       capture_output=True, text=True, env=env)
    if p.returncode != 0:
        raise SystemExit(f"calibration_rx.sh {' '.join(args)} failed:\n"
                         f"{p.stderr or p.stdout}")
    line = [l for l in p.stdout.splitlines() if l.startswith(">> ")]
    if not line:
        raise SystemExit(f"no command line from calibration_rx.sh {' '.join(args)}")
    return line[0][3:].split(), p.stdout


def value_after(tokens, opt):
    if opt not in tokens:
        return None
    i = tokens.index(opt)
    return tokens[i + 1] if i + 1 < len(tokens) else None


def main():
    checked = failures = 0

    def check(label, got, want):
        nonlocal checked, failures
        checked += 1
        if got != want:
            failures += 1
            print(f"  FAIL {label}: got={got!r} want={want!r}")

    with tempfile.TemporaryDirectory() as td:
        prof = os.path.join(td, "phy-test.json")
        with open(prof, "w") as fh:
            json.dump(PROFILE, fh)

        # ── the profile's values reach the modem command ──
        toks, out = compose([], prof)
        check("adopt carrier",   value_after(toks, "--rx-freq"), "902.5e6")
        check("adopt rx gain",   value_after(toks, "--rx-gain"), "27.5")
        check("adopt det-mult",  value_after(toks, "--det-mult"), "3.7")
        # collection gate = 1.3 x noise acq_p95, NOT the profile's stored
        # sync_threshold: the gate must sit low so real peaks print
        check("gate from noise", value_after(toks, "--sync-threshold"), "8.1")
        check("adoption is announced", "[phy-profile]" in out, True)
        check("survey age shown", "2026-09-01T15:00:00Z" in out, True)
        # the printed TX line carries the adopted carrier to the other machine
        check("TX line inherits carrier",
              any("calibration_tx.sh --freq 902.5e6" in l for l in out.splitlines()),
              True)

        # ── a typed flag wins over the profile, value by value ──
        toks, _ = compose(["--freq", "915e6"], prof)
        check("typed freq wins", value_after(toks, "--rx-freq"), "915e6")
        toks, _ = compose(["--gain", "40"], prof)
        check("typed gain wins", value_after(toks, "--rx-gain"), "40")
        toks, _ = compose(["--noise-p95", "10"], prof)
        check("typed noise wins", value_after(toks, "--sync-threshold"), "13.0")
        # an appended raw modem option must REPLACE, not duplicate (the modem
        # rejects a repeated option outright)
        toks, _ = compose(["--det-mult", "9.9"], prof)
        check("appended det-mult replaces",
              (toks.count("--det-mult"), value_after(toks, "--det-mult")),
              (1, "9.9"))

        # ── no profile: built-in defaults, and no ghost det-mult ──
        toks, out = compose([], os.path.join(td, "no-such-profile.json"))
        check("default carrier", value_after(toks, "--rx-freq"), "915e6")
        check("default gain (n210)", value_after(toks, "--rx-gain"), "25")
        check("fallback gate", value_after(toks, "--sync-threshold"), "5")
        check("no det-mult from nowhere", "--det-mult" in toks, False)
        check("absence is announced", "no noise measurement" in out, True)

    if failures:
        print(f"  {failures} of {checked} calibration adoption paths FAILED")
        return 1
    print(f"  {checked} calibration adoption paths checked — profile values "
          f"arrive, typed flags win")
    return 0


if __name__ == "__main__":
    sys.exit(main())
