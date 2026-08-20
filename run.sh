#!/usr/bin/env bash
# run.sh — ONE command to run an algorithm over the SDR PHY.
#
#   ./run.sh                                   # defaults: algo=echo role=loopback channel=ideal
#   ./run.sh --algo marl                       # pick any algorithm from experiments/
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
if [ "${1:-}" = "topology" ] || [ "${1:-}" = "topo" ]; then
  # start every node of a topology file that lives on THIS machine, listeners first
  shift
  exec python3 "$HERE/union/run_topology.py" "$@"
fi
if [ "${1:-}" = "topologies" ]; then
  exec python3 "$HERE/union/topology.py" "${2:-}"
fi
if [ "${1:-}" = "list" ]; then
  echo "algorithms in $HERE/experiments/ :"
  for d in "$HERE"/experiments/*/; do
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
  sed -n '2,55p' "$0"; echo; python3 "$RUN" --help; exit 0
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
