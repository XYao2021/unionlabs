#!/usr/bin/env bash
# linktest-rx.sh — receiver half of a two-step link bring-up test.
# See linktest-tx.sh for the method. Run the matching mode on both ends:
#
#   STEP 1  ./linktest-tx.sh tone     <->  ./linktest-rx.sh power
#   STEP 2  ./linktest-tx.sh data     <->  ./linktest-rx.sh data
#
# Overridable by environment variable, e.g.:
#   DEVICE=b210 FREQ=915e6 ./linktest-rx.sh power
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

MODE="${1:-data}"
DEVICE="${DEVICE:-x310}"

case "$DEVICE" in
  x310) ARGS="${ARGS:-addr=192.168.40.2}"; SUBDEV="${SUBDEV:-A:0}"; GAIN="${GAIN:-25}";;
  n210) ARGS="${ARGS:-addr=192.168.20.2}"; SUBDEV="${SUBDEV:-A:0}"; GAIN="${GAIN:-25}";;
  b210) ARGS="${ARGS:-}";                  SUBDEV="${SUBDEV:-A:A}"; GAIN="${GAIN:-40}";;
  *) echo "unknown DEVICE '$DEVICE' (x310|n210|b210)"; exit 2;;
esac
# RX2 is the normal receive connector. TX/RX also receives, if that is where the
# antenna is — set ANT=TX/RX to match your cabling.
ANT="${ANT:-RX2}"
FREQ="${FREQ:-5680e6}"
RATE="${RATE:-2e6}"
SYM="${SYM:-1e6}"
SCHEME="${SCHEME:-DBPSK}"
MSG_FILE="${MSG_FILE:-/tmp/linktest-msg.txt}"

case "$MODE" in
  power)
    # No decode pipeline at all — just integrate energy and print one line per
    # window. This is the measurement that says whether RF is arriving.
    CMD=("$BIN" --role sense --rx-args "$ARGS" --rx-subdev "$SUBDEV" --rx-ant "$ANT"
         --rx-freq "$FREQ" --rx-rate "$RATE" --tx-rate "$RATE" --symbol_rate "$SYM"
         --rx-gain "$GAIN" --sense-window 10 --sense-count 0)
    cat <<'TXT'
== STEP 1: receive power meter ==
   One [SENSE] line per 10 ms window. Read power_db:
     - transmitter OFF  -> your noise floor
     - transmitter ON   -> should jump well above it (10 dB+ is a healthy link)
   No jump = RF is not arriving. Check, in this order: TX antenna on the TX/RX
   port, both ends on the same --freq, TX gain, then cabling/attenuator.
   Ctrl-C to stop.
TXT
    ;;
  data)
    OPTS=()
    # --ber-expected scores every rejected burst against the known message. It
    # only exists in a patched binary; skip it rather than fail on an old one.
    if [ -f "$MSG_FILE" ] && "$BIN" --help 2>&1 | grep -q -- "--ber-expected"; then
      OPTS+=(--ber-expected "$MSG_FILE")
      echo "   BER scoring ON (reference: $MSG_FILE)"
    else
      [ -f "$MSG_FILE" ] || echo "   note: $MSG_FILE missing — copy it from the transmitter"
      "$BIN" --help 2>&1 | grep -q -- "--ber-expected" || \
        echo "   note: this sdr_system predates --ber-expected; rejected bursts will not be scored"
    fi
    # Run until Ctrl-C: never stop on 'complete', never time out on idle, so the
    # receiver keeps reporting for as long as the transmitter runs.
    CMD=("$BIN" --role rx --rx-args "$ARGS" --rx-subdev "$SUBDEV" --rx-ant "$ANT"
         --rx-freq "$FREQ" --rx-rate "$RATE" --tx-rate "$RATE" --symbol_rate "$SYM"
         --rx-gain "$GAIN" --scheme "$SCHEME" --waveform sc --fec true
         --bytes-length 125 --rx-idle-timeout 0 --stop-on-complete false
         ${OPTS[@]+"${OPTS[@]}"})
    cat <<'TXT'
== STEP 2: decode ==
   Watch for:
     [RX] chunk N/5  [CRC OK, new]   -> a chunk decoded cleanly
     [RX] burst N REJECTED: ...      -> what the failure actually looks like
   On Ctrl-C you get the tally: [RX] bursts=.. CRC-pass=.. CRC-fail(dropped)=..
   A run where every chunk reaches [CRC OK] is a working link.
TXT
    ;;
  *) echo "usage: $0 [power|data]"; exit 2;;
esac

echo ">> ${CMD[*]}"
[ "${DRY:-0}" = 1 ] && exit 0
exec "${CMD[@]}"
