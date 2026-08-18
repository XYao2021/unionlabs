#!/usr/bin/env python3
"""
selftest.py — does this installation actually work?

Runs every experiment over every PHY that needs no hardware, and prints one pass/fail
table. Nothing here touches a radio, so it is safe to run anywhere, any time — on a
fresh clone it is the first thing to run, and after a change it is the thing that says
whether you broke something.

    ./run.sh selftest             # the standard sweep (~1 min)
    ./run.sh selftest --quick     # one experiment per PHY (~15 s)
    ./run.sh selftest --full      # + every role, every topology (~4 min)

Exit code is 0 when everything passed, 1 otherwise, so CI can use it directly.

WHAT IT DOES NOT COVER: anything needing hardware — the LoRa serial/spi backends, the
USRP radio roles, and the two-host role split. Those are listed as SKIPPED with the
reason, so the output never implies more coverage than it has.
"""
import argparse
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

GREEN, RED, YEL, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YEL = DIM = OFF = ""


def experiments():
    """Every folder under experiments/ that has an app.py (so not _shared or _template)."""
    root = os.path.join(REPO, "experiments")
    return sorted(n for n in os.listdir(root)
                  if not n.startswith("_")
                  and os.path.isfile(os.path.join(root, n, "app.py")))


# How each experiment wants to be run, when its default (loopback) is not right.
ROLE_OVERRIDE = {
    "marl_multi":  ["--role", "multi", "--steps", "40"],
    "stc_aircomp": ["--role", "aircomp", "--agents", "4", "--steps", "2"],
    "dl":          ["--role", "gossip", "--agents", "3", "--steps", "2"],
}

# Keep the models small so a full sweep stays under a minute.
SMALL = {"FL_HIDDEN": "8", "DL_HIDDEN": "8"}


