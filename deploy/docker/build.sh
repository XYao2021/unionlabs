#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  build.sh — build the sdr-phy image for the lab's x86_64 Linux hosts.
#
#  This Mac is arm64, the lab boxes are x86_64, so we cross-build linux/amd64
#  via Docker Desktop's built-in buildx + QEMU (slower than a native build, but
#  it needs no lab machine). Prefer building ON a Linux host when you can:
#      docker build -t sdr-phy:22.04 .        # native, fast
#
#  Usage:
#      docker/build.sh                 # build sdr-phy:22.04 for linux/amd64
#      IMAGE=sdr-phy:dev docker/build.sh
#      docker/build.sh --export        # also save a portable tarball to transfer
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")/.."                       # -> Hardware_update (build context)
IMAGE="${IMAGE:-sdr-phy:22.04}"
PLATFORM="${PLATFORM:-linux/amd64}"

# A buildx builder that supports cross-platform (docker-container driver).
docker buildx inspect sdrbuilder >/dev/null 2>&1 \
  || docker buildx create --name sdrbuilder --driver docker-container --use
docker buildx use sdrbuilder

echo ">> Building $IMAGE for $PLATFORM (this compiles sdr_system under emulation)..."
docker buildx build --platform "$PLATFORM" -t "$IMAGE" --load .

echo ">> Done: $IMAGE"
if [ "${1:-}" = "--export" ]; then
  OUT="sdr-phy_$(echo "$IMAGE" | tr ':/' '__').tgz"
  echo ">> Exporting portable image to $OUT ..."
  docker save "$IMAGE" | gzip > "$OUT"
  echo ">> Transfer it:  scp $OUT user@lab-host:~/   then on the host:  gunzip -c $OUT | docker load"
fi
