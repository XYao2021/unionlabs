#!/usr/bin/env bash
# run.sh — ONE command to run an algorithm over the SDR PHY.
#
#   ./run.sh                                   # defaults: algo=echo role=loopback channel=ideal
#   ./run.sh --algo marl                       # pick any algorithm from algorithms/
#   ./run.sh --algo marl --channel pyphy --snr-db 6      # through the real modem + noise
#   ./run.sh --algo clip_semcom --steps 45
#   ./run.sh --algo fl --steps 6
#   # over the radio (two hosts) — start the rx FIRST:
#   ./run.sh --algo marl --role rx --rx-args addr=192.168.20.2
#   ./run.sh --algo marl --role tx --tx-args serial=30CD424 --ack-host <AP_IP>
#   ./run.sh list                              # list available algorithms
#   ./run.sh --help                            # this help + every option
#
# Any run_algo.py option can be given; anything you omit takes its default. The pyphy
# extension (needed for --channel pyphy and the radio roles) is wired automatically.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RUN="$HERE/union/run_algo.py"

if [ "${1:-}" = "list" ]; then
  echo "algorithms in $HERE/algorithms/ :"
  for d in "$HERE"/algorithms/*/; do
    n="$(basename "$d")"; [ "$n" = "_template" ] && continue
    [ -f "$d/app.py" ] && echo "  - $n"
  done
  exit 0
fi
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  sed -n '2,16p' "$0"; echo; python3 "$RUN" --help; exit 0
fi

# defaults — override any by passing the same flag (the later value wins)
DEF=(--algo echo --role loopback --channel ideal --steps 5)

# does this run need the pyphy extension? (only the pyphy channel does; radio uses sdr_system)
NEED_PYPHY=0
case " $* " in
  *" pyphy "*) NEED_PYPHY=1 ;;
esac

CMD=(python3 "$RUN" "${DEF[@]}" "$@")
echo ">> ${CMD[*]}"
if [ "$NEED_PYPHY" = 1 ]; then
  export PYTHONPATH="$HERE/drivers/usrp_uhd/bindings${PYTHONPATH:+:$PYTHONPATH}"
  [ "$(uname)" = "Darwin" ] && exec arch -x86_64 "${CMD[@]}"   # macOS: pyphy is x86_64
fi
exec "${CMD[@]}"
