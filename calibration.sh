#!/usr/bin/env bash
# calibration.sh — ONE command, run identically on both machines, that calibrates
# the sync threshold on a link between two radios. No coordination channel is
# needed between the sessions, and none is used: the two ends agree through a
# file in the shared /workspace, and each drives only its OWN radio.
#
#   # once, anywhere with both radios in view (or edit the file by hand):
#   ./calibration.sh --plan --rx-serial 3169C62 --tx-serial 30CD424 --freq 915e6
#
#   # then the SAME line on each machine — each works out its own role:
#   ./calibration.sh
#
# HOW A ROLE IS DECIDED. The plan names an rx serial and a tx serial. This reads
# the radios THIS session can see and matches: sees the rx serial -> receiver;
# the tx serial -> transmitter; both -> one host, runs the whole thing; neither
# -> nothing to do here. A serial is used because it stays with the hardware,
# unlike a pod hostname, which is a new string every session.
#
# HOW THE TWO ENDS SEQUENCE. A transmitter must not send before the receiver is
# listening, or the bursts are wasted. The receiver writes a ready flag into the
# shared folder once it is collecting; the transmitter waits for a FRESH flag,
# then sends. Both survive the sessions being isolated pods, because the flag is
# a file, not a message.
#
#   --plan               author the plan (needs --rx-serial --tx-serial [--freq
#                        --scheme --rate --sym --band --target --timeout --reps])
#   --role               print this session's role and exit (rx|tx|both|none)
#   --wait-timeout S     transmitter: how long to wait for the receiver (default 180)
#   --dry-run            show what each side would run, do nothing
# Anything else is passed through to the underlying calibration_rx/tx script.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PLANNER="$HERE/union/calibration_plan.py"

MODE=run; WAIT_TIMEOUT=180; DRY=0
PLAN_ARGS=(); PASS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --plan)          MODE=plan; shift;;
    --role)          MODE=role; shift;;
    --wait-timeout)  WAIT_TIMEOUT="$2"; shift 2;;
    --dry-run)       DRY=1; shift;;
    # plan-authoring options are consumed by the planner, not passed to the modem
    --rx-serial|--tx-serial|--freq|--scheme|--rate|--sym|--band|--target|--timeout|--reps)
                     PLAN_ARGS+=("$1" "$2"); shift 2;;
    -h|--help)       sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *)               PASS+=("$1"); shift;;
  esac
done

if [ "$MODE" = plan ]; then
  exec python3 "$PLANNER" --write "${PLAN_ARGS[@]}"
fi

# Resolve this session's role and the link parameters in one shot.
eval "$(python3 "$PLANNER" --emit shell 2>/dev/null || echo 'CAL_ROLE=none')"

if [ "$MODE" = role ]; then
  echo "$CAL_ROLE"; exit 0
fi

case "$CAL_ROLE" in
  none)
    echo "[calibration] no radio in this session is named by the plan."
    [ -n "${CAL_WHY:-}" ] && echo "              $CAL_WHY" || \
      echo "              run --plan first, or check the plan serials against uhd_find_devices"
    exit 1
    ;;
esac

# Common flags both halves take from the plan. A blank value is simply omitted,
# so the underlying script falls back to its own default (and anything the user
# appended after -- still wins there, as those scripts already guarantee).
# CAL_FREQ is already in Hz (the plan stores freq_hz); the underlying scripts'
# --freq takes Hz, so pass it straight through — no e6 suffix.
common=(--freq "$CAL_FREQ" --scheme "$CAL_SCHEME" --rate "$CAL_RATE" --sym "$CAL_SYM")

