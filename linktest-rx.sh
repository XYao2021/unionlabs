#!/usr/bin/env bash
# linktest-rx.sh — receiver half of a two-step link bring-up test.
# See linktest-tx.sh for the method. Run the matching mode on both ends:
#
#   STEP 1  ./linktest-tx.sh tone     <->  ./linktest-rx.sh power
#   STEP 2  ./linktest-tx.sh data     <->  ./linktest-rx.sh data
#
# Device selection (pick ONE way to name the radio):
#   --device b210|n210|x310   per-device defaults for subdev / gain / address
#   --addr <ip>               network radio, e.g. --addr 192.168.40.2
#   --serial <sn>             USB radio,     e.g. --serial 30CD424
#   --args "<uhd args>"       anything else, verbatim
#
# Common options:  --freq --gain --scheme --rate --sym --subdev --ant
#                  --msg-file --dry-run
# ANY other sdr_system option can be appended and WINS over our default:
#   ./linktest-rx.sh data --device b210 --serial 30CD424 --rx-gain 55
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
                   echo "  cd /opt/unionlabs/drivers/usrp/build && make -j\$(nproc)"; exit 1; }

MODE=data
case "${1:-}" in
  power|data) MODE="$1"; shift ;;
  -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac

DEVICE="${DEVICE:-x310}"; ARGS="${ARGS:-}"; ADDR=""; SERIAL=""
FREQ="${FREQ:-5680e6}"; GAIN="${GAIN:-}"; SCHEME="${SCHEME:-DBPSK}"
RATE="${RATE:-2e6}"; SYM="${SYM:-1e6}"; SUBDEV="${SUBDEV:-}"; ANT="${ANT:-RX2}"
MSG_FILE="${MSG_FILE:-/tmp/linktest-msg.txt}"; DRY="${DRY:-0}"
EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --device)  DEVICE="$2";   shift 2;;
    --args)    ARGS="$2";     shift 2;;
    --addr)    ADDR="$2";     shift 2;;
    --serial)  SERIAL="$2";   shift 2;;
    --freq)    FREQ="$2";     shift 2;;
    --gain)    GAIN="$2";     shift 2;;
    --scheme)  SCHEME="$2";   shift 2;;
    --rate)    RATE="$2";     shift 2;;
    --sym)     SYM="$2";      shift 2;;
    --subdev)  SUBDEV="$2";   shift 2;;
    --ant)     ANT="$2";      shift 2;;
    --msg-file) MSG_FILE="$2"; shift 2;;
    --dry-run) DRY=1;         shift;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) EXTRA+=("$1"); shift;;
  esac
done

case "$DEVICE" in
  x310) DEF_ARGS="addr=192.168.40.2"; DEF_SUBDEV=A:0; DEF_GAIN=25;;
  n210) DEF_ARGS="addr=192.168.20.2"; DEF_SUBDEV=A:0; DEF_GAIN=25;;
  b210) DEF_ARGS="";                  DEF_SUBDEV=A:A; DEF_GAIN=40;;
  *) echo "unknown --device '$DEVICE' (x310|n210|b210)"; exit 2;;
esac
[ -n "$ADDR" ]   && [ -z "$ARGS" ] && ARGS="addr=$ADDR"
[ -n "$SERIAL" ] && [ -z "$ARGS" ] && ARGS="serial=$SERIAL"
[ -z "$ARGS" ]   && ARGS="$DEF_ARGS"
[ -z "$SUBDEV" ] && SUBDEV="$DEF_SUBDEV"
[ -z "$GAIN" ]   && GAIN="$DEF_GAIN"

COMMON=(--rx-args "$ARGS" --rx-subdev "$SUBDEV" --rx-ant "$ANT"
        --rx-freq "$FREQ" --rx-rate "$RATE" --tx-rate "$RATE" --symbol_rate "$SYM"
        --rx-gain "$GAIN")

case "$MODE" in
  power)
    echo "== STEP 1: receive power meter | $DEVICE [$ARGS] $FREQ gain=$GAIN port=$ANT =="
    cat <<'TXT'
   One [SENSE] line per 10 ms window. Read power_db:
     - transmitter OFF  -> your noise floor
     - transmitter ON   -> should jump well above it (10 dB+ is a healthy link)
   No jump = RF is not arriving. Check, in this order: TX antenna on the TX/RX
   port, both ends on the same --freq, TX gain, then cabling/attenuator.
   Ctrl-C to stop.
TXT
    CMD=("$BIN" --role sense "${COMMON[@]}" --sense-window 10 --sense-count 0)
    ;;
  data)
    OPTS=()
    # --ber-expected scores every rejected burst against the known message. It
    # only exists in a newer binary, so skip it rather than fail on an old one.
    # Capture --help into a variable instead of piping to grep: `grep -q` exits on
    # the first match, SIGPIPEs the binary, and under `set -o pipefail` that makes
    # the whole test fail intermittently.
    HELP="$("$BIN" --help 2>&1 || true)"
    case "$HELP" in
      *--ber-expected*) HAS_BER=1 ;;
      *)                HAS_BER=0 ;;
    esac
    if [ "$HAS_BER" = 1 ] && [ -f "$MSG_FILE" ]; then
      OPTS+=(--ber-expected "$MSG_FILE")
      echo "   BER scoring ON (reference: $MSG_FILE)"
    elif [ "$HAS_BER" = 1 ]; then
      echo "   note: $MSG_FILE missing — copy it from the transmitter to score bursts"
    else
      echo "   note: this sdr_system predates --ber-expected; rejected bursts are not scored"
    fi
    echo "== STEP 2: decode | $DEVICE [$ARGS] $FREQ $SCHEME =="
    cat <<'TXT'
   Watch for:
     [RX] chunk N/5  [CRC OK, new]   -> a chunk decoded cleanly
     [RX] burst N REJECTED: ...      -> what the failure actually looks like
   On Ctrl-C you get the tally: [RX] bursts=.. CRC-pass=.. CRC-fail(dropped)=..
TXT
    # Run until Ctrl-C: never stop on 'complete', never time out on idle.
    CMD=("$BIN" --role rx "${COMMON[@]}" --scheme "$SCHEME" --waveform sc --fec true
         --bytes-length 125 --rx-idle-timeout 0 --stop-on-complete false
         ${OPTS[@]+"${OPTS[@]}"})
    ;;
esac

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
