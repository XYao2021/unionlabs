#!/usr/bin/env python3
"""Does a topology reproduce the hand-typed radio.sh command, flag for flag?

TWO DIFFERENT THINGS run a radio here, and conflating them cost a bring-up session.

  ./run.sh --algo echo --topology X --node snk
      An ALGORITHM over the radio. phy_link.RadioRoundTrip is request-over-air,
      reply-over-TCP by construction: the source sends the payload over the air and
      dials net_host to read the answer, the sink answers with _tcp_serve_once. That
      socket is structural, and ack_wireless does not move it -- ack_wireless chooses
      how the ARQ LINK-LAYER ack travels, a different acknowledgement entirely. Point
      it at two machines with no route between them and it fails with
      "reply server 127.0.0.1:5700 never came up", which says nothing about radios.

  ./run.sh radio --topology X --node snk
      The C++ modem ALONE, moving its own message, acknowledging over RF or TCP, with
      no Python in the path and nothing for a reply leg to carry. This is what the
      commands in COMMANDS_RUN actually are.

The second is what this checks, against the real commands as typed. Flag ORDER is not
compared -- the modem does not care and neither should a test -- but every flag and
value must be identical, because the point of emitting the command is that it can be
read beside a hand-typed one and trusted to be the same run.
"""
import json
import os
import shlex
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# the two commands from COMMANDS_RUN, verbatim
HAND_TYPED = {
    "snk": """rx --device x310
      --args addr=192.168.40.2 --rx-args addr=192.168.40.2 --tx-args addr=192.168.40.2
      --rx-subdev A:0 --rx-ant RX2   --rx-freq 2400e6 --rx-gain 20
      --tx-subdev A:0 --tx-ant TX/RX --tx-freq 2450e6 --tx-gain 25
      --role sink_arq --ack-transport rf
      --det-mult 30 --sync-threshold 10 --scheme QPSK --bytes-length 1000""",
    "src": """tx --device x310
      --args addr=192.168.30.2 --tx-args addr=192.168.30.2 --rx-args addr=192.168.30.2
      --tx-subdev A:0 --tx-ant TX/RX --tx-freq 2400e6 --tx-gain 25
      --rx-subdev A:0 --rx-ant RX2   --rx-freq 2450e6 --rx-gain 20
      --role source_arq --ack-transport rf --max-attempts 0
      --det-mult 30 --sync-threshold 10 --scheme QPSK --bytes-length 1000""",
}

TOPOLOGY = """// the two hand-typed commands, written down once
{
  "schema": 1, "name": "x310-rf", "algo": "echo",
  "defaults": {
    "channel": "usrp", "scheme": "QPSK", "bytes_length": 1000,
    "det_mult": 30, "sync_threshold": 10, "max_attempts": 0,
    "ack_wireless": true
  },
  "nodes": [
    { "id": "src", "role": "tx",
      "radio": { "device": "x310", "addr": "192.168.30.2",
        "tx": { "ant": "TX/RX", "subdev": "A:0", "gain": 25, "freq_mhz": 2400 },
        "rx": { "ant": "RX2",   "subdev": "A:0", "gain": 20, "freq_mhz": 2450 } } },
    { "id": "snk", "role": "rx",
      "radio": { "device": "x310", "addr": "192.168.40.2",
        "rx": { "ant": "RX2",   "subdev": "A:0", "gain": 20, "freq_mhz": 2400 },
        "tx": { "ant": "TX/RX", "subdev": "A:0", "gain": 25, "freq_mhz": 2450 } } }
  ],
  "links": [ { "from": "src", "to": "snk",
               "medium": { "up": "wireless", "down": "wireless" } } ]
}
"""


def flags(tokens):
    """-> (mode, {flag: value}). A flag with no value is True."""
    mode, out, i = tokens[0], {}, 1
    while i < len(tokens):
        if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
            out[tokens[i]] = tokens[i + 1]
            i += 2
        else:
            out[tokens[i]] = True
            i += 1
    return mode, out


