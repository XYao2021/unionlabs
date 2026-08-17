#!/usr/bin/env bash
# push_to_pis.sh — copy the LoRa driver + the middleware it needs to every Pi.
#
# After this, a Pi can run an experiment locally with its own radio:
#     ./run.sh --algo fl --role client --channel lora \
#              --lora-backend serial --lora-port /dev/ttyACM0
#
#   ./push_to_pis.sh                     # every reachable node
#   ./push_to_pis.sh --deps              # ...and install pyserial + numpy there
#   ./push_to_pis.sh --node pi04 --node pi07     # only these (repeatable)
#   ./push_to_pis.sh --dry-run           # print what would happen, change nothing
#
# Unreachable nodes are SKIPPED and listed at the end rather than failing the run — with
# 8 field nodes, one being off is normal. Re-run once it is back; the copy is idempotent.
#
# Credentials: SSH keys by default. See credentials.sh.example for the password path.
set -uo pipefail
cd "$(dirname "$0")"
source ./inventory.sh

DEPS=0; DRY=0; WANT=()
while [ $# -gt 0 ]; do
  case "$1" in
    --deps)    DEPS=1; shift ;;
    --dry-run) DRY=1;  shift ;;
    --node)    WANT+=("$2"); shift 2 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

wanted() {
  [ ${#WANT[@]} -eq 0 ] && return 0
  local h="$1"; for w in "${WANT[@]}"; do [ "$w" = "$h" ] && return 0; done; return 1
}

# What a Pi needs to drive its own radio: the LoRa driver + the uniform API it sits behind.
DRIVER_DIR="$(cd ../python && pwd)"
UNION_DIR="$(cd ../../../union && pwd)"
FILES=("$DRIVER_DIR/lora_radio.py" "$DRIVER_DIR/framing.py" "$DRIVER_DIR/lora_driver.py")
UNION_FILES=("$UNION_DIR/phy_link.py" "$UNION_DIR/run_algo.py" "$UNION_DIR/driver.py")

for f in "${FILES[@]}" "${UNION_FILES[@]}"; do
  [ -f "$f" ] || { echo "missing source file: $f" >&2; exit 1; }
done

echo "pushing $(basename "$DRIVER_DIR")/{lora_radio,framing,lora_driver}.py + union/ -> ~/$REMOTE_DIR"
[ "$DRY" = 1 ] && echo "(dry run — nothing will be copied)"
echo

ok=0; skipped=()
for entry in "${NODES[@]}"; do
  read -r NODE PIHOST IP SERIAL <<< "$entry"
  wanted "$PIHOST" || continue
  TGT="$(ssh_target "$PIHOST" "$IP")"

  if ! "${SSH_RUN[@]}" "${SSH_OPTS[@]}" "$TGT" true 2>/dev/null; then
    echo ">>> [$NODE] $PIHOST — UNREACHABLE, skipping"
    skipped+=("$NODE/$PIHOST"); continue
  fi
  echo ">>> [$NODE] $TGT"

  if [ "$DRY" = 1 ]; then
    echo "    would: mkdir -p ~/$REMOTE_DIR/union && copy $(( ${#FILES[@]} + ${#UNION_FILES[@]} )) files"
    ok=$((ok+1)); continue
  fi

  "${SSH_RUN[@]}" "${SSH_OPTS[@]}" "$TGT" "mkdir -p ~/$REMOTE_DIR/union" || {
    echo "    mkdir failed"; skipped+=("$NODE/$PIHOST"); continue; }
  "${SCP_RUN[@]}" "${SSH_OPTS[@]}" -q "${FILES[@]}"       "$TGT:~/$REMOTE_DIR/"       || {
    echo "    scp (driver) failed"; skipped+=("$NODE/$PIHOST"); continue; }
  "${SCP_RUN[@]}" "${SSH_OPTS[@]}" -q "${UNION_FILES[@]}" "$TGT:~/$REMOTE_DIR/union/" || {
    echo "    scp (union) failed"; skipped+=("$NODE/$PIHOST"); continue; }
  echo "    copied."

  if [ "$DEPS" = 1 ]; then
    printf "    deps: "
    # Raspberry Pi OS is PEP-668 "externally managed", so plain pip refuses; the
    # --break-system-packages / --user fallbacks are the same ones initialization.sh uses.
    "${SSH_RUN[@]}" "${SSH_OPTS[@]}" "$TGT" '
      need=""
      python3 -c "import serial" 2>/dev/null || need="$need pyserial"
      python3 -c "import numpy"  2>/dev/null || need="$need numpy"
      [ -z "$need" ] && { echo "already present"; exit 0; }
      for p in $need; do
        python3 -m pip install --quiet --break-system-packages "$p" >/dev/null 2>&1 \
          || python3 -m pip install --quiet --user "$p" >/dev/null 2>&1
      done
      python3 -c "import serial, numpy" 2>/dev/null && echo "installed:$need" \
        || { echo "FAILED — on that Pi run: sudo apt install -y python3-serial python3-numpy"; exit 1; }
    ' || true
  fi
  ok=$((ok+1))
done

echo
echo "=== pushed to $ok node(s) ==="
if [ ${#skipped[@]} -gt 0 ]; then
  echo "skipped (${#skipped[@]}): ${skipped[*]}"
  echo "re-run this script when they are back — copying again is harmless."
fi
exit 0
