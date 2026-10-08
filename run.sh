#!/usr/bin/env bash
# run.sh — ONE command to run an algorithm over the SDR PHY.
#
#   ./run.sh                                   # defaults: algo=echo role=loopback channel=ideal
#   ./run.sh --algo marl            # any algorithm in deploy/workspace/algorithms/
#   ./run.sh --algo marl --channel usrp --sim-snr-db 6       # over the USRP PHY
#   ./run.sh --algo fl --channel lora --lora-sf 9        # over the LoRa PHY (SX1276)
#   ./run.sh --algo clip_semcom --steps 45
#   ./run.sh --algo fl --steps 20              # federated learning on MNIST
#   ./run.sh --algo fl --role chain --relays 1 # 3 nodes: client -> relay -> server
#   # over the radio (two hosts) — start the rx FIRST:
#   ./run.sh --algo marl --role rx --rx-args addr=192.168.20.2
#   ./run.sh --algo marl --role tx --tx-args serial=30CD424 --ack-host <AP_IP>
#   ./run.sh --algo dl --role gossip --agents 6 --topology ring   # decentralized, one process
#   # ... or one terminal / one computer PER NODE (--node k implies --role peer):
#   ./run.sh --algo dl --node 0 --agents 3 --topology ring
#   ./run.sh --algo dl --node 1 --agents 3 --topology ring --radio serial=30CD424
#   ./run.sh radio tx | ./run.sh radio rx       # the raw link, no algorithm (= radio.sh)
#   ./run.sh list                              # list available algorithms + their roles
#   ./run.sh --help                            # this help + every option
#
# THE WIRING OF A WHOLE EXPERIMENT (--topology <file>). Instead of typing each node's
# radio, ports and hosts on every machine, put them in ONE file that every node reads —
# /workspace/experiments/topologies/<name>.json — and tell each node which one it is:
#   ./run.sh --algo fl --topology fl-star-tcp --node srv     # the server's machine
#   ./run.sh --algo fl --topology fl-star-tcp --node c0      # the first client's
#   ./run.sh topology fl-star-tcp              # start every node that lives on THIS box
#   ./run.sh topologies                        # list the wiring files
#   ./run.sh ports                             # what another machine must dial to reach this session
#   ./run.sh --algo fl --topology fl-star-tcp --node c0 --print-plan   # resolve, don't run
# The file says what radio each node owns, which connector (TX/RX, RX2) and RF channel
# it uses, which port it listens on, and whether each link is carried over the air or
# over TCP/IP. Anything you type still wins over it. --link tcp runs the client/server
# roles over plain TCP/IP with no radio at all, which is what fl.py's --uplink tcp does.
#
# NODE TYPES (--role). tx = transmits, rx = receives, relay = BOTH (a middle node that
# receives from upstream and re-transmits downstream), peer = BOTH at different steps
# (one node of a decentralized network, run as its own process with --node k).
# loopback/chain/gossip/multi/aircomp build every node in one process for radio-free runs.
# An algorithm can name its own roles by declaring ROLES = {"client": "tx", ...} in its
# app.py, and then you type ITS names:  ./run.sh --algo fl --role server
#
# WHICH PHY (--channel) vs HOW IT IS ATTACHED (--<phy>-backend). Two separate questions,
# answered the same way by every PHY, so an algorithm moves between them by one flag:
#   --channel ideal                                      radio-free, lossless
#   --channel usrp  --usrp-backend pyphy | radio         drivers/usrp
#   --channel lora  --lora-backend sim | serial | spi    drivers/lora
# The default backend of each PHY needs NO hardware. Real radios (usrp-backend radio,
# lora-backend serial/spi) have no peer inside one process, so they run as the two-host
# role split (--role tx / --role rx). Older spellings still work: sim=ideal, pyphy=usrp.
#
# ONE CARRIER OR TWO. --freq sets both directions, which is every link whose ACK goes
# over TCP. An RF ACK returns on its OWN frequency, so the two split:
#     --freq 915                    data and ACK on one carrier
#     --freq 915 --rx-freq 925      transmit at 915, listen for the ACK at 925
# The other end must mirror it (its --tx-freq is this one's --rx-freq) or neither hears
# anything. A topology states them per side and needs no flags. These are MHz here;
# radio.sh takes the same names in Hz.
#
# WHERE A PARAMETER COMES FROM. Three sources, and a fixed precedence so a run can
# always say why it used a number:
#
#     what you type  >  --topology FILE  >  --phy-profile (searching/)  >  built-in
#
#   1. MEASURED, from prepare.sh. The survey writes searching/phy-<radio>-<band>-
#      <subdev>-<ant>-<time>.json: carrier, rx gain, det-mult, sync-threshold. It is
#      found automatically from the radio this process owns, or pinned outright:
#        --phy-profile phy-30CD3F7-ism915-AA-RX2-2026-10-05_12-00-00.json
#        --phy-profile-node KEY / --phy-profile-band BAND   narrow the search instead
#        --no-phy-profile                                   ignore measurements
#      Naming a file PINS the run to one survey, which is what makes a reported
#      number traceable: several surveys accumulate per radio, and otherwise "which
#      measurement was this" is answered by precedence rather than by the author. A
#      name that does not resolve is a hard error, never a quiet fallback.
#
#   2. AUTHORED, in a topology file — the wiring, the roles, and each node's own
#      radio settings. This is where TX vs RX vs a both-ways node is declared:
#        { "nodes": [
#            { "id": "n0", "role": "tx",
#              "radio": { "device": "b210", "serial": "30CD424",
#                         "tx": { "ant": "TX/RX", "subdev": "A:A", "gain": 70,
#                                 "freq_mhz": 915 } } },
#            { "id": "n1", "role": "rx",
#              "radio": { "device": "b210", "serial": "30CD3F7",
#                         "rx": { "ant": "RX2", "subdev": "A:A", "gain": 40 } } },
#            { "id": "n2", "role": "relay",
#              "radio": { "device": "x310", "addr": "192.168.40.2",
#                         "tx": { "ant": "TX/RX", "subdev": "A:0", "gain": 25 },
#                         "rx": { "ant": "RX2",   "subdev": "A:0", "gain": 25 } } } ] }
#      A node carrying BOTH a tx and an rx block is the two-way case; `role` is the
#      name the ALGORITHM knows it by (tx / rx / relay / peer / server / client —
#      see ROLES in docs/HOW_TO_ADD_ALGORITHM.md), while the radio blocks are the
#      hardware. ./run.sh topologies lists them; ./run.sh topologies NAME shows one.
#
#   3. TYPED, every flag below. Anything you type wins over both files, so a file is
#      a default you can always override for one run without editing it.
#
# Each layer announces what it supplied ([phy-profile] ... / [topology] ...), because
# a default that arrives from a file without saying so is worse than no default.
#
# PHY FEATURES, printed per packet, ON BY DEFAULT:
#   [PHY-FEAT] scheme=QPSK fec=turbo syms=768 bits=600 snr_req=10.0dB
#              snr_meas=10.18dB evm=30.98% ber=0.000e+00 errs=0 crc=OK
# --no-phy-features silences them. snr_req is the knob (--sim-snr-db); snr_meas is
# the noise realisation that was actually drawn, which differs from it packet to
# packet -- quote the measured one, never the request. Available on --channel usrp
# because only there does the modem hold both the sent and the received symbols.
#
# THE C++ MODEM'S OWN LOGS ([ACQ] [FILTER] [MODULATION] [DEMODULATION] [AGC] ...)
# appear when the real modem process runs -- radio.sh, or --usrp-backend radio -- and
# do NOT appear under the default in-process pyphy backend, which calls the DSP blocks
# directly and never enters the pipeline loops those prints live in.
#   ./radio.sh rx ... --quiet-phy      silence the per-block chatter
# --quiet-phy silences CHATTER only ([FILTER]/[MODULATION]/[DEMODULATION]/[AGC]/
# [DETECTOR] block-and-FIFO lines); DIAGNOSIS always prints ([ACQ] peaks, [CRC],
# [SOURCE]/[SINK] ARQ, [USRP TX/RX] warnings, [ERROR], [BER]), because a switch that
# can hide an error is worse than a noisy log. It reaches the modem through radio.sh,
# which invokes the binary directly; it is NOT yet wired into sdr.py's option table,
# so --usrp-set quiet_phy=true does not work from here. Use [PHY-FEAT] above for the
# numbers on the radio-free path. (For the record
# there is no [SYNC] tag -- sync logs under [ACQ], [TimingRecovery], [CFO_thread],
# [PhaseEstimator] -- and demodulation is [DEMODULATION], not [DEMOD].)
#
# RADIOS. --radio names the USRP this process owns: a B210 by serial (serial=30CD424),
# an X310/N210 by address (addr=192.168.40.2); a bare serial or IP works too. Use
# --tx-args/--rx-args instead when one node has two radios.
#
# Any run_algo.py option can be given; anything you omit takes its default. The pyphy
# extension (needed for --channel pyphy and the radio roles) is wired automatically.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RUN="$HERE/union/run_algo.py"

