#!/usr/bin/env bash
# linktest-tx.sh — transmitter half of a two-step link bring-up test.
#
# Run this on the TRANSMITTER and linktest-rx.sh on the RECEIVER, in this order:
#
#   STEP 1  ./linktest-tx.sh tone     <->  ./linktest-rx.sh power
#           An unbroken carrier. No modulation, no packets, no decode. Proves RF
#           energy leaves this radio and reaches the other one, and tells you the
#           received level in dB. If this step fails, nothing about the modem
#           matters yet — it is cabling, antenna port, gain, or frequency.
#
#   STEP 2  ./linktest-tx.sh data     <->  ./linktest-rx.sh data
#           A known 5-chunk message, sent as discrete bursts with real gaps, over
#           and over. Only run this once STEP 1 shows a healthy level.
#
#   EXTRA   ./linktest-tx.sh stream   <->  ./linktest-rx.sh power
#           Continuous modulated carrier (--tx-mode continuous). Packets run
#           back-to-back with no gap, so this is for looking at the signal, not
#           for decoding it.
#
# Device selection (pick ONE way to name the radio):
#   --device b210|n210|x310   per-device defaults for subdev / gain / address
#   --addr <ip>               network radio, e.g. --addr 192.168.40.2
#   --serial <sn>             USB radio,     e.g. --serial 30CD424
#   --args "<uhd args>"       anything else, verbatim (e.g. "type=x300,addr=...")
#
# Common options:  --freq --gain --scheme --rate --sym --subdev --ant --tx-scale
#                  --reps --interval --msg-file --dry-run
# ANY other sdr_system option can be appended and WINS over our default for the
# same option, exactly like radio.sh:
#   ./linktest-tx.sh data --device x310 --addr 192.168.40.2 --tx-gain 20
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# Find the modem whether this script sits in the repo root or somewhere else
# entirely (e.g. copied onto the persistent /workspace share).
BIN=""
for c in "$HERE/drivers/usrp/build/sdr_system" "$HERE/build/sdr_system" \
         /opt/unionlabs/drivers/usrp/build/sdr_system \
         /workspace/unionlabs/drivers/usrp/build/sdr_system; do
  [ -x "$c" ] && { BIN="$c"; break; }
done
[ -n "$BIN" ] || BIN="$(command -v sdr_system 2>/dev/null || true)"
[ -n "$BIN" ] || { echo "sdr_system not found — build it first:"; \
                   echo "  cd /opt/unionlabs/drivers/usrp/build && make -j\$(nproc)"; exit 1; }

# Mode is the first bare word; everything else is a flag.
MODE=data
case "${1:-}" in
  tone|data|stream) MODE="$1"; shift ;;
  -h|--help) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac

DEVICE="${DEVICE:-x310}"; ARGS="${ARGS:-}"; ADDR=""; SERIAL=""
FREQ="${FREQ:-5680e6}"; GAIN="${GAIN:-}"; SCHEME="${SCHEME:-DBPSK}"
RATE="${RATE:-2e6}"; SYM="${SYM:-1e6}"; SUBDEV="${SUBDEV:-}"; ANT="${ANT:-TX/RX}"
TXSCALE="${TXSCALE:-0.7}"; REPS="${REPS:-50}"; INTERVAL="${INTERVAL:-500}"
MSG_FILE="${MSG_FILE:-/tmp/linktest-msg.txt}"; DRY="${DRY:-0}"
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
    --msg-file) MSG_FILE="$2"; shift 2;;
    --dry-run)  DRY=1;         shift;;
    -h|--help)  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) EXTRA+=("$1"); shift;;
  esac
done

# Per-device defaults. Only fill in what the caller did not name.
case "$DEVICE" in
  x310) DEF_ARGS="addr=192.168.40.2"; DEF_SUBDEV=A:0; DEF_GAIN=31.5;;
  n210) DEF_ARGS="addr=192.168.20.2"; DEF_SUBDEV=A:0; DEF_GAIN=25;;
  b210) DEF_ARGS="";                  DEF_SUBDEV=A:A; DEF_GAIN=78;;
  *) echo "unknown --device '$DEVICE' (x310|n210|b210)"; exit 2;;
esac
# --addr / --serial are shorthands for the UHD args string; an explicit --args wins.
[ -n "$ADDR" ]   && [ -z "$ARGS" ] && ARGS="addr=$ADDR"
[ -n "$SERIAL" ] && [ -z "$ARGS" ] && ARGS="serial=$SERIAL"
[ -z "$ARGS" ]   && ARGS="$DEF_ARGS"
[ -z "$SUBDEV" ] && SUBDEV="$DEF_SUBDEV"
[ -z "$GAIN" ]   && GAIN="$DEF_GAIN"

# The same 625 bytes (5 chunks x 125) on both ends. Each chunk is labelled, so a
# mis-framed decode is obvious on sight rather than needing a bit comparison.
make_msg() {
  awk 'BEGIN{
    for (c = 1; c <= 5; c++) {
      s = sprintf("CHUNK-%d ", c)
      while (length(s) < 125) s = s "abcdefghijklmnopqrstuvwxyz0123456789 "
      printf "%s", substr(s, 1, 125)
    }
  }'
}

COMMON=(--tx-args "$ARGS" --tx-subdev "$SUBDEV" --tx-ant "$ANT"
        --tx-freq "$FREQ" --tx-rate "$RATE" --rx-rate "$RATE" --symbol_rate "$SYM"
        --tx-gain "$GAIN")

case "$MODE" in
  tone)
    echo "== STEP 1: unbroken carrier | $DEVICE [$ARGS] $FREQ gain=$GAIN port=$ANT =="
    echo "   Run './linktest-rx.sh power' on the receiver. Ctrl-C to stop."
    CMD=("$BIN" --role tx "${COMMON[@]}" --message-type sine --tone-freq 100000
         --tx-mode continuous)
    ;;
  data)
    make_msg > "$MSG_FILE"
    echo "== STEP 2: $(wc -c < "$MSG_FILE") bytes, 5 chunks | $DEVICE [$ARGS] $FREQ $SCHEME =="
    echo "   Reference written to $MSG_FILE — copy it to the RECEIVER at the same path."
    CMD=("$BIN" --role tx "${COMMON[@]}" --scheme "$SCHEME" --waveform sc --fec true
         --tx-scale "$TXSCALE" --message "$(cat "$MSG_FILE")" --bytes-length 125
         --tx-mode burst --tx-reps "$REPS" --interval "$INTERVAL")
    ;;
  stream)
    make_msg > "$MSG_FILE"
    echo "== STREAM: continuous data loop | $DEVICE [$ARGS] $FREQ $SCHEME =="
    echo "   NOTE: --tx-mode continuous sends packets back-to-back with NO gap, so the"
    echo "   receiver cannot find packet boundaries. Use this to watch the signal;"
    echo "   use 'data' when you want chunks to actually decode."
    CMD=("$BIN" --role tx "${COMMON[@]}" --scheme "$SCHEME" --waveform sc --fec true
         --tx-scale "$TXSCALE" --message "$(cat "$MSG_FILE")" --bytes-length 125
         --tx-mode continuous)
    ;;
esac

# Merge: a caller's option REPLACES our default for the same option. The modem
# rejects a repeated option, so appending alone would not be enough.
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
