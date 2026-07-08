#!/usr/bin/env bash
# =============================================================================
#  run_training.sh — one-command MNIST-over-SDR training with clean process mgmt.
#
#  Launches the server (sink) and worker (source) as a paired run, prints the
#  live accuracy trajectory, and GUARANTEES no orphaned processes: it clears any
#  prior sdr_system / trainer instances on start, and kills both ends (and their
#  radio subprocesses) on exit — the thing that otherwise leaks "ghosts" that
#  fight over the two B210s.
#
#  Usage:
#    ./run_training.sh [ROUNDS] [HIDDEN] [CHUNK]
#    ./run_training.sh                 # 10 rounds, hidden=32, chunk=256 (defaults)
#    ./run_training.sh 20 32 256       # 20 rounds
#
#  Radios default to serial=30CD3F7 (RX/server) and serial=30CD424 (TX/worker);
#  override with RX_ARGS / TX_ARGS env vars.
# =============================================================================
set -u
ROUNDS="${1:-10}"; HIDDEN="${2:-32}"; CHUNK="${3:-256}"
RX_ARGS="${RX_ARGS:-serial=30CD3F7}"; TX_ARGS="${TX_ARGS:-serial=30CD424}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SLOG=/tmp/train_server.log; WLOG=/tmp/train_worker.log

cleanup() {
    pkill -9 -f mnist_sgd_over_sdr >/dev/null 2>&1
    pkill -9 -f 'build/sdr_system' >/dev/null 2>&1
    [ -n "${TAILPID:-}" ] && kill "$TAILPID" >/dev/null 2>&1
}
trap cleanup EXIT INT TERM

echo "[run_training] clearing any prior instances ..."
cleanup; sleep 2
cd "$HERE"

echo "[run_training] server (sink) on $RX_ARGS ..."
python3 mnist_sgd_over_sdr.py server --rounds "$ROUNDS" --hidden "$HIDDEN" \
        --chunk "$CHUNK" --rx-args "$RX_ARGS" > "$SLOG" 2>&1 &
SPID=$!
sleep 4                                    # let the sink bind the ACK port first

echo "[run_training] worker (source) on $TX_ARGS ..."
python3 mnist_sgd_over_sdr.py worker --rounds "$ROUNDS" --hidden "$HIDDEN" \
        --chunk "$CHUNK" --tx-args "$TX_ARGS" > "$WLOG" 2>&1 &
WPID=$!

# Live accuracy (worker train-acc + server test-acc) as rounds land.
( tail -n +1 -f "$WLOG" | grep --line-buffered -E '\[worker\] round' ) &
TAILPID=$!
( tail -n +1 -f "$SLOG" | grep --line-buffered -E '\[server\] round' ) &

wait "$WPID"                               # worker drives the round count
sleep 3                                    # let the server apply the last round

echo; echo "=================== FULL WORKER TRAJECTORY ==================="
grep -E '\[worker\]' "$WLOG"
echo "============ FULL SERVER TRAJECTORY (reconstructed over RF) ============"
grep -E '\[server\]' "$SLOG"
echo "=================== per-round transport ==================="
grep -E 'Done. Sent' "$WLOG"
echo "(logs: $WLOG  $SLOG)"
