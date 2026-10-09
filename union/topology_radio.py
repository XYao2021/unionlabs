#!/usr/bin/env python3
"""topology_radio.py — run a topology node as a PLAIN MODEM LINK, not an algorithm.

    ./run.sh radio --topology x310-rf --node snk            # run it
    ./run.sh radio --topology x310-rf --node snk --dry-run  # print, touch no radio

The command is always printed before it runs, so a session log says what it did.

WHY THIS EXISTS, AND WHY --algo IS NOT THE SAME THING.

`./run.sh --algo echo --topology X --node snk` runs an ALGORITHM over the radio.
phy_link.RadioRoundTrip is request-over-air, reply-over-TCP by construction: the
source sends the algorithm's payload over the air and then dials net_host to read the
answer, and the sink answers with _tcp_serve_once. That socket is not optional and
ack_wireless does not move it -- ack_wireless chooses how the ARQ LINK-LAYER ack
travels, which is a different acknowledgement entirely.

The hand-typed commands people actually bring up a link with are not that. They are

    ./radio.sh rx --device x310 --args addr=... --role sink_arq --ack-transport rf ...

-- the C++ modem on its own, moving its own message, acknowledging over RF, with no
socket and no Python in the path. There is nothing for a TCP reply leg to carry,
because there is no algorithm asking a question.

Both are worth having and neither substitutes for the other, so this translates the
topology into the second one. The topology stays the single place the rig is written
down -- carriers, connectors, gains, detector thresholds -- and this reads it and
emits the modem invocation, which is what the original "one consolidated parameter
file" was for.

WHAT MAPS TO WHAT. A node's own radio block is its two signal paths, and its role in
the link decides which is the data direction:

    the node with the OUT-link transmits data  -> radio.sh tx, --role source_arq
    the node with the IN-link receives data    -> radio.sh rx, --role sink_arq

so the data carrier is tx.freq_mhz on the source and rx.freq_mhz on the sink, and the
ACK carrier is the other one. ack_wireless picks --ack-transport; with rf the second
path is a real RF path and both of a node's blocks are emitted, with tcp the ACK rides
a socket and --ack-host/--ack-port come from the sink.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import topology as tp                                        # noqa: E402
import modem_opts                                            # noqa: E402

# defaults -> the modem option each one sets. Only what the modem itself takes: a
# topology key with no modem equivalent (steps, channel) is for the Python layer and
# has no meaning to a bare modem run.
MODEM_DEFAULTS = (
    ("scheme", "--scheme"),
    ("waveform", "--waveform"),
    ("bytes_length", "--bytes-length"),
    ("det_mult", "--det-mult"),
    ("sync_threshold", "--sync-threshold"),
    ("samp_rate", "--rate"),
    ("symbol_rate", "--sym"),
    ("quiet_phy", "--quiet-phy"),
)


def _hz(mhz):
    """MHz as the modem wants it: Hz, written <MHz>e6.

    Always e6, never e9. 2400e6 is how every worked command in COMMANDS_RUN and docs/
    writes it, so an emitted command can be read beside a hand-typed one and compared
    field by field -- which is the whole point of emitting it rather than describing
    it. 2.4e9 is the same number and defeats that.
    """
    return f"{float(mhz):g}e6"


def _fec(defaults):
    """--fec true|false plus the family, when the file names one.

    radio.sh passes --fec true and leaves the family to the modem's own default of
    conv, while run.sh defaults to turbo -- two defaults for a setting the modem says
    must MATCH on both ends. A topology that states fec is therefore stated here in
    full rather than half-passed.
    """
    if "fec" not in defaults:
        # The file does not say, so do not decide for it: radio.sh applies its own
        # default and the emitted command stays comparable to a hand-typed one. Stating
        # it in the topology is how you pin it, and then it is passed in full.
        return []
    fec = defaults["fec"]
    if fec in ("", None, False):
        return ["--fec", "false"]
    out = ["--fec", "true"]
    if isinstance(fec, str) and fec not in ("true", "1"):
        out += ["--fec-type", fec]
        if fec in ("ldpc", "turbo"):
            # UNDERSCORE. The modem's registry spells it fec_soft, while fec-type is
            # hyphenated -- the C++ option names are not consistent with each other,
            # so they are not guessable and must be read off the registry.
            out += ["--fec_soft", "true"]
    return out


# radio.sh's own options, read off its argument loop. It consumes these and emits
# the modem's equivalents itself, so they are not modem option names and must not be
# checked against the modem's registry.
RADIO_SH_OWN = frozenset((
    "--device", "--args", "--addr", "--serial", "--freq", "--scheme", "--waveform",
    "--gain", "--rate", "--sym", "--fec", "--ant", "--subdev", "--dry-run",
    "--no-profile", "--phy-node",
))


def check_flags(cmd):
    """Every modem option this emits must EXIST in the modem's registry.

    sdr.py is generated from `sdr_system --help`, so it is the authority on what the
    binary accepts -- and the C++ names are not guessable from each other: --fec-type
    is hyphenated while --fec_soft is not. Hand-writing them here produced
    --fec-soft, which Boost rejected by refusing the whole run, on hardware, after a
    survey. Checked at emit time so a wrong spelling fails in the test suite instead.

    Silent when the registry cannot be imported: a checkout without the driver tree is
    not evidence that a flag is wrong.
    """
    try:
        sys.path.insert(0, os.path.join(REPO, "drivers", "usrp", "python"))
        import sdr
    except Exception:
        return []
    bad = []
    for tok in cmd:
        if not tok.startswith("--") or tok in RADIO_SH_OWN:
            continue
        if tok[2:] not in sdr.OPTIONS:
            bad.append(tok)
    return bad


def command(topo, node_id):
    """-> (argv for radio.sh, note lines). Raises TopologyError on a node that cannot."""
    nd = topo.node(node_id)
    if not nd.radio:
        raise tp.TopologyError(
            f"node {nd.id} has no radio block, so there is no modem to run. This mode "
            f"drives the C++ modem directly; a node without a radio only means "
            f"anything to the Python layer (./run.sh --algo ... --topology ...).")

    out_links = [ln for ln in topo.links_of(nd) if ln.a.id == nd.id]
    in_links = [ln for ln in topo.links_of(nd) if ln.b.id == nd.id]
    if not out_links and not in_links:
        raise tp.TopologyError(f"node {nd.id} is in no link, so it has no peer to "
                               f"transmit to or receive from")
    sends_data = bool(out_links)
    link = (out_links or in_links)[0]
    rf_ack = link.down == "wireless"

    data_side, ack_side = ("tx", "rx") if sends_data else ("rx", "tx")
    data = nd.radio.get(data_side) or {}
    ack = nd.radio.get(ack_side) or {}
    if not data:
        raise tp.TopologyError(
            f"node {nd.id} {'transmits' if sends_data else 'receives'} the data, so it "
            f"needs a radio.{data_side} block and has none")

    args = nd.radio["args"]
    notes = []
    cmd = ["tx" if sends_data else "rx", "--device", str(nd.radio.get("device") or ""),
           "--args", args]

    # BOTH --tx-args and --rx-args, always the same radio: one box, one USRP, two
    # signal paths. That is what the modem means by full duplex, and it is what the
    # worked commands pass.
    cmd += ["--tx-args", args, "--rx-args", args]

    for side, blk in (("tx", nd.radio.get("tx") or {}), ("rx", nd.radio.get("rx") or {})):
        if not blk:
            continue
        if side == ack_side and not rf_ack:
            notes.append(f"radio.{side} is this node's ACK path and the ACK goes over "
                         f"TCP, so it is not emitted")
            continue
        if blk.get("subdev"):
            cmd += [f"--{side}-subdev", str(blk["subdev"])]
        if blk.get("ant"):
            cmd += [f"--{side}-ant", str(blk["ant"])]
        f = blk.get("freq_mhz")
        if f is None:
            raise tp.TopologyError(f"node {nd.id}: radio.{side}.freq_mhz is not set, so "
                                   f"there is no carrier for that direction")
        if tp.is_placeholder(f):
            raise tp.TopologyError(f"node {nd.id}: radio.{side}.freq_mhz is still "
                                   f"{f} — fill it in from the carriers the survey "
                                   f"found (the file's header lists them)")
        if isinstance(f, list):
            raise tp.TopologyError(
                f"node {nd.id}: radio.{side}.freq_mhz is a candidate list {f}. A bare "
                f"modem run has no survey resolver behind it — pick one number, or use "
                f"./run.sh --algo ... --topology ... which does resolve candidates.")
        cmd += [f"--{side}-freq", _hz(f)]
        if blk.get("gain") is not None:
            cmd += [f"--{side}-gain", f"{float(blk['gain']):g}"]

    cmd += ["--role", "source_arq" if sends_data else "sink_arq",
            "--ack-transport", "rf" if rf_ack else "tcp"]
    if not rf_ack:
        sink = link.b
        cmd += ["--ack-port", str(sink.dial_port("ack", 5599))]
        if sends_data:
            cmd += ["--ack-host", sink.dial_host() or "127.0.0.1"]
            if not sink.dial_host():
                notes.append(f"{sink.id} has no host, so the ACK socket is dialled at "
                             f"127.0.0.1 — right on one machine, wrong on two")

    d = topo.defaults
    for key, flag in MODEM_DEFAULTS:
        if key in d:
            v = d[key]
            # quiet_phy is a CONVENIENCE, and Boost refuses the whole run over an
            # option it does not know. A modem compiled before --quiet-phy existed
            # would lose the link because the file asked for quieter logs, and the
            # error would name an option nobody typed.
            if key == "quiet_phy" and not modem_opts.supports(flag):
                notes.append(
                    "this modem has no --quiet-phy, so the per-block chatter will "
                    "print. It is a C++ option: deploy/initialization.sh --only-build, or "
                    "a newer image, is what delivers it")
                continue
            if isinstance(v, bool):
                cmd += [flag, "true" if v else "false"]
            elif flag in ("--rate", "--sym") and isinstance(v, (int, float)) and v < 1e6:
                cmd += [flag, _hz(v)]
            elif isinstance(v, (int, float)):
                cmd += [flag, f"{v:g}"]
            else:
                cmd += [flag, str(v)]
    # A node's own receive-side gates override the experiment-wide ones. The DATA
    # side is what this node receives on, so that is where they belong.
    for key, flag in (("det_mult", "--det-mult"),
                      ("sync_threshold", "--sync-threshold")):
        own = (nd.radio.get(data_side if not sends_data else ack_side) or {}).get(key)
        if own is None:
            continue
        # drop the experiment-wide value, then state this node's
        if flag in cmd:
            i = cmd.index(flag)
            del cmd[i:i + 2]
        cmd += [flag, f"{float(own):g}"]
        notes.append(f"{key}={own:g} from this node's receive block, overriding the "
                     f"file's {d.get(key)}")

    cmd += _fec(d)
    bad = check_flags(cmd)
    if bad:
        raise tp.TopologyError(
            f"emitting {', '.join(bad)}, which the modem's own option registry does "
            f"not list (docs/PARAMETERS.md, generated from sdr_system --help). That is "
            f"a bug here, not in your file.")

    # ...and of the options that DO exist in this checkout, which does the binary on
    # this box actually have? Those are two different questions: Python arrives by git
    # pull and a C++ option only when something recompiles. Boost answers the second
    # one by refusing the whole run and naming a single option, so you fix that one,
    # re-run, and meet the next -- which is how one old binary costs several attempts
    # on a radio. Name them all at once, with what delivers them.
    stale = [t for t in cmd
             if t.startswith("--") and t not in RADIO_SH_OWN
             and not modem_opts.supports(t)]
    if stale:
        raise tp.TopologyError(
            f"this modem does not have {', '.join(stale)} — it was built before this "
            f"checkout. Nothing is wrong with your file; the binary is behind.\n"
            f"  cd {REPO} && ./deploy/initialization.sh --only-build\n"
            f"rebuilds it in place, or use an image built from this commit. "
            f"(./run.sh radio --dry-run shows the command without running anything.)")
    # max_attempts belongs to the SOURCE: the sink has nothing to give up on
    if sends_data and "max_attempts" in d:
        cmd += ["--max-attempts", str(int(d["max_attempts"]))]
    return cmd, notes


def main():
    ap = argparse.ArgumentParser(
        description="run one topology node as a plain modem link (radio.sh), "
                    "with no Python algorithm and no TCP reply leg")
    ap.add_argument("--topology", required=True, dest="name",
                    help="topology file name or path")
    ap.add_argument("--node", required=True, help="which node of it to run")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the radio.sh command and the modem line it resolves "
                         "to, and open no radio")
    # Accepted and ignored: running IS the default now, and this was the flag in the
    # first version. Cheaper to keep honouring it than to have a command someone has
    # in their notes start failing.
    ap.add_argument("--run", action="store_true", help=argparse.SUPPRESS)
    a, extra = ap.parse_known_args()

    try:
        topo = tp.load(a.name)
        cmd, notes = command(topo, a.node)
    except tp.TopologyError as e:
        sys.exit(f"topology: {e}")

    cmd += extra                       # anything typed still wins, as everywhere else
    for n in notes:
        print(f"[radio] note: {n}", file=sys.stderr)
    script = os.path.join(REPO, "radio.sh")
    # Printed either way. This is a translation of a file into a modem invocation, and
    # a run that does not say what it ran leaves nothing to compare against the
    # hand-typed command the file is standing in for.
    print(f"{script} " + " ".join(cmd))
    if a.dry_run:
        # radio.sh's own --dry-run prints the sdr_system line it would exec, so the
        # whole chain -- topology to radio.sh to modem -- is visible without a radio.
        cmd.append("--dry-run")
    # FLUSH FIRST. execv replaces the process image without running Python's exit
    # handlers, so anything still in a buffered stdout is simply gone -- and stdout is
    # buffered exactly when it is piped or redirected, which is when someone is
    # capturing the command to compare it against their notes.
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(script, [script] + cmd)


if __name__ == "__main__":
    sys.exit(main())