if [ "${1:-}" = "selftest" ]; then
  # "does my installation work?" — every experiment over every PHY that needs no radio.
  shift
  exec python3 "$HERE/union/selftest.py" "$@"
fi
if [ "${1:-}" = "refresh-workspace" ]; then
  # force the persistent /workspace to re-seed from THIS checkout now, without waiting
  # for a session restart. Refreshes shipped topologies/algorithms you have not edited;
  # yours are kept (see init-workspace.sh). Use after a git pull to make it take effect.
  shift
  exec env FORCE=1 bash "$HERE/deploy/workspace/init-workspace.sh" "$@"
fi
if [ "${1:-}" = "radio" ]; then
  # The RAW link, with no algorithm above it: ./run.sh radio tx|rx [options].
  # Everything else here runs an algorithm over a PHY; this runs the modem alone with
  # its default message, which is the thing to reach for when you need to know whether
  # the radios work before asking whether the experiment does.
  #
  # It DELEGATES to radio.sh rather than absorbing it. radio.sh is the script people
  # already have in their notes and in docs/COMMANDS.md, it is what calibration and
  # auto_link invoke, and it carries per-device defaults plus the profile resolution
  # for the receive side. Copying that logic to a second place would give the two
  # copies a chance to disagree, and the first symptom of a disagreement is a link
  # that works through one entry point and not the other.
  shift
  exec "$HERE/radio.sh" "$@"
