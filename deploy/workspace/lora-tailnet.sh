#!/bin/bash
# lora-tailnet.sh — put this session on the tailnet, so its browser can reach the LoRa gateway.
#
# Run from the session-start hook (/root/.vnc/xstartup), backgrounded, every time a
# desktop opens. It must be HARMLESS: if anything is missing it logs why and exits 0,
# and the desktop is never delayed or lost because of it.
#
# WHY. The LoRa Pis cannot join the UnionLabs platform. A gateway on a lab machine in the
# tailnet runs jobs on them (drivers/lora/gateway). A session reaches that gateway by
# joining the tailnet itself, as an EPHEMERAL device tagged tag:unionlabs-session.
#
# WHAT KEEPS THIS SAFE. Anything the container holds, the experimenter at its root shell
# holds -- including the key below. That is acceptable ONLY because the tailnet policy
# lets tag:unionlabs-session reach the gateway's page and nothing else: no Pi, no other
# machine. The key is not the protection; the policy is. Never widen that rule.
#
# HOW, inside a pod that cannot create a TUN device:
#   tailscaled --tun=userspace-networking   no kernel interface, no privileges needed
#   --state=mem:                            nothing persisted; the device dies with the pod
#   --outbound-http-proxy-listen            the browser reaches tailnet hosts through it
# and Chrome is pointed at that proxy for tailnet addresses ONLY (a PAC rule), so every
# other site still goes direct.
#
# Inputs (all optional; absent = do nothing):
#   /etc/unionlabs/tailnet.key   OAuth client secret (tskey-client-...) or an auth key,
#                                mode 600, written at image build from a build secret
#   /etc/unionlabs/lora-gateway  the gateway URL (default http://thinkpad-laptop:8080/)
set -u
LOG=/tmp/lora-tailnet.log
exec >>"$LOG" 2>&1
echo "== $(date -Is) lora-tailnet start"

KEY_FILE=/etc/unionlabs/tailnet.key
TAG=tag:unionlabs-session
PROXY=127.0.0.1:1055
SOCK=/run/tailscale/tailscaled.sock
GW_URL="$(cat /etc/unionlabs/lora-gateway 2>/dev/null || echo http://thinkpad-laptop:8080/)"

command -v tailscaled >/dev/null || { echo "tailscaled not installed in this image -- skipping"; exit 0; }
[ -s "$KEY_FILE" ] || { echo "no $KEY_FILE -- this image was built without a tailnet key; skipping"; exit 0; }

if ! tailscale --socket="$SOCK" status >/dev/null 2>&1; then
  mkdir -p /run/tailscale
  setsid tailscaled --tun=userspace-networking --state=mem: --socket="$SOCK" \
    --outbound-http-proxy-listen="$PROXY" >/tmp/tailscaled.log 2>&1 < /dev/null &
  for _ in $(seq 1 30); do [ -S "$SOCK" ] && break; sleep 0.5; done
fi

# THE KEY IS USED ONCE AND THEN DELETED. A session user is root, so a key left on disk
# is a key they can read. It is needed only for this login (tailscaled keeps the node
# in memory afterwards), so after the attempt it is removed from the filesystem. The
# copy in the image layer is not reachable from inside the pod.
# It goes to tailscale as a FILE (--auth-key=file:), never on a command line, where
# `ps` or /proc/<pid>/cmdline would show it to anyone.
mkdir -p /run/tailscale; TMPK="$(mktemp /run/tailscale/.k.XXXXXX)"; chmod 600 "$TMPK"
# An OAuth client secret mints a fresh key per login; these parameters make that key
# ephemeral and pre-approved. A plain auth key carries its own settings and takes none.
case "$(head -c 13 "$KEY_FILE")" in
  tskey-client-) printf '%s?ephemeral=true&preauthorized=true' "$(tr -d '\n\r ' < "$KEY_FILE")" > "$TMPK" ;;
  *)             tr -d '\n\r ' < "$KEY_FILE" > "$TMPK" ;;
esac

# One device per session, named after the pod so the admin console says which is which.
tailscale --socket="$SOCK" up --auth-key="file:$TMPK" --advertise-tags="$TAG" \
  --hostname="unionlabs-$(hostname | tr -c 'a-zA-Z0-9-\n' '-' | cut -c1-40)" \
  --accept-dns=false --timeout=60s
rc=$?
shred -u "$TMPK" 2>/dev/null || rm -f "$TMPK"
shred -u "$KEY_FILE" 2>/dev/null || rm -f "$KEY_FILE"
echo "key removed from this session's filesystem"
if [ "$rc" = 0 ]; then
  echo "joined the tailnet as $(tailscale --socket="$SOCK" ip -4 2>/dev/null)"
else
  echo "tailscale up FAILED (rc=$rc) -- see /tmp/tailscaled.log"; exit 0
fi

# The browser: tailnet hosts (100.64.0.0/10, *.ts.net, the gateway's bare name) through the
# proxy, everything else direct. Chrome cannot read a file:// PAC, so it is inlined.
GW_HOST="$(echo "$GW_URL" | sed -E 's#^[a-z]+://([^/:]+).*#\1#')"
PAC="function FindProxyForURL(u,h){if(h==\"$GW_HOST\"||shExpMatch(h,\"*.ts.net\")||(/^100\\./.test(h)&&isInNet(h,\"100.64.0.0\",\"255.192.0.0\")))return \"PROXY $PROXY\";return \"DIRECT\";}"
PAC_URL="data:application/x-ns-proxy-autoconfig;base64,$(printf '%s' "$PAC" | base64 -w0)"

mkdir -p /root/Desktop
cat > /usr/local/bin/lora-gateway <<EOF
#!/bin/sh
# Open the LoRa gateway in a browser that reaches the tailnet through this session's proxy.
exec /usr/local/bin/google-chrome --user-data-dir=/root/.config/lora-gateway-chrome \\
  --proxy-pac-url='$PAC_URL' --no-first-run --new-window "\${1:-$GW_URL}"
EOF
chmod +x /usr/local/bin/lora-gateway
cat > /root/Desktop/lora-gateway.desktop <<EOF
[Desktop Entry]
Type=Application
Name=LoRa Gateway
Comment=Run point-to-point tests on the LoRa Pis
Exec=/usr/local/bin/lora-gateway
Icon=network-wireless
Terminal=false
EOF
chmod +x /root/Desktop/lora-gateway.desktop

# Prove the path end to end, so the log says whether the page will open.
if curl -s -o /dev/null -w '%{http_code}' --max-time 15 -x "http://$PROXY" "${GW_URL%/}/api/airtime?sf=7&bw=125000&cr=5&size=8" | grep -q 200; then
  echo "gateway reachable at $GW_URL"
else
  echo "gateway NOT reachable at $GW_URL (is it running? does the tailnet policy allow $TAG -> it?)"
fi
