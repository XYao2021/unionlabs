#!/usr/bin/env python3
"""What the BUILT modem actually accepts, as against what this checkout describes.

Python reaches a node by `git pull`; a new C++ option reaches it only when something
recompiles. So a tree that knows about --quiet-phy can be driving a binary that does
not, and Boost program_options rejects an unknown option by refusing the whole run:

    Error: unrecognised option '--quiet-phy'

For a setting that carries the experiment -- a carrier, a gain, a detector threshold --
that refusal is right: a value that cannot arrive must not look applied. For a
CONVENIENCE it is not. Losing a link because the file asked for quieter logs trades a
cosmetic preference for the whole run, and the error names an option the operator never
typed, so it reads as a broken install rather than a version skew.

This answers "does the binary on THIS box know that option", so a convenience can be
dropped with a word about why. It is deliberately not a general filter: an unknown
option that someone typed should still fail loudly, because that is a typo or a wrong
assumption and both want finding.
"""
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
_CACHE = {}


def binary():
    """The modem radio.sh would run, resolved the same way it resolves it."""
    for p in (os.path.join(REPO, "drivers", "usrp", "build", "sdr_system"),
              os.path.join(REPO, "build", "sdr_system")):
        if os.access(p, os.X_OK):
            return p
    return None


def supports(flag, bin_path=None):
    """Does the built modem accept `flag` (with or without the leading --)?

    Unknown means True: when the binary cannot be found or will not answer -- a
    checkout with nothing built, a --dry-run on a laptop -- the honest answer is that
    we do not know, and silently dropping a requested option on a guess would be worse
    than letting the modem speak for itself.
    """
    name = "--" + flag.lstrip("-").replace("_", "-")
    b = bin_path or binary()
    if b is None:
        return True
    if b not in _CACHE:
        try:
            p = subprocess.run([b, "--help"], capture_output=True, text=True,
                               timeout=20)
            _CACHE[b] = (p.stdout or "") + (p.stderr or "")
        except Exception:
            _CACHE[b] = None
    text = _CACHE[b]
    if not text:
        return True
    # the alias too: the modem spells some options both ways (--det-mult is an alias
    # for --IIR_threshold_multiplier), and --help lists each spelling it answers to
    return name in text or name.replace("-", "_") in text
