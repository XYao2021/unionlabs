#!/usr/bin/env bash
# run-unionlabs.sh — start the all-in-one image and print the noVNC link.
#
#   ./run-unionlabs.sh                 # figures + terminal in the browser
#   ./run-unionlabs.sh --usb           # + a B210 over USB
#   ./run-unionlabs.sh --host-net      # + an N210 (host networking; Linux only)
#   ./run-unionlabs.sh --port 6081     # a different browser port
#   ./run-unionlabs.sh --refresh       # git pull inside the container on start
#   ./run-unionlabs.sh --stop          # stop and remove it
#
# No password: the VNC server runs with -nopw, so the link opens straight in.
set -euo pipefail
IMAGE="${IMAGE:-unionlabs}"
NAME="${NAME:-unionlabs}"
PORT=6080
ARGS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --usb)      ARGS+=(--device /dev/bus/usb); shift ;;
    --host-net) ARGS+=(--network host); shift ;;
    --port)     PORT="$2"; shift 2 ;;
    --refresh)  ARGS+=(-e REFRESH=1); shift ;;
    --stop)     docker rm -f "$NAME" >/dev/null 2>&1 && echo "stopped $NAME"; exit 0 ;;
    -h|--help)  sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

# --network host publishes nothing: the container already shares the host's ports.
case " ${ARGS[*]-} " in
  *" --network host "*) ;;
  *) ARGS+=(-p "${PORT}:6080") ;;
esac

# UHD wants real-time scheduling and big socket buffers; harmless without a radio.
ARGS+=(--cap-add=SYS_NICE --ulimit rtprio=99 --ulimit memlock=-1)

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" "${ARGS[@]}" "$IMAGE" >/dev/null

# Report a URL that actually works from another machine, not "localhost".
ip="$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo localhost)"
echo
echo "  $NAME is up."
echo "  open:  http://${ip}:${PORT}/vnc.html?autoconnect=1&resize=scale"
echo "         (no password)"
echo
echo "  shell: docker exec -it $NAME bash"
echo "  logs:  docker logs -f $NAME"
echo "  stop:  $0 --stop"
