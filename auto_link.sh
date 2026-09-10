#!/usr/bin/env bash
# auto_link.sh — ONE command, run identically on BOTH machines, that takes a link
# from a hand-written file all the way to a running radio.
#
#   # once: copy the template into the shared workspace and fill in your two radios
#   cp deploy/workspace/settings/link.template.json \
#      /workspace/experiments/settings/link.json    # then edit it
#
#   # then the SAME line on each machine — each works out which end it is:
#   ./auto_link.sh
#
# WHAT IT DOES, per box, in order:
#   1. reads link.json and decides this box's role by the radio serial it can see
#      (source = transmits data / receives ACK;  sink = receives data / transmits ACK)
#   2. prepare.sh — surveys THIS box's RECEIVE band: noise floor, quiet carrier,
#      detector margin (det-mult). Receive-only; nothing transmits.
#   3. calibration.sh — runs the TX->RX link so the receiver measures the real
#      sync-threshold. Both ends agree through the shared file; each drives its
#      own radio. (Author of the plan is derived from link.json — no second file
#      to keep in step.)
#   4. writes ONE resolved parameter file recording exactly what was measured, and
#   5. launches radio.sh with det-mult and sync-threshold filled in from the profile.
#
# The frequencies are settled in link.json (agreed by hand) so the two ends cannot
# drift apart; everything measured (det-mult, sync-threshold, noise floor) is read
# back from the survey/calibration profile, never retyped.
#
#   --role              print this box's role (source|sink|none) and exit
#   --dry-run           show every step's command, run nothing
#   --skip-survey       reuse an existing survey instead of re-running prepare.sh
#   --skip-calibration  reuse an existing sync-threshold instead of running the link
#   --no-radio          stop after writing the resolved parameter file (do not launch)
#   --wait-timeout S    transmitter: seconds to wait for the receiver (default 180)
# Anything else is passed through to radio.sh at the end.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SETUP="$HERE/union/link_setup.py"
[ -f "$SETUP" ] || { echo "link_setup.py not found at $SETUP" >&2; exit 1; }

MODE=run; DRY=0; SKIP_SURVEY=0; SKIP_CAL=0; NO_RADIO=0; WAIT_TIMEOUT=180
PASS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --role)             MODE=role; shift;;
    --dry-run)          DRY=1; shift;;
    --skip-survey)      SKIP_SURVEY=1; shift;;
    --skip-calibration) SKIP_CAL=1; shift;;
    --no-radio)         NO_RADIO=1; shift;;
    --wait-timeout)     WAIT_TIMEOUT="$2"; shift 2;;
    -h|--help)          sed -n '2,38p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *)                  PASS+=("$1"); shift;;
  esac
done

# ── 1 · read the link and this box's place in it ─────────────────────────────
eval "$(python3 "$SETUP" --emit shell 2>/dev/null || echo 'LINK_ROLE=none')"

if [ "$MODE" = role ]; then echo "${LINK_ROLE:-none}"; exit 0; fi

if [ "${LINK_ROLE:-none}" = none ]; then
  echo "[auto_link] no radio in this box matches a role in the link file."
  [ -n "${LINK_WHY:-}" ] && echo "            ${LINK_WHY}"
  echo "            copy deploy/workspace/settings/link.template.json to"
  echo "            /workspace/experiments/settings/link.json and fill in the serials,"
  echo "            or check them against 'uhd_find_devices'."
  exit 1
fi

echo "[auto_link] this box is the $(printf %s "$LINK_ROLE" | tr a-z A-Z) (device ${LINK_DEVICE}, ${LINK_ARGS})."
echo "[auto_link] it RECEIVES on the '${LINK_RX_BAND}' band; ACK transport is ${LINK_ACK_TRANSPORT}."

run() { echo ">> $*"; [ "$DRY" = 1 ] || "$@"; }

# ── 2 · survey THIS box's receive band (det-mult, noise floor, quiet carrier) ──
if [ "$SKIP_SURVEY" = 1 ]; then
  echo "[auto_link] --skip-survey: using the existing survey profile."
elif [ -z "${LINK_RX_BAND:-}" ]; then
  echo "[auto_link] no receive band set for this role in link.json — skipping survey."
else
  echo "[auto_link] === survey (prepare.sh) ==="
  run "$HERE/prepare.sh" --device "$LINK_DEVICE" --args "$LINK_ARGS" --band "$LINK_RX_BAND" \
    || echo "[auto_link] prepare.sh did not complete — calibration will use a one-sided threshold."
