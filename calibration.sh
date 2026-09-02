#!/usr/bin/env bash
# calibration.sh — ONE command, run identically on both machines, that calibrates
# the sync threshold on a link between two radios. No coordination channel is
# needed between the sessions, and none is used: the two ends agree through a
# file in the shared /workspace, and each drives only its OWN radio.
#
#   # once (or edit the file by hand). The reservation names the radios and the
#   # band; the CARRIER is NOT typed here — it comes from prepare.sh's survey of
#   # the receiver, the quiet spot it found. --freq is available only as an override.
#   ./calibration.sh --plan --rx-serial 3169C62 --tx-serial 30CD424 --band vert900
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

survey_exists() {
  python3 "$HERE/union/phy_profile.py" --emit shell --args "$CAL_RX_ARGS" \
    2>/dev/null | grep -q '^PHY_PROFILE_PATH=.\+'
}

# The carrier the whole calibration runs at. It is NOT hand-typed: a pinned plan
# freq wins if present, else prepare.sh's survey recommendation (the quiet spot
# the receiver found), else the system default. Settled ONCE, here, and handed to
# both ends — so the transmitter cannot land on a different frequency than the
# receiver is listening on.
resolve_freq_hz() {
  if [ -n "$CAL_FREQ" ]; then echo "$CAL_FREQ"; return; fi
  local mhz
  mhz="$(python3 "$HERE/union/phy_profile.py" --emit shell --args "$CAL_RX_ARGS" \
         2>/dev/null | sed -n 's/^PHY_FREQ=//p' | tr -d "'\"")"
  if [ -n "$mhz" ]; then python3 -c "print(float('$mhz') * 1e6)"; return; fi
  echo 915000000        # system default; matches calibration_rx.sh's own default
}

run_rx() {
  local args=(--args "$CAL_RX_ARGS")
  [ -n "$CAL_RX_DEVICE" ] && args+=(--device "$CAL_RX_DEVICE")
  [ -n "$CAL_TARGET" ]    && args+=(--target "$CAL_TARGET")
  [ -n "$CAL_TIMEOUT" ]   && args+=(--timeout "$CAL_TIMEOUT")

  # 1 · a surveyed radio needs no prepare; an unsurveyed one on a named band gets
  #     one first, so there is a recommended carrier and a noise floor to place
  #     the threshold against.
  if [ -n "$CAL_RX_BAND" ] && [ "$DRY" = 0 ] && ! survey_exists; then
    echo "[calibration] no survey for this radio yet — running prepare.sh first"
    "$HERE/prepare.sh" --args "$CAL_RX_ARGS" ${CAL_RX_DEVICE:+--device "$CAL_RX_DEVICE"} \
      --band "$CAL_RX_BAND" || echo "[calibration] prepare.sh did not complete — "\
      "calibration will use a one-sided threshold"
  fi

  # 2 · settle the carrier from the (now-present) survey.
  local freq_hz; freq_hz="$(resolve_freq_hz)"
  local rx_common=(--freq "$freq_hz")
  [ -n "$CAL_SCHEME" ] && rx_common+=(--scheme "$CAL_SCHEME")
  [ -n "$CAL_RATE" ]   && rx_common+=(--rate "$CAL_RATE")
  [ -n "$CAL_SYM" ]    && rx_common+=(--sym "$CAL_SYM")

  # 3 · announce ready WITH that carrier, so the transmitter matches it exactly.
  [ "$DRY" = 0 ] && python3 -c "
import sys; sys.path.insert(0, '$HERE/union'); import calibration_plan as c
c.mark_ready('$CAL_PLAN_PATH',
             link={'freq_hz': float('$freq_hz'),
                   'scheme': '${CAL_SCHEME:-QPSK}',
                   'rate': float('${CAL_RATE:-2e6}'), 'sym': float('${CAL_SYM:-1e6}')},
             note='rx listening')" 2>/dev/null || true

  echo ">> calibration_rx.sh ${rx_common[*]} ${args[*]} ${PASS[*]-}"
  [ "$DRY" = 1 ] && return 0
  "$HERE/calibration_rx.sh" "${rx_common[@]}" "${args[@]}" ${PASS[@]+"${PASS[@]}"}
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
  if [ "$DRY" = 1 ]; then
    echo ">> (wait for RX ready, then) calibration_tx.sh --freq <from RX> ${args[*]} ${PASS[*]-}"
    return 0
  fi
  wait_for_rx || return 1
  # Learn the exact carrier the receiver settled on — from its survey, via the
  # ready flag it wrote into the shared workspace. Nothing about the frequency is
  # retyped on this machine.
  eval "$(python3 -c "
import sys; sys.path.insert(0, '$HERE/union'); import calibration_plan as c
L = c.read_ready('$CAL_PLAN_PATH') or {}
for k, v in (('TX_FREQ','freq_hz'),('TX_SCHEME','scheme'),('TX_RATE','rate'),('TX_SYM','sym')):
    print('%s=%s' % (k, L.get(v) if L.get(v) is not None else ''))")"
  local tx_common=()
  [ -n "${TX_FREQ:-}" ]   && tx_common+=(--freq "$TX_FREQ")
  [ -n "${TX_SCHEME:-}" ] && tx_common+=(--scheme "$TX_SCHEME")
  [ -n "${TX_RATE:-}" ]   && tx_common+=(--rate "$TX_RATE")
  [ -n "${TX_SYM:-}" ]    && tx_common+=(--sym "$TX_SYM")
  echo "[calibration] receiver is listening on ${TX_FREQ:-?} Hz ${TX_SCHEME:-} — transmitting to match."
  "$HERE/calibration_tx.sh" "${tx_common[@]}" "${args[@]}" ${PASS[@]+"${PASS[@]}"}
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
