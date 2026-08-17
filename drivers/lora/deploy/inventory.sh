# inventory.sh — the LoRa testbed map. SOURCE this from the other scripts.
#
# NODES ARE DISCOVERED LIVE from `tailscale status`, not hardcoded. Tailscale IPs change
# when a device is re-registered, and every static list we inherited had gone stale: the
# sensing and multipath inventories both listed IPs that no longer exist in the tailnet.
# Asking Tailscale is the only way to be right, and it costs nothing.
#
#   Tailscale device name : raspberrypi-16
#   SSH user              : pi16          (the short form — this is the convention)
#   Serial device         : /dev/ttyACM0  (Teensy/Arduino, when one is attached)
#
# Set PI_STATIC=1 to skip discovery and use the fallback list at the bottom (useful off
# the tailnet, or to pin an experiment to a known set).
#
# NO SECRETS IN THIS FILE. It is committed. See credentials.sh.example.

# ── discovery ───────────────────────────────────────────────────────────────
# Emits "NODE PIHOST IP SERIAL" per line, ONLINE NODES ONLY when discovering live.
_discover_nodes() {
  command -v tailscale >/dev/null 2>&1 || return 1
  local line name ip num
  # `tailscale status` marks a peer offline in its trailing status column; anything
  # without "offline" is up right now.
  while read -r line; do
    [ -z "$line" ] && continue
    ip="$(awk '{print $1}' <<< "$line")"
    name="$(awk '{print $2}' <<< "$line")"
    case "$name" in raspberrypi-*) ;; *) continue ;; esac
    grep -q "offline" <<< "$line" && continue
    num="${name##*-}"                      # raspberrypi-16 -> 16
    echo "pi${num} pi${num} ${ip} /dev/ttyACM0"
  done < <(tailscale status 2>/dev/null)
}

NODES=()
if [ "${PI_STATIC:-0}" != "1" ]; then
  while read -r l; do [ -n "$l" ] && NODES+=("$l"); done < <(_discover_nodes)
fi

# Fallback: every Pi we know of, online or not. Used when tailscale is unavailable, when
# PI_STATIC=1, or when discovery finds nothing. IPs are deliberately absent — the name is
# resolved by Tailscale MagicDNS, which cannot go stale the way a copied IP does.
if [ ${#NODES[@]} -eq 0 ]; then
  for n in 01 02 03 04 05 06 07 08 09 16 17 18; do
    NODES+=("pi${n} pi${n} raspberrypi-${n} /dev/ttyACM0")
  done
fi

# Board: Teensy 4.0 + RFM95 (SX1276) on the nodes that have a radio attached.
FQBN="${FQBN:-teensy:avr:teensy40}"
TEENSY_MCU="${TEENSY_MCU:-TEENSY40}"
TEENSY_URL="${TEENSY_URL:-https://www.pjrc.com/teensy/package_teensy_index.json}"

# Where the driver is pushed, relative to the Pi user's home.
REMOTE_DIR="${REMOTE_DIR:-unionlabs/lora}"

# SSH user. Empty => each node logs in as its short name (pi16), which is the testbed
# convention. NOTE this is NOT the Tailscale device name (raspberrypi-16).
SSH_USER="${SSH_USER:-}"

# ── credentials ─────────────────────────────────────────────────────────────
# Optional and UNTRACKED. Keys are the norm here and are already installed on the Pis, so
# this is usually absent. With it, sshpass supplies SSH_PASS. See credentials.sh.example.
_CRED="$(dirname "${BASH_SOURCE[0]}")/credentials.sh"
# shellcheck disable=SC1090
[ -f "$_CRED" ] && source "$_CRED"

# batch mode so a missing key fails fast instead of hanging on a prompt
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new)

SSH_RUN=(ssh)
SCP_RUN=(scp)
if [ -n "${SSH_PASS:-}" ]; then
  if command -v sshpass >/dev/null 2>&1; then
    SSH_OPTS=(-o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new)
    SSH_RUN=(sshpass -e ssh)
    SCP_RUN=(sshpass -e scp)
    export SSHPASS="$SSH_PASS"     # sshpass -e reads it from here, never from argv
  else
    echo "[inventory] SSH_PASS is set but sshpass is not installed — falling back to keys." >&2
  fi
fi

# Helper: echo "USER@HOST" for a given PIHOST/IP-or-name.
ssh_target() {  # args: pihost ip_or_name
  local pihost="$1" host="$2" user
  user="${SSH_USER:-$pihost}"
  echo "${user}@${host}"
}
