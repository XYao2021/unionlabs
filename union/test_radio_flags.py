#!/usr/bin/env python3
"""Does a flag passed to radio.sh actually REACH the modem, and win?

radio.sh emits a tuned default for a dozen modem options. The modem rejects a repeated
option ("--tx-ant cannot be specified more than once"), so a caller's flag must REPLACE
the wrapper's default, never follow it — otherwise the most useful options are the
unreachable ones, which is exactly how this started.

This walks every option the wrapper emits, in both roles, overrides it, and asserts the
composed command contains that option exactly ONCE with the caller's value. Companion to
test_flags.py, which does the same job for run.sh.
"""
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RADIO = os.path.join(REPO, "radio.sh")
SENTINEL = "SENTINEL_VALUE"


def compose(args):
    """The command radio.sh would run (--dry-run needs no radio and no binary)."""
    p = subprocess.run([RADIO] + args + ["--dry-run"], cwd=REPO,
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"radio.sh {' '.join(args)} failed:\n{p.stderr or p.stdout}")
    line = [l for l in p.stdout.splitlines() if l.startswith(">> ")]
    if not line:
        raise SystemExit(f"no command line from radio.sh {' '.join(args)}")
    return line[0][3:].split()


def emitted_options(tokens):
    return [t for t in tokens if t.startswith("--")]


def value_after(tokens, opt):
    i = tokens.index(opt)
    return tokens[i + 1] if i + 1 < len(tokens) else None


def main():
    checked = failures = 0
    for role in ("tx", "rx"):
        base = compose([role, "--device", "b210"])
        for opt in emitted_options(base):
            got = compose([role, "--device", "b210", opt, SENTINEL])
            n = got.count(opt)
            v = value_after(got, opt)
            checked += 1
            if n != 1 or v != SENTINEL:
                failures += 1
                print(f"  FAIL {role} {opt}: occurrences={n} value={v!r}")

        # everything at once — the case where a naive filter would leave a duplicate
        overrides, expect = [], {}
        for i, opt in enumerate(emitted_options(base)):
            val = f"{SENTINEL}{i}"
            overrides += [opt, val]
            expect[opt] = val
        got = compose([role, "--device", "b210"] + overrides)
        dups = {o for o in emitted_options(got) if got.count(o) > 1}
        checked += 1
        if dups:
            failures += 1
            print(f"  FAIL {role} all-at-once: duplicated {sorted(dups)}")
        for opt, val in expect.items():
            if value_after(got, opt) != val:
                failures += 1
                print(f"  FAIL {role} all-at-once: {opt} = {value_after(got, opt)!r}, want {val!r}")

    # a raw modem flag must beat the wrapper's own shorthand for the same thing
    for shorthand, raw, opt in (("--gain", "--tx-gain", "--tx-gain"),
                                ("--freq", "--tx-freq", "--tx-freq")):
        got = compose(["tx", "--device", "b210", shorthand, "11", raw, "99"])
        checked += 1
        if got.count(opt) != 1 or value_after(got, opt) != "99":
            failures += 1
            print(f"  FAIL precedence {shorthand} vs {raw}: "
                  f"{opt}={value_after(got, opt)!r} x{got.count(opt)}")

    print(f"{checked} radio.sh override paths checked, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
