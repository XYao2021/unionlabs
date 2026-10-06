#!/usr/bin/env python3
"""Does each container start only the topology nodes whose RADIOS it holds?

/workspace is shared across containers on different machines, so every one of them
reads the SAME topology file. What distinguishes them is the hardware each can see --
not the hostname, which is a fresh pod id every session, and not the `host` field,
which would have to be edited per container and so defeats sharing the file at all.

The failure this prevents is silent and expensive: with both nodes written as
host 127.0.0.1 (the normal thing when the radios are reachable over their own
subnets), host matching makes EVERY container start EVERY node. Two transmitters and
two receivers come up, each pair fighting for one radio, and the symptom is a link
that half-works for reasons that have nothing to do with the radio.

No radio is needed to check this: a fake uhd_find_devices on PATH is exactly what the
selection reads, so the three cases that matter can be walked here.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fake_uhd(dirpath, *devices):
    """A uhd_find_devices that reports exactly the radios we say it does."""
    os.makedirs(dirpath, exist_ok=True)
    lines = ['#!/bin/sh']
    for serial, addr in devices:
        lines.append(f'echo "    serial: {serial}"')
        if addr:
            lines.append(f'echo "    addr: {addr}"')
    p = os.path.join(dirpath, "uhd_find_devices")
    with open(p, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(p, 0o755)
    return dirpath


def run_topology(topo, binpath, extra=()):
    env = dict(os.environ)
    env["PATH"] = binpath + os.pathsep + env.get("PATH", "")
    env.pop("UNION_TOPOLOGY_DIR", None)
    r = subprocess.run([os.path.join(REPO, "run.sh"), "topology", topo,
                        "--dry-run", *extra],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=180)
    return (r.stdout or "") + (r.stderr or "")


def started(out):
    m = re.search(r"starting \d+ node\(s\) of \S+ here: (.+)", out)
    return sorted(x.strip() for x in m.group(1).split(",")) if m else []


def main():
    checked = failures = 0

    def check(label, got, want):
        nonlocal checked, failures
        checked += 1
        if got != want:
            failures += 1
            print(f"  FAIL {label}: got={got!r} want={want!r}")

    tmp = tempfile.mkdtemp(prefix="union-own-")
    try:
        # echo-pair-x310 names its radios by ADDRESS and puts both nodes on
        # host 127.0.0.1 — the exact shape that host matching gets wrong.
        tx_box = fake_uhd(os.path.join(tmp, "tx"), ("F5B2C30", "192.168.30.2"))
        rx_box = fake_uhd(os.path.join(tmp, "rx"), ("F5B2C40", "192.168.40.2"))
        none_box = fake_uhd(os.path.join(tmp, "none"), ("NOBODY", "10.9.9.9"))

        out = run_topology("echo-pair-x310", tx_box)
        check("holder of .30.2 starts tx only", started(out), ["tx"])
        check("...and says why rx was skipped",
              "192.168.40.2 is not attached here" in out, True)

        out = run_topology("echo-pair-x310", rx_box)
        check("holder of .40.2 starts rx only", started(out), ["rx"])
        check("...and says why tx was skipped",
              "192.168.30.2 is not attached here" in out, True)

        # a container holding NEITHER radio must refuse, and name what it saw against
        # what the file wanted — "not this machine" sends the reader to the wrong layer
        out = run_topology("echo-pair-x310", none_box)
        check("holder of neither starts nothing", started(out), [])
        check("...names the radios it can see", "10.9.9.9" in out, True)
        check("...and the radios the file wants",
              "192.168.30.2" in out and "192.168.40.2" in out, True)

        # --all is still the escape hatch for one machine holding everything
        out = run_topology("echo-pair-x310", none_box, extra=("--all",))
        check("--all overrides ownership", started(out), ["rx", "tx"])

        # BY SERIAL, not just by address. A B210 is USB-attached and has no IP at all,
        # so a rig of two B210s can ONLY be told apart by serial — and that is the more
        # common case, not the exception. echo-pair-radio names serial=30CD424 and
        # serial=30CD3F7; a fake device with a serial and no addr is exactly a B210.
        b_tx = fake_uhd(os.path.join(tmp, "b210tx"), ("30CD424", None))
        b_rx = fake_uhd(os.path.join(tmp, "b210rx"), ("30CD3F7", None))
        out = run_topology("echo-pair-radio", b_tx)
        check("B210 serial 30CD424 starts tx only", started(out), ["tx"])
        check("...naming the serial it lacks", "30CD3F7 is not attached here" in out, True)
        out = run_topology("echo-pair-radio", b_rx)
        check("B210 serial 30CD3F7 starts rx only", started(out), ["rx"])
        check("...naming the serial it lacks", "30CD424 is not attached here" in out, True)
        # a box holding BOTH B210s legitimately owns both nodes (the single-bench case)
        b_both = fake_uhd(os.path.join(tmp, "b210both"),
                          ("30CD424", None), ("30CD3F7", None))
        out = run_topology("echo-pair-radio", b_both)
        check("one bench with both B210s owns both", started(out), ["rx", "tx"])

        # AN ADDRESS CLAIM IS WARNED ABOUT, a serial claim is not. 192.168.40.2 is
        # UHD's default for an X310, so two machines each with one on its own isolated
        # subnet both answer to it: matching on an address is right on one bench and
        # ambiguous across a testbed, and nothing at run time can tell which this is.
        out = run_topology("echo-pair-x310", rx_box)
        check("address claim warns", "claimed by ADDRESS" in out, True)
        check("...and says why it matters", "unique only within one host" in out, True)
        out = run_topology("echo-pair-radio", b_tx)
        check("serial claim does not warn", "claimed by ADDRESS" in out, False)

        # a pure-TCP topology names no radio, so host matching must still decide it;
        # radio ownership must not quietly break every radio-free experiment
        out = run_topology("fl-star-tcp", none_box)
        check("pure-TCP unaffected by radios", started(out), ["c0", "c1", "srv"])

        # and with NO radio visible at all (no UHD, a laptop), nothing regresses
        empty = fake_uhd(os.path.join(tmp, "empty"))
        out = run_topology("fl-star-tcp", empty)
        check("no radios at all -> host decides", started(out), ["c0", "c1", "srv"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f"  {failures} of {checked} ownership paths FAILED")
        return 1
    print(f"  {checked} ownership paths checked — a container starts the nodes whose "
          f"radios it holds, and only those")
    return 0


if __name__ == "__main__":
    sys.exit(main())
