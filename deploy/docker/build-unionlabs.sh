#!/usr/bin/env bash
# build-unionlabs.sh — build the all-in-one image (downloads the code itself).
#
#   ./build-unionlabs.sh                       # linux/amd64 (the lab hosts), current main
#   ./build-unionlabs.sh --export              # ...and write a shippable .tar.gz
#   ./build-unionlabs.sh --minimal             # skip torch/networkx/opencv
#   ./build-unionlabs.sh --with-phy            # also compile sdr_system + pyphy
#   ./build-unionlabs.sh --ref my-branch       # a different branch/tag/commit
#   ./build-unionlabs.sh --native              # this machine's arch (fast; local testing)
#
# The build needs NO local checkout of the platform — the image clones it.
#
# PLATFORM: defaults to linux/amd64, because that is what the lab hosts are. On an
# Apple-Silicon Mac that is an emulated build and takes considerably longer than
# --native; the result runs on the hosts, which is the point.
set -euo pipefail
cd "$(dirname "$0")/.."                       # deploy/
IMAGE="${IMAGE:-unionlabs}"
REF=main
INIT="--no-images --cpu-torch"
PLATFORM=(--platform linux/amd64)      # the lab hosts; --native overrides
EXPORT=0

while [ $# -gt 0 ]; do
  case "$1" in
    --minimal)  INIT="--no-images --minimal"; shift ;;
    --with-phy) INIT="--build"; shift ;;
    --ref)      REF="$2"; shift 2 ;;
    --amd64)    PLATFORM=(--platform linux/amd64); shift ;;
    --native)   PLATFORM=(); shift ;;
    --export)   EXPORT=1; shift ;;
    -h|--help)  sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

# Derive the refs API URL from the repo so a fork or branch invalidates the cache too.
REPO_URL="${UNIONLABS_REPO:-https://github.com/XYao2021/unionlabs.git}"
SLUG="$(sed -E 's#^https://github.com/##; s#\.git$##' <<<"$REPO_URL")"
REFS_URL="https://api.github.com/repos/${SLUG}/git/refs/heads/${REF}"

echo ">> building $IMAGE  (ref=$REF, initialization.sh $INIT)"
echo ">> cache key: $REFS_URL"
BUILD=(docker build)
[ ${#PLATFORM[@]} -gt 0 ] && BUILD=(docker buildx build "${PLATFORM[@]}" --load)
"${BUILD[@]}" -f Dockerfile.unionlabs \
  --build-arg "UNIONLABS_REPO=$REPO_URL" \
  --build-arg "UNIONLABS_REF=$REF" \
  --build-arg "UNIONLABS_REFS_URL=$REFS_URL" \
  --build-arg "INIT_ARGS=$INIT" \
  -t "$IMAGE" .
echo ">> built $IMAGE — start it with docker/run-unionlabs.sh"

# A docker image is not a file: it lives in the daemon's storage. Ship it to a host
# that cannot pull from a registry by saving a tarball and loading it there.
if [ "$EXPORT" = 1 ]; then
  OUT_DIR="$(cd .. && pwd)/results/images"        # regenerable output, already gitignored
  mkdir -p "$OUT_DIR"
  OUT="$OUT_DIR/${IMAGE//[:\/]/_}-amd64.tar.gz"
  echo ">> exporting to $OUT  (this takes a few minutes)"
  docker save "$IMAGE" | gzip > "$OUT"
  echo ">> wrote $OUT  ($(du -h "$OUT" | cut -f1))"
  echo "   copy it to the lab host, then:   gunzip -c $(basename "$OUT") | docker load"
fi