fi
if [ "${1:-}" = "topology" ] || [ "${1:-}" = "topo" ]; then
  # start every node of a topology file that lives on THIS machine, listeners first
  shift
  exec python3 "$HERE/union/run_topology.py" "$@"
fi
if [ "${1:-}" = "topologies" ]; then
  # no argument = list them all; a name = show that one's wiring. Passing "${2:-}"
  # handed the lister an EMPTY NAME to look up, which it correctly failed to find.
  shift
  exec python3 "$HERE/union/topology.py" "$@"
fi
if [ "${1:-}" = "ports" ]; then
  # what another machine must dial to reach THIS session: a site NAME and a port,
  # never an address. Published from the node by deploy/testbed/expose-session-ports.sh
  # within ~15s of the session starting; this only reads the record.
  shift
  exec python3 "$HERE/union/node_ports.py" "$@"
fi
if [ "${1:-}" = "list" ]; then
  echo "algorithms in $HERE/deploy/workspace/algorithms/ :"
  for d in "$HERE"/deploy/workspace/algorithms/*/; do
    n="$(basename "$d")"; [ "$n" = "_template" ] && continue
    [ -f "$d/app.py" ] || continue
    # an algorithm may name its own roles; show them so --role can be typed correctly
    r="$(sed -n 's/^ROLES *= *//p' "$d/app.py" | head -1)"
    if [ -n "$r" ]; then echo "  - $n   roles: $r"
    else echo "  - $n   roles: tx, rx, relay, peer"; fi
  done
  exit 0
fi
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  # Print the WHOLE header block, however long it grows, instead of a line number
  # that silently truncates it. A fixed range quietly dropped the parameter-source
  # and PHY-feature sections the moment the header outgrew 55 lines, so --help
  # stopped mentioning documentation that existed three lines further down.
  sed -n '2,/^[^#]/p' "$0" | sed '$d'; echo; python3 "$RUN" --help; exit 0
fi

# What this wrapper adds, and nothing more. --channel ideal and --steps 5 used to be
# injected here; they are already run_algo's own defaults, and injecting them made them
# indistinguishable from flags the experimenter typed — which is how a --topology file
# would lose a setting to a default nobody chose.
DEF=(--algo echo)
# don't force a role if the user picked one, or asked for a specific node of a
# decentralised network (--node K means "I am one peer", i.e. --role peer)
case " $* " in
  *" --role "*|*" --node "*) ;;
  *) DEF+=(--role loopback) ;;
esac

# does this run need the pyphy extension? The USRP PHY's in-process modem does, under
# either spelling (--channel usrp, or its older alias --channel pyphy). The real radio
# uses sdr_system instead, and the LoRa PHY needs neither.
NEED_PYPHY=0
case " $* " in
  *" pyphy "*|*" usrp "*) NEED_PYPHY=1 ;;
esac

CMD=(python3 "$RUN" "${DEF[@]}" "$@")
echo ">> ${CMD[*]}"
if [ "$NEED_PYPHY" = 1 ]; then
  export PYTHONPATH="$HERE/drivers/usrp/bindings${PYTHONPATH:+:$PYTHONPATH}"
  [ "$(uname)" = "Darwin" ] && exec arch -x86_64 "${CMD[@]}"   # macOS: pyphy is x86_64
fi
exec "${CMD[@]}"
