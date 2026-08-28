#!/usr/bin/env bash
# prepare.sh — measure this testbed once, so experiments here configure themselves.
#
#   ./prepare.sh --device x310 --addr 192.168.40.2 --band vert2450-5g
#   ./prepare.sh --device b210 --serial 30CD424 --band vert900
#   ./prepare.sh --all --band-map '30CD424:vert900,192.168.40.2:vert2450-5g'
#   ./prepare.sh --device x310 --addr 192.168.40.2 --dry-run   # plan + how long
#
# Sweeps the band the antenna serves, finds every quiet region, and derives the
# carrier, the noise floor and the detector thresholds. Nothing transmits: the
# whole run is receive-only, so it is safe with the other radio idle.
#
# RUN THIS ON THE RECEIVER. Everything it measures is a property of what THIS
# radio hears -- the noise floor, which stretches are quiet, the energy
# detector's margin above that floor, the correlator's threshold, the receive
# gain. A transmitter's own noise says nothing about the link. What a transmitter
# needs is one number, the carrier, and it needs the RECEIVER's:
#
#   ./radio.sh tx --device x310 --phy-node <the receiver's key>
#
# Two things this cannot settle, because no receive-only measurement contains
# them: the TRANSMIT gain, which depends on the path loss between the radios, and
# the true sync threshold, which depends on what a real preamble scores. Both
# want a link test. The saved file marks the threshold as a placeholder when the
# survey could not measure it.
#
# The result is PUBLISHED by default, to the shared workspace, keyed by the
# radio's serial. That is the point: run.sh and radio.sh read it back, so nobody
# tunes --det-mult and --sync-threshold by folklore, and a session that starts on
# this testbed months from now still finds it. --no-write measures without
# publishing. Anything you type on run.sh or radio.sh still wins over it.
#
# You must say which ANTENNA is on the radio (--band). It cannot be probed, and
# sweeping 5 GHz through a 900 MHz antenna measures the antenna, not the band.
#
#   vert900       824-960 MHz      vert2450     2.4-2.5 GHz
#   ism915        902-928 MHz      vert2450-5g  4.9-5.9 GHz
#
# Every prepare_phy option can still be appended and wins over ours; see
#   python3 drivers/usrp/python/prepare_phy.py --help
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HERE/drivers/usrp/python/prepare_phy.py"
[ -f "$PY" ] || { echo "prepare_phy.py not found at $PY"; exit 1; }

DEVICE="${DEVICE:-x310}"; ARGS="${ARGS:-}"; ADDR=""; SERIAL=""
BAND="${BAND:-}"; GAIN="${GAIN:-}"; ANT="${ANT:-RX2}"; SUBDEV="${SUBDEV:-}"
WRITE=1; DRY=0; ALL=0
EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --device)   DEVICE="$2"; shift 2;;
    --args)     ARGS="$2";   shift 2;;
    --addr)     ADDR="$2";   shift 2;;
    --serial)   SERIAL="$2"; shift 2;;
    --band)     BAND="$2";   shift 2;;
    --gain)     GAIN="$2";   shift 2;;
    --ant)      ANT="$2";    shift 2;;
    --subdev)   SUBDEV="$2"; shift 2;;
    --no-write) WRITE=0;     shift;;
    --dry-run)  DRY=1;       shift;;
    --all)      ALL=1;       shift;;
    -h|--help)  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) EXTRA+=("$1"); shift;;
  esac
done

# Per-device defaults: the address it usually lives at, its RF channel, and a
# receive gain that is sane for it. Same values radio.sh uses.
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

if [ -z "$BAND" ] && [ "$ALL" = 0 ]; then
  cat >&2 <<'TXT'
--band is required: which antenna is on this radio?

  --band vert900        824-960 MHz     --band vert2450     2.4-2.5 GHz
  --band ism915         902-928 MHz     --band vert2450-5g  4.9-5.9 GHz

It cannot be probed, and sweeping the wrong one measures the antenna rather than
the band -- a 5 GHz sweep through a 900 MHz antenna looks like a dead channel.
TXT
  exit 2
fi

CMD=(python3 "$PY" --device "$DEVICE" --args "$ARGS" --rx-ant "$ANT"
     --subdev "$SUBDEV" --gain "$GAIN")
[ -n "$BAND" ] && CMD+=(--band "$BAND")
[ "$ALL"   = 1 ] && CMD+=(--all)
[ "$WRITE" = 1 ] && CMD+=(--write)
[ "$DRY"   = 1 ] && CMD+=(--dry-run)

# A caller's option REPLACES ours for the same flag: argparse takes the last
# occurrence, so appending is enough here (unlike the C++ modem, which rejects a
# repeated option outright).
[ ${#EXTRA[@]} -gt 0 ] && CMD+=("${EXTRA[@]}")

echo ">> ${CMD[*]}"
exec "${CMD[@]}"
