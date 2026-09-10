#!/usr/bin/env bash
# build-platform.sh — build the platform-faithful unionlabs image and export the tar.
#   ./build-platform.sh            # linux/amd64 + export
#   ./build-platform.sh --native   # local-arch quick look, no export
#
# The export is written as a CLASSIC single-manifest docker-archive (manifest.json
# + layer blobs), with build attestations turned OFF. This matters: with the
# containerd image store (now Docker Desktop's default) `docker save` instead
# writes an OCI archive whose index carries buildx's PROVENANCE attestation
# manifest, tagged `platform: unknown/unknown`. A containerd-based node — which is
# how a k8s/EC2 testbed imports the tar — hits that unknown platform and refuses
# to create a container ("cannot read image" / no runnable manifest). `--output
# type=docker` + `--provenance=false --sbom=false` produces the old layout every
# loader (docker load, ctr import, nerdctl) reads. Do NOT switch back to
# `--load` + `docker save` for the exported artifact.
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE="${IMAGE:-unionlabs-platform}"
EXPORT=1; NATIVE=0
[ "${1:-}" = "--native" ] && { NATIVE=1; EXPORT=0; }
REPO_URL="${UNIONLABS_REPO:-https://github.com/XYao2021/unionlabs.git}"
SLUG="$(sed -E 's#^https://github.com/##; s#\.git$##' <<<"$REPO_URL")"
REF="${UNIONLABS_REF:-main}"

COMMON=(-f Dockerfile.platform
        --build-arg "UNIONLABS_REPO=$REPO_URL" --build-arg "UNIONLABS_REF=$REF"
        --build-arg "UNIONLABS_REFS_URL=https://api.github.com/repos/${SLUG}/git/refs/heads/${REF}"
        -t "$IMAGE")

if [ "$NATIVE" = 1 ]; then
  # local-arch quick look: load into the daemon, no export.
  docker buildx build --provenance=false --sbom=false --load "${COMMON[@]}" .
else
  # linux/amd64, exported straight to a classic docker-archive (see header).
  OUT="$(cd .. && pwd)/results/images/${IMAGE}-amd64.tar"
  echo ">> building linux/amd64 and exporting to $OUT (classic docker-archive, no attestations)"
  docker buildx build --platform linux/amd64 --provenance=false --sbom=false \
    --output "type=docker,dest=$OUT" "${COMMON[@]}" .
  echo ">> wrote $OUT ($(du -h "$OUT" | cut -f1))"
  # load the SAME tar into the local daemon so any verification runs on exactly
  # what the node will receive.
  echo ">> loading $OUT into the local daemon for verification"
  docker load -i "$OUT"
fi
