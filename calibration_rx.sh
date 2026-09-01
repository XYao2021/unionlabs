#!/usr/bin/env bash
# calibration_rx.sh — measure the true --sync-threshold on a REAL link, and write
# it into this radio's PHY profile. Run this on the receiver FIRST; then start
# calibration_tx.sh on the transmitting machine (it prints the exact line).
#
#   ./calibration_rx.sh --device n210 --addr 192.168.10.2 --freq 915e6
#   ./calibration_rx.sh --device b210 --serial 30CD424 --freq 915e6 --target 60
#
# WHY THIS EXISTS. prepare.sh is receive-only, and says so: the sync threshold
# is one of the two things a survey cannot settle, because it depends on what a
# real preamble scores ON THIS LINK. This is the link test that settles it. The
# receiver runs with the ACQ gate lowered to just above the measured noise, so
# every real burst prints its peak; only bursts that then pass CRC are counted,
# so stray RF cannot pollute the result. The threshold is the geometric mean of
# the noise p95 and the weakest real peak, clamped away from both.
#
# The TX side needs no coordination channel — RF is the coordination. It can be
# on another machine, another testbed, another institution.
#
# Device selection (pick ONE):  --device b210|n210|x310  |  --addr <ip>  |
#   --serial <sn>  |  --args "<uhd args>"
# Options: --freq --gain --scheme --rate --sym --subdev --ant
#          --target N (CRC-passing bursts to collect, default 40)
#          --timeout S (default 300)  --noise-p95 X (override the profile)
#          --no-write (measure, do not touch the profile)  --dry-run
# Any other sdr_system option can be appended and WINS over our default.
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
                   echo "  cd drivers/usrp && cmake -S . -B build && cmake --build build"; exit 1; }
CAL="$HERE/drivers/usrp/python/calibrate_sync.py"

DEVICE="${DEVICE:-n210}"; ARGS="${ARGS:-}"; ADDR=""; SERIAL=""
FREQ="${FREQ:-915e6}"; GAIN="${GAIN:-}"; SCHEME="${SCHEME:-QPSK}"
RATE="${RATE:-2e6}"; SYM="${SYM:-1e6}"; SUBDEV="${SUBDEV:-}"; ANT="${ANT:-RX2}"
TARGET="${TARGET:-40}"; TIMEOUT="${TIMEOUT:-300}"; NOISE=""; WRITE=1; DRY=0
EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --device)    DEVICE="$2";  shift 2;;
    --args)      ARGS="$2";    shift 2;;
    --addr)      ADDR="$2";    shift 2;;
    --serial)    SERIAL="$2";  shift 2;;
    --freq)      FREQ="$2";    shift 2;;
    --gain)      GAIN="$2";    shift 2;;
    --scheme)    SCHEME="$2";  shift 2;;
    --rate)      RATE="$2";    shift 2;;
    --sym)       SYM="$2";     shift 2;;
    --subdev)    SUBDEV="$2";  shift 2;;
    --ant)       ANT="$2";     shift 2;;
    --target)    TARGET="$2";  shift 2;;
    --timeout)   TIMEOUT="$2"; shift 2;;
    --noise-p95) NOISE="$2";   shift 2;;
    --no-write)  WRITE=0;      shift;;
    --dry-run)   DRY=1;        shift;;
    -h|--help)   sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
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

# ── noise p95: the profile's, unless the caller supplied one ──────────────────
# The gate for the COLLECTION run comes from this: 1.3x the noise p95, so real
# bursts print their peaks while noise mostly stays out. With no measurement the
# gate falls back to 5 — collection still works, but the final threshold is
# one-sided and says so.
if [ -z "$NOISE" ]; then
  NOISE="$(HERE="$HERE" FREQ="$FREQ" ANT="$ANT" SUBDEV="$SUBDEV" ARGS="$ARGS" \
  python3 - <<'PY' 2>/dev/null || true
import json, os, sys
sys.path.insert(0, os.path.join(os.environ["HERE"], "union"))
import phy_profile
path, why = phy_profile.find(near_mhz=float(os.environ["FREQ"]) / 1e6,
                             ant=os.environ.get("ANT"),
                             subdev=os.environ.get("SUBDEV"),
                             args=os.environ.get("ARGS"))
if path:
    v = (json.load(open(path)).get("noise") or {}).get("acq_p95")
    if v is not None:
        print(v)
