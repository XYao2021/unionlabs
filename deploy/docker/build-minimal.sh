#!/usr/bin/env bash
# build-minimal.sh — build the plain noVNC test image and save it under results/images.
#
#   ./build-minimal.sh              # linux/amd64 (the lab hosts) + export the tar
#   ./build-minimal.sh --native     # this machine's arch, for a quick local look
#
# This image contains NO unionlabs code. It exists to test a DEPLOYMENT: if it works
# where the full image does not, the fault is ours; if it fails too, it is the platform.
set -euo pipefail
cd "$(dirname "$0")/.."                       # deploy/
IMAGE="${IMAGE:-novnc-minimal}"
PLATFORM=(--platform linux/amd64)
EXPORT=1

while [ $# -gt 0 ]; do
  case "$1" in
    --native)   PLATFORM=(); shift ;;
    --amd64)    PLATFORM=(--platform linux/amd64); shift ;;
    --no-export) EXPORT=0; shift ;;
    -h|--help)  sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

BUILD=(docker build)
[ ${#PLATFORM[@]} -gt 0 ] && BUILD=(docker buildx build "${PLATFORM[@]}" --load)
echo ">> building $IMAGE"
"${BUILD[@]}" -f Dockerfile.novnc-minimal -t "$IMAGE" .

if [ "$EXPORT" = 1 ]; then
  OUT_DIR="$(cd .. && pwd)/results/images"
  mkdir -p "$OUT_DIR"
  OUT="$OUT_DIR/${IMAGE}-amd64.tar"
  echo ">> exporting to $OUT"
  docker save -o "$OUT" "${IMAGE}:latest"
  echo ">> wrote $OUT  ($(du -h "$OUT" | cut -f1))"
  echo "   on the host:   docker load -i $(basename "$OUT")"
fi