def emit(tdir, node, *extra):
    """--dry-run, because this subcommand RUNS by default like every other one here.
    It still prints the radio.sh command it is about to exec, which is the line
    compared below -- and it must survive being piped, which means flushing before
    execv replaces the process."""
    env = dict(os.environ, UNION_TOPOLOGY_DIR=tdir)
    env.pop("UNION_SETTINGS_DIR", None)
    r = subprocess.run([os.path.join(REPO, "run.sh"), "radio",
                        "--topology", "x310-rf", "--node", node, "--dry-run", *extra],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    out = (r.stdout or "").strip()
    # the radio.sh line specifically, not "the first line": stdout also carries the
    # modem line radio.sh prints under its own --dry-run
    cmd = next((l for l in out.splitlines() if "radio.sh" in l), "")
    return r.returncode, cmd, (r.stderr or ""), out


def main():
    checked = failures = 0

    def check(label, got, want):
        nonlocal checked, failures
        checked += 1
        if got != want:
            failures += 1
            print(f"  FAIL {label}: got={got!r} want={want!r}")

    tmp = tempfile.mkdtemp(prefix="union-radiocmd-")
    try:
        with open(os.path.join(tmp, "x310-rf.jsonc"), "w") as fh:
            fh.write(TOPOLOGY)

        for node, want in HAND_TYPED.items():
            code, line, _, _ = emit(tmp, node)
            check(f"{node}: command emitted", code, 0)
            # the script path contains spaces on some machines, so split on the name
            rest = line.split("radio.sh", 1)[1] if "radio.sh" in line else ""
            gm, g = flags(shlex.split(rest)) if rest else ("", {})
            wm, w = flags(shlex.split(want))
            check(f"{node}: radio.sh mode", gm, wm)
            check(f"{node}: every flag and value", g, w)

        # the ACK transport follows the one switch, and with TCP the ACK socket
        # appears while the node's reverse RF path does not
        tcp = TOPOLOGY.replace('"ack_wireless": true', '"ack_wireless": false')
        with open(os.path.join(tmp, "x310-rf.jsonc"), "w") as fh:
            fh.write(tcp)
        _, line, _, _ = emit(tmp, "src")
        _, g = flags(shlex.split(line.split("radio.sh", 1)[1]))
        check("tcp ack: transport", g.get("--ack-transport"), "tcp")
        check("tcp ack: the sink's port is dialled", g.get("--ack-port"), "5599")
        check("tcp ack: no reverse RF carrier is tuned", "--rx-freq" in g, False)

        # RUNNING IS THE DEFAULT. Every other run.sh subcommand runs, and --dry-run
        # is how this project asks for a preview, so a mode that only ever printed was
        # the odd one out. The command is printed either way, so a session log says
        # what it did -- and that print has to survive a pipe, which it did not until
        # stdout was flushed before execv.
        env = dict(os.environ, UNION_TOPOLOGY_DIR=tmp)
        env.pop("UNION_SETTINGS_DIR", None)
        r = subprocess.run([os.path.join(REPO, "run.sh"), "radio", "--topology",
                            "x310-rf", "--node", "snk", "--dry-run"],
                           cwd=REPO, env=env, capture_output=True, text=True,
                           timeout=120)
        lines = (r.stdout or "").strip().splitlines()
        check("the command is printed when piped", bool(lines), True)
        check("...and stdout's first line IS the command, not a warning",
              "radio.sh" in (lines[0] if lines else ""), True)
        check("...followed by the modem line radio.sh resolves",
              any("sdr_system" in l for l in lines[1:]), True)
        # the first version used --run for this; a command in someone's notes should
        # not start failing
        r2 = subprocess.run([os.path.join(REPO, "run.sh"), "radio", "--topology",
                             "x310-rf", "--node", "snk", "--run", "--dry-run"],
                            cwd=REPO, env=env, capture_output=True, text=True,
                            timeout=120)
        check("--run is still accepted", r2.returncode, 0)

        # ── a node's own receive gates beat the experiment-wide ones ─────────
        # With a wireless ACK both nodes receive, but not the same thing: one takes a
        # long data burst, the other a short ACK, at different carriers with different
        # noise floors. One experiment-wide det_mult cannot fit both -- tuned for the
        # data, the ACK receiver sleeps through its burst, and the log says only
        # "TIMEOUT on chunk 1" with nothing about why.
        pn = dict(json.loads(TOPOLOGY.split("\n", 1)[1]))
        pn["defaults"]["det_mult"] = 500
        pn["defaults"]["sync_threshold"] = 15
        for node in pn["nodes"]:
            if node["id"] == "src":                 # its rx block IS the ACK receiver
                node["radio"]["rx"]["det_mult"] = 30
                node["radio"]["rx"]["sync_threshold"] = 12
        with open(os.path.join(tmp, "x310-rf.jsonc"), "w") as fh:
            json.dump(pn, fh)

        def gates(node):
            _, line, _, _ = emit(tmp, node)
            f = flags(shlex.split(line.split("radio.sh", 1)[1]))[1]
            return f.get("--det-mult"), f.get("--sync-threshold")

        check("the data receiver keeps the file's gate", gates("snk"), ("500", "15"))
        check("the ACK receiver uses its own", gates("src"), ("30", "12"))
        # exactly one of each reaches the modem: the override has to REPLACE, not
        # append, or program_options rejects the repeated option outright
        _, line, _, _ = emit(tmp, "src")
        check("det-mult is passed once", line.count("--det-mult"), 1)
        check("sync-threshold is passed once", line.count("--sync-threshold"), 1)
        _, _, err, _ = emit(tmp, "src")
        check("...and the override is announced", "overriding the file's 500" in err,
              True)

        # ── the option NAMES are not guessable, so they are checked ──────────
        # The C++ spellings are inconsistent with each other: --fec-type is
        # hyphenated, --fec_soft is not. Hand-writing them here produced --fec-soft,
        # which Boost rejected by refusing the whole run -- on hardware, after a
        # survey. sdr.py is generated from `sdr_system --help`, so it is the authority.
        import sys as _sys
        _sys.path.insert(0, os.path.join(REPO, "drivers", "usrp", "python"))
        import sdr as _sdr
        import topology_radio as _tr

        for fec, want in (("turbo", ["--fec", "true", "--fec-type", "turbo",
                                     "--fec_soft", "true"]),
                          ("ldpc", ["--fec", "true", "--fec-type", "ldpc",
                                    "--fec_soft", "true"]),
                          ("conv", ["--fec", "true", "--fec-type", "conv"]),
                          ("", ["--fec", "false"])):
            check(f"fec {fec!r} emits the modem's own spelling",
                  _tr._fec({"fec": fec}), want)
        check("a file that says nothing about fec passes nothing",
              _tr._fec({}), [])
        # every flag emitted must EXIST in the registry, radio.sh's own excepted
        check("the registry knows every modem flag we emit",
              _tr.check_flags(["--fec-type", "turbo", "--fec_soft", "true",
                               "--det-mult", "30", "--sync-threshold", "15",
                               "--bytes-length", "1000", "--quiet-phy", "true",
                               "--role", "rx", "--ack-transport", "rf"]), [])
        check("...and radio.sh's own flags are not checked against it",
              _tr.check_flags(["--device", "x310", "--rate", "2e6", "--sym", "1e6"]), [])
        check("an invented flag is caught",
              _tr.check_flags(["--fec-soft", "true"]), ["--fec-soft"])
        # and the registry really does spell it that way, so this is not two copies of
        # the same guess agreeing with each other
        check("the registry spells it fec_soft", "fec_soft" in _sdr.OPTIONS, True)
        check("...and not fec-soft", "fec-soft" in _sdr.OPTIONS, False)

        # a candidate list cannot be resolved by a bare modem: there is no survey
        # resolver in this path, and silently taking the first entry would put the
        # link on a carrier nobody chose
        cand = TOPOLOGY.replace('"freq_mhz": 2400 }',
                                '"freq_mhz": [2400, 2410] }')
        with open(os.path.join(tmp, "x310-rf.jsonc"), "w") as fh:
            fh.write(cand)
        code, _, err, _ = emit(tmp, "src")
        check("a candidate list is refused here", code, 1)
        check("...and says which mode does resolve them",
              "--algo" in err, True)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f"  {failures} of {checked} radio-command paths FAILED")
        return 1
    print(f"  {checked} radio-command paths checked — a topology emits the "
          f"hand-typed radio.sh command, flag for flag")
    return 0


if __name__ == "__main__":
    sys.exit(main())
