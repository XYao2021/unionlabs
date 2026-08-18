#!/usr/bin/env bash
# build-unionlabs.sh — build the all-in-one image (downloads the code itself).
#
#   ./build-unionlabs.sh                       # full stack, current main
#   ./build-unionlabs.sh --minimal             # skip torch/networkx/opencv
#   ./build-unionlabs.sh --with-phy            # also compile sdr_system + pyphy
#   ./build-unionlabs.sh --ref my-branch       # a different branch/tag/commit
#   ./build-unionlabs.sh --amd64               # cross-build for x86_64 lab hosts
#
# The build needs NO local checkout of the platform — the image clones it.
set -euo pipefail
cd "$(dirname "$0")/.."                       # deploy/
IMAGE="${IMAGE:-unionlabs}"
REF=main
INIT="--no-images"
PLATFORM=()

while [ $# -gt 0 ]; do
  case "$1" in
    --minimal)  INIT="--no-images --minimal"; shift ;;
    --with-phy) INIT="--build"; shift ;;
    --ref)      REF="$2"; shift 2 ;;
    --amd64)    PLATFORM=(--platform linux/amd64); shift ;;
    -h|--help)  sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

echo ">> building $IMAGE  (ref=$REF, initialization.sh $INIT)"
BUILD=(docker build)
[ ${#PLATFORM[@]} -gt 0 ] && BUILD=(docker buildx build "${PLATFORM[@]}" --load)
"${BUILD[@]}" -f Dockerfile.unionlabs \
  --build-arg "UNIONLABS_REF=$REF" \
  --build-arg "INIT_ARGS=$INIT" \
  -t "$IMAGE" .
echo ">> built $IMAGE — start it with docker/run-unionlabs.sh"
