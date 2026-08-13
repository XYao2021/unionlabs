#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  run-vnc.sh — run the sdr-phy-vnc container (GUI over noVNC) on a Linux host.
#
#  Then open in a browser:  http://<host>:6080/vnc.html   (no password)
#
#  Modes:
#    docker/run-vnc.sh                 # figures only (bridge net, publishes :6080)
#    USRP=1 docker/run-vnc.sh          # + N210: host networking (6080 lands on the host)
#    USRP=1 USB=1 docker/run-vnc.sh    # + B210 over USB too
#
#  Once up:
#    docker exec -it sdrviz bash                     # run PHY + plots inside the desktop
#    docker exec -it sdrviz feh viz/DQPSK/figure.png # view a figure in the browser desktop
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

IMAGE="${IMAGE:-sdr-phy-vnc:22.04}"
NAME="${NAME:-sdrviz}"
PORT="${PORT:-6080}"

docker rm -f "$NAME" >/dev/null 2>&1 || true

if [ "${USRP:-0}" = "1" ]; then
  # USRP needs host networking; 6080 is then exposed directly on the host.
  sudo sysctl -w net.core.rmem_max=50000000 net.core.wmem_max=2500000 >/dev/null 2>&1 || true
  ARGS=(--network host)
  [ "${USB:-0}" = "1" ] && ARGS+=(--device /dev/bus/usb)
else
  ARGS=(-p "${PORT}:6080")
fi

docker run -d --name "$NAME" "${ARGS[@]}" "$IMAGE"
echo ">> $NAME up. Open:  http://localhost:${PORT}/vnc.html   (from another machine use the host's IP)"
echo ">> Shell in:        docker exec -it $NAME bash"
