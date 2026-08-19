#!/usr/bin/env bash
# build-platform.sh — build the platform-faithful unionlabs image and export the tar.
#   ./build-platform.sh            # linux/amd64 + export
#   ./build-platform.sh --native   # local-arch quick look, no export
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE="${IMAGE:-unionlabs-platform}"
PLATFORM=(--platform linux/amd64); EXPORT=1
[ "${1:-}" = "--native" ] && { PLATFORM=(); EXPORT=0; }
REPO_URL="${UNIONLABS_REPO:-https://github.com/XYao2021/unionlabs.git}"
SLUG="$(sed -E 's#^https://github.com/##; s#\.git$##' <<<"$REPO_URL")"
REF="${UNIONLABS_REF:-main}"
BUILD=(docker build)
[ ${#PLATFORM[@]} -gt 0 ] && BUILD=(docker buildx build "${PLATFORM[@]}" --load)
"${BUILD[@]}" -f Dockerfile.platform \
  --build-arg "UNIONLABS_REPO=$REPO_URL" --build-arg "UNIONLABS_REF=$REF" \
  --build-arg "UNIONLABS_REFS_URL=https://api.github.com/repos/${SLUG}/git/refs/heads/${REF}" \
  -t "$IMAGE" .
if [ "$EXPORT" = 1 ]; then
  OUT="$(cd .. && pwd)/results/images/${IMAGE}-amd64.tar"
  echo ">> exporting to $OUT"; docker save -o "$OUT" "${IMAGE}:latest"
  echo ">> wrote $OUT ($(du -h "$OUT" | cut -f1))"
fi
