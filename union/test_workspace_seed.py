#!/usr/bin/env python3
"""Does init-workspace.sh seed, refresh, and protect the shared workspace right?

/workspace is persistent and wins over the repo at run time, so a stale seed there
outlives a newer image unless init-workspace refreshes it. That refresh must never
clobber an edit, and — the bug that shipped — the script must not abort under
`set -e` on the ordinary FORCE-unset session-start path. All of that is invisible
to a run that "works", and it runs only in a session, so exercise it here with a
temp ROOT and no /workspace.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INIT = os.path.join(REPO, "deploy", "workspace", "init-workspace.sh")
# A seeded file we can perturb. It used to be a shipped TOPOLOGY, but topologies/
# and searching/ now start empty but for their README -- a fresh session should hold
# only what the account made, and seeding the examples also put every name in two
# places at once, which the lister reported as "SHADOWED, not read". The seed/refresh/
# edit-protect machinery is unchanged and still applies to what IS seeded, so the
# subject moves to one of those.
SEEDED = os.path.join("settings", "link.template.json")


def run(root, force=False):
    env = dict(os.environ, ROOT=root)
    env["FORCE"] = "1" if force else "0"
    return subprocess.run(["bash", INIT], capture_output=True, text=True, env=env)


def marker(path):
    """A value we can perturb and compare. Read generically rather than through one
    schema's field names, so the subject can move again without rewriting this."""
    return json.load(open(path)).get("_seed_marker", "<image>")


def set_marker(path, value):
    d = json.load(open(path))
    d["_seed_marker"] = value
    json.dump(d, open(path, "w"))


def main():
    checked = failures = 0

    def check(label, got, want):
        nonlocal checked, failures
        checked += 1
        if got != want:
            failures += 1
            print(f"  FAIL {label}: got={got!r} want={want!r}")

    with tempfile.TemporaryDirectory() as root:
        dst = os.path.join(root, SEEDED)

        # 1 · first seed: exit 0, file present, manifest written
        r = run(root)
        check("first seed exit 0", r.returncode, 0)
        check("a shipped template is seeded", os.path.exists(dst), True)
        # ...and the two folders the account owns start EMPTY but for their README
        for d in ("topologies", "searching"):
            check(f"{d}/ starts with only its README",
                  sorted(os.listdir(os.path.join(root, d))), ["README.md"])
        check("manifest written", os.path.exists(os.path.join(root, ".seed-manifest")), True)
        repo_marker = marker(os.path.join(REPO, "deploy", "workspace", SEEDED))

        # 2 · the ordinary session-start path (FORCE unset) must NOT abort
        r = run(root, force=False)
        check("FORCE-unset exit 0 (the set -e trap)", r.returncode, 0)

        # 3 · a STALE seed with NO manifest (legacy) -> refreshed, old kept as .local
        set_marker(dst, "STALE")
        os.remove(os.path.join(root, ".seed-manifest"))
        r = run(root, force=True)
        check("legacy reseed exit 0", r.returncode, 0)
        check("stale refreshed to repo", marker(dst), repo_marker)
        check("stale saved as .local", os.path.exists(dst + ".local"), True)

        # 4 · with a manifest, a USER EDIT is protected in place, image dropped as .repo
        set_marker(dst, "MINE")
        r = run(root, force=True)
        check("edit-protect exit 0", r.returncode, 0)
        check("user edit kept in place", marker(dst), "MINE")
        check("image version at .repo", os.path.exists(dst + ".repo"), True)
        if os.path.exists(dst + ".repo"):
            check("the .repo is the image's", marker(dst + ".repo"), repo_marker)

        # 5 · a file the user did NOT touch, unchanged since seed -> stays current, no churn
        r = run(root, force=True)
        check("no-op reseed exit 0", r.returncode, 0)

    if failures:
        print(f"  {failures} of {checked} workspace-seed paths FAILED")
        return 1
    print(f"  {checked} workspace-seed paths checked — seeds, refreshes stale, "
          f"protects edits, never aborts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
