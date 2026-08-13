#!/usr/bin/env bash
# run_source.sh — launch the B210 source (TX + ARQ) for an FEC test.
# Start the SINK (run_sink.sh) on the N210 host FIRST, then this on the B210 host.
#
# Usage:  ./run_source.sh [FEC] [SCHEME] [WAVE] [TXG] [peak] [-- extra sdr args...]
#   FEC    : ldpc | turbo | conv        (default ldpc)  -- MUST match the sink
#   SCHEME : DQPSK|DBPSK|8-DPSK|QPSK|BPSK|8-PSK ...  (default DQPSK) -- MUST match
#   WAVE   : sc | ofdm                  (default sc)     -- MUST match
#   TXG    : tx-gain (B210, up to ~89)  (default 78)
#   peak   : the literal word "peak" -> adds --ofdm-tx-peak 0.5 (OFDM only)
#
# Examples:
#   ./run_source.sh ldpc  DQPSK sc   78            # Group A
#   ./run_source.sh turbo QPSK  ofdm 80 peak       # Group B
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/fec_test.env"

if [ -z "${SDR:-}" ]; then
  if   [ -x ./sdr_system ];          then SDR=./sdr_system
  elif [ -x ./build/sdr_system ];    then SDR=./build/sdr_system
  elif [ -x "$DIR/build/sdr_system" ]; then SDR="$DIR/build/sdr_system"
  elif [ -x "$DIR/../phy/build/sdr_system" ]; then SDR="$DIR/../phy/build/sdr_system"
  else echo "sdr_system not found — set SDR=/path/to/sdr_system in fec_test.env"; exit 1; fi
fi

FEC="${1:-ldpc}"; SCHEME="${2:-DQPSK}"; WAVE="${3:-sc}"; TXG="${4:-78}"
PEAK=""; [ "${5:-}" = "peak" ] && PEAK="--ofdm-tx-peak 0.5"

# quick reachability nudge (non-fatal)
if command -v nc >/dev/null 2>&1; then
  nc -z -w3 "$N210_HOST" "$ACKPORT" 2>/dev/null \
    || echo "[run_source] note: $N210_HOST:$ACKPORT not accepting yet — is the sink running / firewall open?"
fi

# optional: base everything on a config file (CONFIG=phy.cfg); explicit flags below override.
CFGOPT=""; [ -n "${CONFIG:-}" ] && CFGOPT="--config $CONFIG"

echo "[run_source] $SDR ${CFGOPT:+[$CFGOPT] }FEC=$FEC SCHEME=$SCHEME WAVE=$WAVE tx-gain=$TXG ${PEAK:+ofdm-peak} ack=tcp $N210_HOST:$ACKPORT max-attempts=${MAXATT:-50}"
# anything after the 5 positional args is passed straight through.
# max-attempts is FINITE by default so the source terminates (gives up on an
# unrecoverable chunk) instead of retrying forever — matches stop-after-finish.
# Set MAXATT=0 in fec_test.env for the old never-give-up lockstep behaviour.
exec "$SDR" $CFGOPT --role source_arq \
  --tx-args serial="$B210_SERIAL" --tx-subdev A:A --tx-ant TX/RX \
  --tx-freq "$FREQ" --tx-rate "$TXRATE" --rx-rate "$RXRATE" --symbol_rate "$SYMRATE" --tx-gain "$TXG" \
  --scheme "$SCHEME" --waveform "$WAVE" $PEAK \
  --fec true --fec-type "$FEC" --ldpc-k "$K" \
  --ack-transport tcp --ack-host "$N210_HOST" --ack-port "$ACKPORT" \
  --max-attempts "${MAXATT:-50}" --timeout 3000 --viz false \
  "${@:6}"
