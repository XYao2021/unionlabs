#!/bin/bash
# One-time install on the RKE2 testbed node (run with sudo):
# copies the publisher and arms a systemd timer that runs it every 15 seconds.
#
# This is the same shape as install-attach-n210.sh, and for the same reason: the
# website platform creates session pods with no hook of ours in them, so anything
# a session needs has to be pushed in from the node afterwards. Here that is a
# NodePort block plus the site aliases, so a session is reachable from the other
# testbed without any experimenter ever handling — or seeing — a host address.
#
# Withdraw the old in-pod grant while you are here; nothing needs it any more. By
# NAME, not by file: the commit that made this script redundant deleted
# rbac-expose.yaml too, so `delete -f` has nothing to read.
#
#     kubectl -n default delete role,rolebinding unionlabs-expose-port --ignore-not-found
#
# Check it is gone -- a session should now be refused, which is the point:
#     kubectl -n default auth can-i create services --as=system:serviceaccount:default:default
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
install -m 755 "$HERE/expose-session-ports.sh" /usr/local/sbin/expose-session-ports.sh
cat > /etc/systemd/system/expose-session-ports.service <<'EOF'
[Unit]
Description=Publish session pod ports and inject site aliases
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/expose-session-ports.sh
EOF
cat > /etc/systemd/system/expose-session-ports.timer <<'EOF'
[Unit]
Description=Run expose-session-ports every 15s
[Timer]
OnBootSec=30
OnUnitActiveSec=15
AccuracySec=5
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now expose-session-ports.timer
echo "armed: sessions get their port block and site aliases within ~15s of starting."
echo
echo "name the sites:   sudoedit /etc/unionlabs/sites.conf     (written on first run)"
echo "watch it work:    journalctl -t expose-session-ports -f"
echo "check one by hand: sudo VERBOSE=1 /usr/local/sbin/expose-session-ports.sh"
