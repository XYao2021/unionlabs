#!/usr/bin/env bash
# run_sink.sh — launch the N210 sink (RX + TCP-ACK server) for an FEC test.
#
# Usage:  ./run_sink.sh [FEC] [SCHEME] [WAVE] [RXG] [soft] [-- extra sdr args...]
#   FEC    : ldpc | turbo | conv        (default ldpc)
#   SCHEME : DQPSK|DBPSK|8-DPSK|QPSK|BPSK|8-PSK ...  (default DQPSK)
#   WAVE   : sc | ofdm                  (default sc)
#   RXG    : rx-gain 0..31.5 (N210)     (default 25)
#   soft   : the literal word "soft" -> adds --fec_soft (coherent schemes only)
#
# Examples:
#   ./run_sink.sh ldpc  DQPSK sc   25              # Group A (SC differential, hard)
#   ./run_sink.sh turbo QPSK  ofdm 22 soft         # Group B (OFDM coherent, soft)
#   ./run_sink.sh turbo QPSK  ofdm 22 soft --ber-expected   # add per-burst BER
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/fec_test.env"

# locate the binary
if [ -z "${SDR:-}" ]; then
  if   [ -x ./sdr_system ];          then SDR=./sdr_system
  elif [ -x ./build/sdr_system ];    then SDR=./build/sdr_system
  elif [ -x "$DIR/build/sdr_system" ]; then SDR="$DIR/build/sdr_system"
  elif [ -x "$DIR/../drivers/usrp/build/sdr_system" ]; then SDR="$DIR/../drivers/usrp/build/sdr_system"
  else echo "sdr_system not found — set SDR=/path/to/sdr_system in fec_test.env"; exit 1; fi
fi

FEC="${1:-ldpc}"; SCHEME="${2:-DQPSK}"; WAVE="${3:-sc}"; RXG="${4:-25}"
SOFT=""; [ "${5:-}" = "soft" ] && SOFT="--fec_soft"

# DEFAULT: stop-after-finish — stop once the whole message is CRC-verified, and
# give up if nothing arrives within IDLE seconds (time to start the source). Set
# SINK_MODE=serve in fec_test.env for the old warm always-on AP (never stops).
if [ "${SINK_MODE:-stop}" = "serve" ]; then
  MODE="--serve-forever --rx-idle-timeout 0 --stop-on-complete false"
  MODEDESC="serve(warm-AP)"
else
  MODE="--stop-on-complete true --rx-idle-timeout ${IDLE:-30}"
  MODEDESC="stop-after-finish(idle=${IDLE:-30}s)"
fi

# optional: base everything on a config file (CONFIG=phy.cfg in fec_test.env); the
# explicit flags below still override it (you change only what you need on top).
CFGOPT=""; [ -n "${CONFIG:-}" ] && CFGOPT="--config $CONFIG"

echo "[run_sink] $SDR ${CFGOPT:+[$CFGOPT] }FEC=$FEC SCHEME=$SCHEME WAVE=$WAVE rx-gain=$RXG ${SOFT:-hard} ack=tcp:$ACKPORT $MODEDESC"
# anything after the 5 positional args is passed straight through (e.g. --ber-expected)
exec "$SDR" $CFGOPT --role sink_arq \
  --rx-args addr="$N210_ADDR" --rx-subdev A:0 --rx-ant RX2 \
  --rx-freq "$FREQ" --rx-rate "$RXRATE" --tx-rate "$TXRATE" --symbol_rate "$SYMRATE" --rx-gain "$RXG" \
  --scheme "$SCHEME" --waveform "$WAVE" \
  --fec true --fec-type "$FEC" --ldpc-k "$K" $SOFT \
  --ack-transport tcp --ack-port "$ACKPORT" \
  $MODE --viz false --det-mult 3 \
  "${@:6}"
