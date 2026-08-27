#!/usr/bin/env bash
# radio.sh — run raw TX or RX on a USRP.  Device: B210 (default) | N210 | X310.
#
#   ./radio.sh rx                              # B210 receive
#   ./radio.sh tx                              # B210 transmit
#   ./radio.sh tx --device n210                # N210 transmit
#   ./radio.sh rx --device x310 --freq 2437e6  # X310 receive @ 2.437 GHz
#   ./radio.sh tx --args serial=30CD424 --scheme QPSK --gain 78
#   ./radio.sh tx --dry-run                    # print the command, don't run
#
# Options (all optional — sensible per-device defaults are applied):
#   --device b210|n210|x310   --args <uhd args>   --freq <Hz>   --scheme <NAME>
#   --waveform sc|ofdm        --gain <dB>   --rate <Hz>   --sym <Hz>   --fec true|false
#   --ant <TX/RX|RX2>         which CONNECTOR to use (default: TX/RX to send, RX2 to receive)
#   --subdev <A:A|A:B|A:0>    which RF CHANNEL — a B210 has two, RF A (A:A) and RF B (A:B)
# ANY sdr_system option can be appended, and it WINS over the wrapper's default for the
# same option — so the whole modem is reachable here, not just the flags listed above:
#   ./radio.sh tx --device b210 --tx-gain 85 --det-mult 5      # override + add
#   ./radio.sh rx --role sink_arq --ack-port 5599              # ARQ roles, tuned RX defaults
#   ./radio.sh tx --dry-run                                    # see the exact command first
# Full option list:  drivers/usrp/build/sdr_system --help   (also docs/PARAMETERS.md)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HERE/drivers/usrp/build/sdr_system"; [ -x "$BIN" ] || BIN="$HERE/build/sdr_system"

ROLE="${1:-}"; shift 2>/dev/null || true
case "$ROLE" in
  tx|rx) ;;
  -h|--help) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "usage: ./radio.sh <tx|rx> [--device b210|n210|x310] [opts]   (--help for more)"; exit 2 ;;
esac

DEVICE=b210 ARGS="" FREQ=915e6 SCHEME=DQPSK WAVE=sc RATE=2e6 SYM=1e6 GAIN="" FEC=true DRY=0
ANT="" SUB=""      # empty = the sensible default for this role/device
FREQ_SET=0 PROFILE=1   # did the caller name a freq? should we consult the PHY profile?
EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --device) DEVICE="$2"; shift 2;;
    --args)   ARGS="$2";   shift 2;;
    --freq)   FREQ="$2"; FREQ_SET=1; shift 2;;
    --scheme) SCHEME="$2"; shift 2;;
    --waveform) WAVE="$2"; shift 2;;
    --gain)   GAIN="$2";   shift 2;;
    --rate)   RATE="$2";   shift 2;;
    --sym)    SYM="$2";    shift 2;;
    --fec)    FEC="$2";    shift 2;;
    --ant)    ANT="$2";    shift 2;;
    --subdev) SUB="$2";    shift 2;;
    --dry-run) DRY=1;      shift;;
    --no-profile) PROFILE=0; shift;;
    *) EXTRA+=("$1");      shift;;
  esac
done

# per-device defaults (from USRP_CARRIER_MODULATION.txt): addr/subdev + default gains
case "$DEVICE" in
  b210) SUBDEV=A:A; DEF_ARGS="";                  TXG=78; RXG=20;;
  n210) SUBDEV=A:0; DEF_ARGS="addr=192.168.20.2"; TXG=25; RXG=25;;
  x310) SUBDEV=A:0; DEF_ARGS="addr=192.168.40.2"; TXG=25; RXG=25;;
  *) echo "unknown --device '$DEVICE' (use b210|n210|x310)"; exit 2;;
esac
[ -z "$ARGS" ] && ARGS="$DEF_ARGS"
# An explicit channel wins over the per-device default. These must REPLACE the values
# below rather than be appended by the caller: the modem rejects a repeated option
# ("--tx-ant cannot be specified more than once"), so passing them through would fail.
[ -n "$SUB" ] && SUBDEV="$SUB"
case "${ANT:-}" in
  ""|TX/RX|RX2) ;;
  *) echo "note: --ant '$ANT' is not TX/RX or RX2 — passing it through anyway" >&2;;