run_rx() {
  local args=(--args "$CAL_RX_ARGS")
  [ -n "$CAL_RX_DEVICE" ] && args+=(--device "$CAL_RX_DEVICE")
  [ -n "$CAL_TARGET" ]    && args+=(--target "$CAL_TARGET")
  [ -n "$CAL_TIMEOUT" ]   && args+=(--timeout "$CAL_TIMEOUT")
  # A surveyed radio needs no prepare; an unsurveyed one on a named band gets one
  # first, so calibration has a noise floor to place the threshold against.
  if [ -n "$CAL_RX_BAND" ] && [ "$DRY" = 0 ]; then
    if ! python3 "$HERE/union/phy_profile.py" --emit shell --args "$CAL_RX_ARGS" \
         2>/dev/null | grep -q '^PHY_PROFILE_PATH=.\+'; then
      echo "[calibration] no survey for this radio yet — running prepare.sh first"
      "$HERE/prepare.sh" --args "$CAL_RX_ARGS" ${CAL_RX_DEVICE:+--device "$CAL_RX_DEVICE"} \
        --band "$CAL_RX_BAND" || echo "[calibration] prepare.sh did not complete — "\
        "calibration will use a one-sided threshold"
    fi
  fi
  # Tell the transmitter we are up, the moment before we start listening.
  [ "$DRY" = 0 ] && python3 -c "
import sys; sys.path.insert(0, '$HERE/union'); import calibration_plan as c
c.mark_ready('$CAL_PLAN_PATH', 'rx listening')" 2>/dev/null || true
  echo ">> calibration_rx.sh ${common[*]} ${args[*]} ${PASS[*]-}"
  [ "$DRY" = 1 ] && return 0
  "$HERE/calibration_rx.sh" "${common[@]}" "${args[@]}" ${PASS[@]+"${PASS[@]}"}
  python3 -c "
import sys; sys.path.insert(0, '$HERE/union'); import calibration_plan as c
c.clear_ready('$CAL_PLAN_PATH')" 2>/dev/null || true
}

wait_for_rx() {
  echo "[calibration] waiting up to ${WAIT_TIMEOUT}s for the receiver to start listening..."
  local t=0
  while [ "$t" -lt "$WAIT_TIMEOUT" ]; do
    if python3 -c "
import sys; sys.path.insert(0, '$HERE/union'); import calibration_plan as c
sys.exit(0 if c.ready_seen('$CAL_PLAN_PATH') else 1)" 2>/dev/null; then
      echo "[calibration] receiver is listening — transmitting."
      return 0
    fi
    sleep 3; t=$((t + 3))
  done
  echo "[calibration] receiver never signalled ready after ${WAIT_TIMEOUT}s."
  echo "              Start ./calibration.sh on the receiver first, or raise --wait-timeout."
  return 1
}

run_tx() {
  local args=(--args "$CAL_TX_ARGS")
  [ -n "$CAL_TX_DEVICE" ] && args+=(--device "$CAL_TX_DEVICE")
  [ -n "$CAL_REPS" ]      && args+=(--reps "$CAL_REPS")
  echo ">> calibration_tx.sh ${common[*]} ${args[*]} ${PASS[*]-}"
  [ "$DRY" = 1 ] && return 0
  wait_for_rx || return 1
  "$HERE/calibration_tx.sh" "${common[@]}" "${args[@]}" ${PASS[@]+"${PASS[@]}"}
}

case "$CAL_ROLE" in
  rx)
    echo "[calibration] this session is the RECEIVER."
    run_rx
    ;;
  tx)
    echo "[calibration] this session is the TRANSMITTER."
    run_tx
    ;;
  both)
    # One host holds both radios: be the receiver in the background, then
    # transmit into it. UHD claims a device per process, so these are two
    # processes on two radios, not one.
    echo "[calibration] both radios are here — running the whole link locally."
    if [ "$DRY" = 1 ]; then run_rx; run_tx; exit 0; fi
    run_rx & RX_PID=$!
    trap 'kill "$RX_PID" 2>/dev/null || true' EXIT INT TERM
    run_tx
    wait "$RX_PID" 2>/dev/null || true
    trap - EXIT INT TERM
    ;;
esac
