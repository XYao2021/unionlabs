#!/bin/bash
# One-time install on the RKE2 testbed node (run with sudo):
# copies the watcher and arms a systemd timer that runs it every 15 seconds.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
install -m 755 "$HERE/attach-n210.sh" /usr/local/sbin/attach-n210.sh
cat > /etc/systemd/system/attach-n210.service <<'EOF'
[Unit]
Description=Attach the N210 radio NIC to gnuradio session pods
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/attach-n210.sh
EOF
cat > /etc/systemd/system/attach-n210.timer <<'EOF'
[Unit]
Description=Run attach-n210 every 15s
[Timer]
OnBootSec=30
OnUnitActiveSec=15
AccuracySec=5
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now attach-n210.timer
echo "armed: sessions get the radio NIC within ~15s of starting."
echo "watch it work:   journalctl -t attach-n210 -f"