esac
# ── the PHY profile: what prepare_phy measured on THIS testbed ──────────────
# It fills only what the caller did not name, so an explicit flag always wins,
# and it says what it applied — a default that arrives from a file silently is
# worse than no default at all. --no-profile skips it entirely.
PROF_EXTRA=()
if [ "$PROFILE" = 1 ] && [ -r "$HERE/union/phy_profile.py" ]; then
  PHY_PROFILE_PATH=""; PHY_FREQ=""; PHY_GAIN=""; PHY_DET_MULT=""; PHY_SYNC_THRESHOLD=""
  eval "$(python3 "$HERE/union/phy_profile.py" --emit shell 2>/dev/null || true)"
  if [ -n "$PHY_PROFILE_PATH" ]; then
    APPLIED=()
    if [ "$FREQ_SET" = 0 ] && [ -n "$PHY_FREQ" ]; then
      FREQ="${PHY_FREQ}e6"; APPLIED+=("freq=${FREQ}")
    fi
    if [ -z "$GAIN" ] && [ -n "$PHY_GAIN" ]; then
      GAIN="$PHY_GAIN"; APPLIED+=("gain=${GAIN}")
    fi
    [ -n "$PHY_DET_MULT" ] && { PROF_EXTRA+=(--det-mult "$PHY_DET_MULT"); APPLIED+=("det-mult=$PHY_DET_MULT"); }
    [ -n "$PHY_SYNC_THRESHOLD" ] && { PROF_EXTRA+=(--sync-threshold "$PHY_SYNC_THRESHOLD"); APPLIED+=("sync-threshold=$PHY_SYNC_THRESHOLD"); }
    [ ${#APPLIED[@]} -gt 0 ] && \
      echo "[phy-profile] $(basename "$PHY_PROFILE_PATH"): ${APPLIED[*]}  (anything you typed still wins)" >&2
  fi
fi

[ -z "$GAIN" ] && { [ "$ROLE" = tx ] && GAIN="$TXG" || GAIN="$RXG"; }

if [ "$ROLE" = tx ]; then
  CMD=("$BIN" --role tx --tx-args "$ARGS" --tx-subdev "$SUBDEV" --tx-ant "${ANT:-TX/RX}"
       --tx-freq "$FREQ" --tx-rate "$RATE" --rx-rate "$RATE" --symbol_rate "$SYM"
       --tx-gain "$GAIN" --scheme "$SCHEME" --waveform "$WAVE" --fec "$FEC"
       ${PROF_EXTRA[@]+"${PROF_EXTRA[@]}"})
else
  CMD=("$BIN" --role rx --rx-args "$ARGS" --rx-subdev "$SUBDEV" --rx-ant "${ANT:-RX2}"
       --rx-freq "$FREQ" --rx-rate "$RATE" --tx-rate "$RATE" --symbol_rate "$SYM"
       --rx-gain "$GAIN" --scheme "$SCHEME" --waveform "$WAVE" --fec "$FEC"
       ${PROF_EXTRA[@]+"${PROF_EXTRA[@]}"})
fi
# ── merge: a caller's option REPLACES our default for the same option ──
# The modem rejects a repeated option ("--tx-ant cannot be specified more than once"),
# so appending was not enough: anything the wrapper already emits was unreachable. Drop
# our default for every option the caller named, then append theirs. That is what makes
# the entire modem configurable from here without enumerating its options in this file.
if [ ${#EXTRA[@]} -gt 0 ]; then
  USER_OPTS=" "
  for t in "${EXTRA[@]}"; do
    case "$t" in --*) USER_OPTS="$USER_OPTS${t%%=*} ";; esac
  done
  MERGED=(); i=0
  while [ $i -lt ${#CMD[@]} ]; do
    tok="${CMD[$i]}"
    case "$tok" in
      --*) case "$USER_OPTS" in
             *" $tok "*) i=$((i + 2)); continue ;;   # ours + its value: dropped
           esac ;;
    esac
    MERGED+=("$tok"); i=$((i + 1))
  done
  CMD=("${MERGED[@]}" "${EXTRA[@]}")
fi

echo ">> ${CMD[*]}"
[ "$DRY" = 1 ] && exit 0
exec "${CMD[@]}"