fi

# ── 3 · measure the sync-threshold on the data link (source -> sink) ──────────
# The DATA link is what carries user traffic, so its receiver's threshold is the
# one worth measuring. auto_link authors the calibration plan straight from the
# link file (same serials, band, carrier), then hands the run to calibration.sh —
# which does the ready-flag handshake so the two isolated sessions sequence
# correctly. On an RF-ACK link the SOURCE's ACK receiver keeps the threshold from
# its own survey (a placeholder until you calibrate the reverse direction too);
# run auto_link again with data/ack swapped to measure that one.
if [ "$SKIP_CAL" = 1 ]; then
  echo "[auto_link] --skip-calibration: using the existing sync-threshold."
elif [ -z "${LINK_DATA_RX_SERIAL:-}" ] || [ -z "${LINK_DATA_TX_SERIAL:-}" ]; then
  echo "[auto_link] link.json is missing a source or sink serial — skipping calibration."
else
  echo "[auto_link] === calibration link (calibration.sh) ==="
  plan_args=(--plan --rx-serial "$LINK_DATA_RX_SERIAL" --tx-serial "$LINK_DATA_TX_SERIAL"
             --scheme "${LINK_SCHEME:-QPSK}" --rate "${LINK_RATE:-2e6}" --sym "${LINK_SYM:-1e6}")
  [ -n "${LINK_DATA_BAND:-}" ] && plan_args+=(--band "$LINK_DATA_BAND")
  [ -n "${LINK_DATA_FREQ:-}" ] && plan_args+=(--freq "$LINK_DATA_FREQ")
  run "$HERE/calibration.sh" "${plan_args[@]}"
  # calibration.sh surveys the receiver only if no profile exists; step 2 already
  # produced it, so this run goes straight to the link and measures the threshold.
  cal_run=(--wait-timeout "$WAIT_TIMEOUT")
  [ "$DRY" = 1 ] && cal_run+=(--dry-run)
  run "$HERE/calibration.sh" "${cal_run[@]}" \
    || echo "[auto_link] calibration did not complete — the profile keeps its prior threshold."
fi

# ── 4 · one resolved parameter file, for the record and for radio.sh ─────────
# Re-emit AFTER calibration so det-mult and sync-threshold are read from the
# freshly-written profile, and capture the exact radio.sh line.
eval "$(python3 "$SETUP" --emit shell 2>/dev/null || echo 'LINK_ROLE=none')"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUTDIR="${UNION_SETTINGS_DIR:-/workspace/experiments/settings}"
[ -d "$OUTDIR" ] || OUTDIR="$HERE/deploy/workspace/settings"
RESOLVED="$OUTDIR/link-resolved-${LINK_ROLE}-${STAMP}.json"
if [ "$DRY" = 0 ]; then
  export LINK_ROLE LINK_DEVICE LINK_ARGS LINK_RX_BAND LINK_DATA_FREQ \
         LINK_ACK_TRANSPORT LINK_ACK_FREQ LINK_RADIO_CMD RESOLVED MEASURED_AT="$STAMP"
  python3 - <<'PY' || echo "[auto_link] (could not write $RESOLVED)"
import json, os
keys = ("role:LINK_ROLE", "device:LINK_DEVICE", "args:LINK_ARGS",
        "rx_band:LINK_RX_BAND", "data_freq_hz:LINK_DATA_FREQ",
        "ack_transport:LINK_ACK_TRANSPORT", "ack_freq_hz:LINK_ACK_FREQ",
        "radio_cmd:LINK_RADIO_CMD", "measured_at:MEASURED_AT")
rec = {k: os.environ.get(v, "") for k, v in (p.split(":") for p in keys)}
open(os.environ["RESOLVED"], "w").write(json.dumps(rec, indent=2) + "\n")
PY
  [ -e "$RESOLVED" ] && echo "[auto_link] resolved parameters written to $RESOLVED"
fi

# ── 5 · launch radio.sh with everything filled in ────────────────────────────
echo "[auto_link] === launch (radio.sh) ==="
# LINK_RADIO_CMD is a shell-quoted argument list beginning with tx|rx.
eval "set -- $LINK_RADIO_CMD"
if [ "$NO_RADIO" = 1 ]; then
  echo ">> ./radio.sh $* ${PASS[*]-}"
  echo "[auto_link] --no-radio: stopping before launch. The line above is ready to run."
  exit 0
fi
run "$HERE/radio.sh" "$@" ${PASS[@]+"${PASS[@]}"}
