#!/usr/bin/env bash
# linktest-tx.sh — transmitter half of a two-step link bring-up test.
#
# Run this on the TRANSMITTER, and linktest-rx.sh on the RECEIVER, in this order:
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
# Everything is overridable by environment variable, e.g.:
#   DEVICE=b210 FREQ=915e6 ./linktest-tx.sh data
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

# Per-device RF front end. These are the settings that differ between radios and
# are the usual cause of a silent transmitter.
case "$DEVICE" in
  x310) ARGS="${ARGS:-addr=192.168.40.2}"; SUBDEV="${SUBDEV:-A:0}"; GAIN="${GAIN:-31.5}";;
  n210) ARGS="${ARGS:-addr=192.168.20.2}"; SUBDEV="${SUBDEV:-A:0}"; GAIN="${GAIN:-25}";;
  b210) ARGS="${ARGS:-}";                  SUBDEV="${SUBDEV:-A:A}"; GAIN="${GAIN:-78}";;
  *) echo "unknown DEVICE '$DEVICE' (x310|n210|b210)"; exit 2;;
esac
# TX/RX is the ONLY connector that can transmit. RX2 is receive-only: transmitting
# into it radiates nothing, which looks exactly like a broken link.
ANT="${ANT:-TX/RX}"
FREQ="${FREQ:-5680e6}"
# X310 master clock is 200 MHz, so keep 200e6/RATE an even integer. 2e6 -> 100.
RATE="${RATE:-2e6}"
SYM="${SYM:-1e6}"
SCHEME="${SCHEME:-DBPSK}"
REPS="${REPS:-50}"
# Digital back-off before the DAC. The single-carrier chain has no amplitude
# control of its own, and a pulse-shaped burst can overshoot full scale (1.0) and
# be hard-clipped. Set TXSCALE=1 to reproduce the old, unscaled behaviour.
TXSCALE="${TXSCALE:-0.7}"
INTERVAL="${INTERVAL:-500}"
MSG_FILE="${MSG_FILE:-/tmp/linktest-msg.txt}"

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

case "$MODE" in
  tone)
    CMD=("$BIN" --role tx --tx-args "$ARGS" --tx-subdev "$SUBDEV" --tx-ant "$ANT"
         --tx-freq "$FREQ" --tx-rate "$RATE" --rx-rate "$RATE" --symbol_rate "$SYM"
         --tx-gain "$GAIN" --message-type sine --tone-freq 100000
         --tx-mode continuous)
    echo "== STEP 1: unbroken carrier at $FREQ, gain $GAIN dB, port $ANT =="
    echo "   Run './linktest-rx.sh power' on the receiver. Ctrl-C to stop."
    ;;
  data)
    make_msg > "$MSG_FILE"
    echo "== STEP 2: $(wc -c < "$MSG_FILE") bytes, 5 chunks, $SCHEME, $REPS cycles =="
    echo "   Reference message written to $MSG_FILE"
    echo "   Copy that file to the RECEIVER at the same path before running rx."
    CMD=("$BIN" --role tx --tx-args "$ARGS" --tx-subdev "$SUBDEV" --tx-ant "$ANT"
         --tx-freq "$FREQ" --tx-rate "$RATE" --rx-rate "$RATE" --symbol_rate "$SYM"
         --tx-gain "$GAIN" --scheme "$SCHEME" --waveform sc --fec true
         --tx-scale "$TXSCALE"
         --message "$(cat "$MSG_FILE")" --bytes-length 125
         --tx-mode burst --tx-reps "$REPS" --interval "$INTERVAL")
    ;;
  stream)
    make_msg > "$MSG_FILE"
    echo "== STREAM: continuous data loop, $SCHEME, until Ctrl-C =="
    echo "   Reference message written to $MSG_FILE"
    echo "   NOTE: --tx-mode continuous sends packets back-to-back with NO gap, so"
    echo "   the receiver's energy detector cannot find packet boundaries and will"
    echo "   swallow several packets per burst. Use this to watch the spectrum or"
    echo "   feed './linktest-rx.sh power' with a modulated signal — use 'data'"
    echo "   when you want chunks to actually decode."
    CMD=("$BIN" --role tx --tx-args "$ARGS" --tx-subdev "$SUBDEV" --tx-ant "$ANT"
         --tx-freq "$FREQ" --tx-rate "$RATE" --rx-rate "$RATE" --symbol_rate "$SYM"
         --tx-gain "$GAIN" --scheme "$SCHEME" --waveform sc --fec true
         --tx-scale "$TXSCALE"
         --message "$(cat "$MSG_FILE")" --bytes-length 125
         --tx-mode continuous)
    ;;
  *) echo "usage: $0 [tone|data|stream]"; exit 2;;
esac

echo ">> ${CMD[*]}"
[ "${DRY:-0}" = 1 ] && exit 0
exec "${CMD[@]}"