PY
)"
fi
if [ -n "$NOISE" ]; then
  GATE="$(python3 -c "print(round(max(1.3 * float('$NOISE'), 3.0), 1))")"
  echo "[calibrate] noise ACQ p95 $NOISE (from the profile) -> collection gate $GATE"
else
  GATE=5
  echo "[calibrate] no noise measurement found — run prepare.sh first for a"
  echo "            two-sided threshold. Collecting anyway with gate $GATE."
fi

# ── the exact TX line, so the two ends cannot disagree by transcription ───────
cat <<TXT

  On the TRANSMITTING machine, run:

    ./calibration_tx.sh --freq $FREQ --scheme $SCHEME --rate $RATE --sym $SYM

  (add its radio: --device b210|n210|x310, --addr <ip> or --serial <sn>)

TXT

# mktemp on macOS only expands TRAILING Xs, so derive both names from one base.
BASE="$(mktemp /tmp/calibration-rx.XXXXXX)"
LOG="$BASE.log"; RESULT="$BASE.json"
: > "$LOG"
CMD=("$BIN" --role rx --rx-args "$ARGS" --rx-subdev "$SUBDEV" --rx-ant "$ANT"
     --rx-freq "$FREQ" --rx-rate "$RATE" --tx-rate "$RATE" --symbol_rate "$SYM"
     --rx-gain "$GAIN" --scheme "$SCHEME" --waveform sc --fec true
     --bytes-length 125 --sync-threshold "$GATE"
     --rx-idle-timeout 0 --stop-on-complete false)

# Merge: a caller's option REPLACES our default for the same option (the modem
# rejects a repeated option, so appending alone would not be enough).
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
echo ">> receiver log: $LOG"
[ "$DRY" = 1 ] && exit 0

"${CMD[@]}" >"$LOG" 2>&1 &
RX_PID=$!
trap 'kill "$RX_PID" 2>/dev/null || true' EXIT INT TERM

FOLLOW=(python3 "$CAL" --follow "$LOG" --target "$TARGET" --timeout "$TIMEOUT"
        --result "$RESULT")
[ -n "$NOISE" ] && FOLLOW+=(--noise-p95 "$NOISE")
STATUS=0
"${FOLLOW[@]}" || STATUS=$?
kill "$RX_PID" 2>/dev/null || true
trap - EXIT INT TERM

[ "$STATUS" = 0 ] || { echo "[calibrate] see the receiver's own log: $LOG"; exit "$STATUS"; }

THR="$(python3 -c "
import json; print(json.load(open('$RESULT'))['threshold'])")"

echo
echo "[calibrate] use now:      --sync-threshold $THR   (radio.sh / calibration_rx.sh)"
echo "[calibrate]               --usrp-set sync_threshold=$THR   (run.sh)"

# ── write it where every later run looks ──────────────────────────────────────
if [ "$WRITE" = 1 ]; then
  HERE="$HERE" FREQ="$FREQ" ANT="$ANT" SUBDEV="$SUBDEV" ARGS="$ARGS" RESULT="$RESULT" \
  python3 - <<'PY'
import json, os, sys, time
sys.path.insert(0, os.path.join(os.environ["HERE"], "union"))
import phy_profile
res = json.load(open(os.environ["RESULT"]))
mhz = float(os.environ["FREQ"]) / 1e6
try:
    path, why = phy_profile.find(near_mhz=mhz, ant=os.environ.get("ANT"),
                                 subdev=os.environ.get("SUBDEV"),
                                 args=os.environ.get("ARGS"))
except Exception as e:
    path, why = None, str(e)
if not path or not os.path.exists(str(path)):
    print("[calibrate] no PHY profile found to update"
          + (f" ({why})" if why else "")
          + " — run prepare.sh once on this radio, then rerun; the flag lines "
            "above work regardless.")
    sys.exit(0)
prof = json.load(open(path))
prof["sync_threshold"] = res["threshold"]
prof["sync_threshold_measured"] = True
prof["link_calibration"] = {
    "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "bursts_crc_pass": res["n_good"], "bursts_rejected": res.get("n_rejected", 0),
    "peak_min": res["peak_min"], "peak_median": res["peak_median"],
    "noise_p95_used": res.get("noise_p95"),
}
tmp = str(path) + ".tmp"
with open(tmp, "w") as fh:
    json.dump(prof, fh, indent=2)
    fh.flush(); os.fsync(fh.fileno())
os.replace(tmp, path)
print(f"[calibrate] written to {path}  (sync_threshold_measured: true) — "
      f"radio.sh and run.sh pick it up on the next run, and anything you type "
      f"still wins over it.")
PY
fi
