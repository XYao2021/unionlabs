#!/usr/bin/env bash
# check_devices.sh — which Pis are up, and does each one have its radio attached?
#
# Run this BEFORE a deployment. With 8 field nodes, one being off is normal; this tells
# you which, so you size the experiment to what is actually there instead of discovering
# it mid-run.
#
#   ./check_devices.sh              # all nodes
#   ./check_devices.sh --node pi04  # just one (repeatable)
#
# Reports per node: SSH reachable · the serial device present · the Python side installed.
set -uo pipefail
cd "$(dirname "$0")"
source ./inventory.sh

WANT=()
while [ $# -gt 0 ]; do
  case "$1" in
    --node) WANT+=("$2"); shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

wanted() {  # is this Pi in the --node filter (or was no filter given)?
  [ ${#WANT[@]} -eq 0 ] && return 0
  local h="$1"; for w in "${WANT[@]}"; do [ "$w" = "$h" ] && return 0; done; return 1
}

printf "%-5s %-7s %-17s %-9s %-8s %s\n" NODE PI IP SSH RADIO DRIVER
printf "%-5s %-7s %-17s %-9s %-8s %s\n" ----- ------- ----------------- --------- -------- ------
up=0; down=0
for entry in "${NODES[@]}"; do
  read -r NODE PIHOST IP SERIAL <<< "$entry"
  wanted "$PIHOST" || continue
  TGT="$(ssh_target "$PIHOST" "$IP")"
  if ! "${SSH_RUN[@]}" "${SSH_OPTS[@]}" "$TGT" true 2>/dev/null; then
    printf "%-5s %-7s %-17s %-9s %-8s %s\n" "$NODE" "$PIHOST" "$IP" "no" "-" "-"
    down=$((down+1)); continue
  fi
  radio="missing"; drv="no"
  "${SSH_RUN[@]}" "${SSH_OPTS[@]}" "$TGT" "test -e $SERIAL" 2>/dev/null && radio="$SERIAL"
  "${SSH_RUN[@]}" "${SSH_OPTS[@]}" "$TGT" "test -f ~/$REMOTE_DIR/lora_radio.py" 2>/dev/null && drv="yes"
  printf "%-5s %-7s %-17s %-9s %-8s %s\n" "$NODE" "$PIHOST" "$IP" "yes" "$radio" "$drv"
  up=$((up+1))
done
echo
echo "=== $up reachable, $down unreachable ==="
[ "$down" -gt 0 ] && echo "Unreachable nodes are skipped by push_to_pis.sh; re-run it once they are back."
exit 0