def run(args, timeout=600):
    env = dict(os.environ, **SMALL)
    t0 = time.time()
    try:
        p = subprocess.run([os.path.join(REPO, "run.sh")] + args, cwd=REPO, env=env,
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, time.time() - t0, (p.stderr or p.stdout)
    except subprocess.TimeoutExpired:
        return False, time.time() - t0, f"timed out after {timeout}s"


# An experiment that needs torch, cv2 or the compiled pyphy extension cannot run if
# they are not installed. That is a missing OPTIONAL dependency, not a broken install
# — reporting it as FAILED sends a newcomer hunting a bug that does not exist.
MISSING_DEP = [
    ("No module named 'torch'",  "torch not installed (pip install torch)"),
    ("No module named 'cv2'",    "opencv not installed (pip install opencv-python-headless)"),
    ("No module named 'networkx'", "networkx not installed"),
    ("pyphy",                    "pyphy not built for this Python (drivers/usrp/bindings/build.sh)"),
]


def missing_dependency(output):
    """-> a human reason if this failed only because something is not installed."""
    for needle, reason in MISSING_DEP:
        if needle in output:
            return reason
    return None


def why(output):
    """The one line from a failure that actually explains it."""
    for line in reversed(output.strip().splitlines()):
        s = line.strip()
        if s and not s.startswith(("File ", "  ", "Traceback")):
            return s[:90]
    return "(no output)"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="one experiment per PHY")
    ap.add_argument("--full", action="store_true", help="also every role and topology")
    a = ap.parse_args()

    exps = experiments()
    checks = []   # (group, label, args)

    if a.quick:
        for ch in ("ideal", "usrp", "lora"):
            checks.append(("PHYs", f"echo over {ch}",
                           ["--algo", "echo", "--channel", ch, "--steps", "2", "--snr-db", "10"]))
    else:
        for e in exps:
            checks.append(("experiments (ideal PHY)", e,
                           ["--algo", e] + ROLE_OVERRIDE.get(e, ["--steps", "2"])))
        for ch in ("usrp", "lora"):
            for e in ("echo", "fl", "dl"):
                checks.append((f"{ch} PHY", e,
                               ["--algo", e, "--channel", ch, "--snr-db", "10", "--lora-sf", "7"]
                               + ROLE_OVERRIDE.get(e, ["--steps", "2"])))

    if a.full:
        for r in ("loopback", "chain", "gossip"):
            for ch in ("ideal", "usrp", "lora"):
                checks.append(("roles x PHYs", f"{r} over {ch}",
                               ["--algo", "dl", "--role", r, "--agents", "3", "--relays", "1",
                                "--steps", "2", "--channel", ch, "--snr-db", "10", "--lora-sf", "7"]))
        for t in ("ring", "full", "0-1,1-2,2-3"):
            checks.append(("topologies", t,
                           ["--algo", "dl", "--role", "gossip", "--agents", "4",
                            "--steps", "2", "--topology", t]))

    print(f"\n  UnionLabs selftest — {len(checks)} checks, no hardware required\n")
    failures, skipped_deps, group = [], [], None
    for g, label, args in checks:
        if g != group:
            group = g
            print(f"  {DIM}{g}{OFF}")
        print(f"    {label:<26} ", end="", flush=True)
        ok, dt, out = run(args)
        if ok:
            print(f"{GREEN}pass{OFF} {DIM}{dt:5.1f}s{OFF}")
        else:
            dep = missing_dependency(out)
            if dep:
                print(f"{YEL}skip{OFF} {DIM}{dt:5.1f}s  {dep}{OFF}")
                skipped_deps.append((label, dep))
            else:
                print(f"{RED}FAIL{OFF} {DIM}{dt:5.1f}s{OFF}  {why(out)}")
                failures.append((label, why(out)))

    # Do the CLI flags still reach what they configure? Two have already shipped broken
    # in exactly that way (--fec never reached the radio; --snr-db did nothing on
    # hardware), and neither is visible from a run that otherwise "works".
    print(f"  {DIM}flag paths{OFF}")
    print(f"    {'CLI flags reach their target':<26} ", end="", flush=True)
    t0 = time.time()
    fp = subprocess.run([sys.executable, os.path.join(HERE, "test_flags.py")],
                        cwd=REPO, capture_output=True, text=True,
                        env=dict(os.environ,
                                 PYTHONPATH=os.path.join(REPO, "drivers", "usrp", "bindings")))
    dt = time.time() - t0
    if fp.returncode == 0:
        m = re.search(r"(\d+) flag paths checked", fp.stdout)
        print(f"{GREEN}pass{OFF} {DIM}{dt:5.1f}s  ({m.group(1) if m else '?'} paths){OFF}")
    else:
        print(f"{RED}FAIL{OFF} {DIM}{dt:5.1f}s{OFF}")
        for line in fp.stdout.splitlines():
            if "FAIL" in line:
                print(f"      {line.strip()}")
        failures.append(("flag paths", "a CLI flag no longer reaches what it configures"))

    print(f"\n  {DIM}skipped (needs hardware):{OFF} LoRa serial/spi backends · USRP radio "
          f"backend · the two-host tx/rx/relay/peer roles")

    n = len(checks) + 1        # + the flag-path check
    if skipped_deps:
        print(f"\n  {YEL}{len(skipped_deps)} skipped{OFF} — an optional dependency is not installed:")
        for label, dep in skipped_deps:
            print(f"    {label}: {dep}")
        print(f"    {DIM}pip install -r requirements.txt installs all of them{OFF}")
    if failures:
        print(f"\n  {RED}{len(failures)} of {n} checks FAILED{OFF}")
        for label, msg in failures:
            print(f"    {label}: {msg}")
        print(f"\n  If the usrp checks are the only failures, the pyphy extension is probably")
        print(f"  missing for your Python — see drivers/usrp/bindings/build.sh, or just use")
        print(f"  --channel ideal / --channel lora, which need nothing.\n")
        return 1
    passed = n - len(skipped_deps)
    print(f"\n  {GREEN}all {passed} runnable checks passed{OFF} — your installation works"
          + (f" ({len(skipped_deps)} skipped for missing optional deps).\n" if skipped_deps else ".\n"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
