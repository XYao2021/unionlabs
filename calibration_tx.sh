#!/usr/bin/env bash
# calibration_tx.sh — the transmitting half of the sync-threshold calibration.
# Start calibration_rx.sh on the RECEIVER first; it prints the exact line to run
# here. This side is deliberately dumb: it repeats a known message as discrete
# bursts with real gaps, and RF is the only coordination the two ends need — the
# machines can be on different testbeds with no network path between them.
#
#   ./calibration_tx.sh --device b210 --serial 30CD424 --freq 915e6
#   ./calibration_tx.sh --device x310 --addr 192.168.40.2 --freq 915e6 --reps 80
#
# The receiver counts only bursts that pass CRC, so nothing here needs to be
# copied to the other machine — any burst that decodes cleanly is proof enough.
#
# Device selection (pick ONE):  --device b210|n210|x310  |  --addr <ip>  |
#   --serial <sn>  |  --args "<uhd args>"
# Options: --freq --gain --scheme --rate --sym --subdev --ant --tx-scale
#          --reps N (bursts = 5x this; default 60)  --interval MS  --dry-run
# Any other sdr_system option can be appended and WINS over our default.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN=""
for c in "$HERE/drivers/usrp/build/sdr_system" "$HERE/build/sdr_system" \
         /opt/unionlabs/drivers/usrp/build/sdr_system \
         /workspace/unionlabs/drivers/usrp/build/sdr_system; do
  [ -x "$c" ] && { BIN="$c"; break; }
done
[ -n "$BIN" ] || BIN="$(command -v sdr_system 2>/dev/null || true)"
[ -n "$BIN" ] || { echo "sdr_system not found — build it first:"; \
                   echo "  cd drivers/usrp && cmake -S . -B build && cmake --build build"; exit 1; }

DEVICE="${DEVICE:-b210}"; ARGS="${ARGS:-}"; ADDR=""; SERIAL=""
FREQ="${FREQ:-915e6}"; GAIN="${GAIN:-}"; SCHEME="${SCHEME:-QPSK}"
RATE="${RATE:-2e6}"; SYM="${SYM:-1e6}"; SUBDEV="${SUBDEV:-}"; ANT="${ANT:-TX/RX}"
TXSCALE="${TXSCALE:-0.7}"; REPS="${REPS:-60}"; INTERVAL="${INTERVAL:-500}"; DRY=0
EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --device)   DEVICE="$2";   shift 2;;
    --args)     ARGS="$2";     shift 2;;
    --addr)     ADDR="$2";     shift 2;;
    --serial)   SERIAL="$2";   shift 2;;
    --freq)     FREQ="$2";     shift 2;;
    --gain)     GAIN="$2";     shift 2;;
    --scheme)   SCHEME="$2";   shift 2;;
    --rate)     RATE="$2";     shift 2;;
    --sym)      SYM="$2";      shift 2;;
    --subdev)   SUBDEV="$2";   shift 2;;
    --ant)      ANT="$2";      shift 2;;
    --tx-scale) TXSCALE="$2";  shift 2;;
    --reps)     REPS="$2";     shift 2;;
    --interval) INTERVAL="$2"; shift 2;;
    --dry-run)  DRY=1;         shift;;
    -h|--help)  sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) EXTRA+=("$1"); shift;;
  esac
done

case "$DEVICE" in
  x310) DEF_ARGS="addr=192.168.40.2"; DEF_SUBDEV=A:0; DEF_GAIN=31.5;;
  n210) DEF_ARGS="addr=192.168.20.2"; DEF_SUBDEV=A:0; DEF_GAIN=25;;
  b210) DEF_ARGS="";                  DEF_SUBDEV=A:A; DEF_GAIN=78;;
  *) echo "unknown --device '$DEVICE' (x310|n210|b210)"; exit 2;;
esac
[ -n "$ADDR" ]   && [ -z "$ARGS" ] && ARGS="addr=$ADDR"
[ -n "$SERIAL" ] && [ -z "$ARGS" ] && ARGS="serial=$SERIAL"
[ -z "$ARGS" ]   && ARGS="$DEF_ARGS"
[ -z "$SUBDEV" ] && SUBDEV="$DEF_SUBDEV"
[ -z "$GAIN" ]   && GAIN="$DEF_GAIN"

# The same labelled 625 bytes (5 chunks x 125) every time, so a mis-framed
# decode is obvious on sight. Content does not have to match the receiver:
# only the CRC verdict matters there.
MSG="$(awk 'BEGIN{
  for (c = 1; c <= 5; c++) {
    s = sprintf("CHUNK-%d ", c)
    while (length(s) < 125) s = s "abcdefghijklmnopqrstuvwxyz0123456789 "
    printf "%s", substr(s, 1, 125)
  }
}')"

echo "== calibration TX: $REPS reps x 5 bursts | $DEVICE [$ARGS] $FREQ $SCHEME =="
echo "   calibration_rx.sh should already be waiting on the receiver."
CMD=("$BIN" --role tx --tx-args "$ARGS" --tx-subdev "$SUBDEV" --tx-ant "$ANT"
     --tx-freq "$FREQ" --tx-rate "$RATE" --rx-rate "$RATE" --symbol_rate "$SYM"
     --tx-gain "$GAIN" --scheme "$SCHEME" --waveform sc --fec true
     --tx-scale "$TXSCALE" --message "$MSG" --bytes-length 125
     --tx-mode burst --tx-reps "$REPS" --interval "$INTERVAL")

# Merge: a caller's option REPLACES our default for the same option.
if [ ${#EXTRA[@]} -gt 0 ]; then
  USER_OPTS=" "
  for t in "${EXTRA[@]}"; do
    case "$t" in --*) USER_OPTS="$USER_OPTS${t%%=*} ";; esac
  done
  MERGED=(); i=0
  while [ $i -lt ${#CMD[@]} ]; do
    tok="${CMD[$i]}"
    case "$tok" in
      --*) case "$USER_OPTS" in *" $tok "*) i=$((i + 2)); continue ;; esac ;;
    esac
    MERGED+=("$tok"); i=$((i + 1))
  done
  CMD=("${MERGED[@]}" "${EXTRA[@]}")
fi

echo ">> ${CMD[*]}"
[ "$DRY" = 1 ] && exit 0
exec "${CMD[@]}"
