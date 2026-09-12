#!/usr/bin/env python3
"""Does having SURVEYED a radio break the radio-free runs?

It did. prepare.sh publishes det_mult and sync_threshold, and those reach the modem
through --usrp-set. apply_profile injected them whenever a profile matched, including
for `--channel usrp` with the default in-process pyphy backend -- which does not start
the C++ modem process and therefore REFUSES --usrp-set outright, rather than silently
ignoring a setting that cannot arrive.

So the machine that had done the most setup was the one where three radio-free checks
stopped working, and because the refusal message names pyphy, it read as the compiled
extension being broken. It was not; it imported fine. The remedy the output suggested
-- rebuild the extension -- could not have worked, and rebuilding changed nothing.

A surveyed value must only be injected where it can actually be used. A value the
experimenter TYPES still gets the hard refusal: that one is a request that cannot be
honoured, and must not look applied.
"""
import os
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "union"))
import run_algo  # noqa: E402

VALS = {"freq": 916.0, "det_mult": 5.0, "sync_threshold": 15.0}


class _AP:
    """Stands in for the parser: nothing was typed, so every default is None."""
    def get_default(self, dest):
        return None


def _args(backend, typed=None, vals=None):
    """An `a` with only what apply_phy_profile reads, and a fake phy_profile that
    hands back a survey without needing one on disk."""
    fake = types.ModuleType("phy_profile")
    fake.load = lambda *args, **kw: (dict(VALS if vals is None else vals),
                                     "/tmp/phy-TEST.json", "matched TEST")
    sys.modules["phy_profile"] = fake
    return types.SimpleNamespace(
        no_phy_profile=False, usrp_backend=backend, usrp_set=list(typed or []),
        freq=None, role="rx", tx_ant=None, rx_ant=None, tx_subdev=None,
        rx_subdev=None, tx_args=None, rx_args=None,
        phy_profile_node=None, phy_profile_band=None,
        _typed=set(("usrp_set",) if typed else ()))


def main():
    checked = failures = 0

    def check(label, got, want):
        nonlocal checked, failures
        checked += 1
        if got != want:
            failures += 1
            print(f"  FAIL {label}: got={got!r} want={want!r}")

    # 1 | the in-process backend: surveyed values must NOT be injected, because it
    #     refuses --usrp-set and the whole run dies
    a = _args("pyphy")
    run_algo.apply_phy_profile(_AP(), a)
    check("pyphy: no det_mult injected", any("det_mult" in s for s in a.usrp_set), False)
    check("pyphy: no sync_threshold injected",
          any("sync_threshold" in s for s in a.usrp_set), False)
    check("pyphy: usrp_set stays empty", a.usrp_set, [])
    check("pyphy: carrier still adopted", a.freq, 916.0)   # freq suits either backend

    # 2 | the real-radio backend starts the modem process, so they SHOULD be injected
    a = _args("radio")
    run_algo.apply_phy_profile(_AP(), a)
    check("radio: det_mult injected", "det_mult=5.0" in a.usrp_set, True)
    check("radio: sync_threshold injected", "sync_threshold=15.0" in a.usrp_set, True)
    check("radio: carrier adopted", a.freq, 916.0)

    # 3 | a typed value still wins, and the profile does not add a second one
    a = _args("radio", typed=["det_mult=99"])
    run_algo.apply_phy_profile(_AP(), a)
    check("typed det_mult kept", "det_mult=99" in a.usrp_set, True)
    check("profile did not duplicate it",
          sum(1 for s in a.usrp_set if s.startswith("det_mult=")), 1)

    # 4 | a profile with no thresholds injects nothing
    a = _args("radio", vals={"freq": 916.0})
    run_algo.apply_phy_profile(_AP(), a)
    check("no thresholds -> no usrp_set", a.usrp_set, [])

    if failures:
        print(f"  {failures} of {checked} profile/backend paths FAILED")
        return 1
    print(f"  {checked} profile/backend paths checked - surveyed values reach the "
          f"modem backend only, never the in-process one")
    return 0


if __name__ == "__main__":
    sys.exit(main())
