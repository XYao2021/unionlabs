#!/usr/bin/env bash
# build-platform.sh — build the platform-faithful unionlabs image and export the tar.
#   ./build-platform.sh            # linux/amd64 + export
#   ./build-platform.sh --native   # local-arch quick look, no export
#
# THE EXPORT FORMAT IS LOAD-BEARING. Two independent things can make an image the
# testbed refuses to run, and each cost a day to find. Both are settled here; the
# script re-checks the finished tar and refuses to claim success if either regresses.
#
#   1. NO BUILD ATTESTATIONS.  buildx attaches a provenance attestation by default.
#      It rides along in the index as an extra manifest at `platform: unknown/unknown`,
#      and a containerd-based node will not create a container from an index carrying
#      it. Hence --provenance=false --sbom=false.
#
#   2. OCI MEDIA TYPES, NOT DOCKER SCHEMA2.  The testbed's ingestion path is
#      OCI-native: it accepts an archive whose manifest/config/layers are
#      application/vnd.oci.* and rejects the vnd.docker.* schema2 flavour. The two
#      archives are otherwise identical -- same 18 layers, same 20 blobs, same
#      layout version, same manifest.json -- so the difference is invisible unless
#      you read the media types, which is exactly how an image that "built fine"
#      failed to start.
#      buildx's docker exporter emits SCHEMA2 unless told otherwise, so the
#      `oci-mediatypes=true` below is doing real work: drop it and the image builds,
#      exports, loads locally, and then will not run on the testbed. `type=oci`
#      also yields OCI types but omits the manifest.json compat entry that
#      `docker load` needs, hence type=docker + oci-mediatypes rather than type=oci.
#
# A good export: index.json holds ONE manifest, application/vnd.oci.image.manifest.v1+json,
# with no unknown/unknown entry, over application/vnd.oci.image.layer.v1.tar+gzip layers.
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE="${IMAGE:-unionlabs-platform}"
EXPORT=1; NATIVE=0
[ "${1:-}" = "--native" ] && { NATIVE=1; EXPORT=0; }
REPO_URL="${UNIONLABS_REPO:-https://github.com/XYao2021/unionlabs.git}"
SLUG="$(sed -E 's#^https://github.com/##; s#\.git$##' <<<"$REPO_URL")"
REF="${UNIONLABS_REF:-main}"

# --provenance/--sbom are buildx flags; they are what keeps rule 1 above true.
COMMON=(--provenance=false --sbom=false
        -f Dockerfile.platform
        --build-arg "UNIONLABS_REPO=$REPO_URL" --build-arg "UNIONLABS_REF=$REF"
        --build-arg "UNIONLABS_REFS_URL=https://api.github.com/repos/${SLUG}/git/refs/heads/${REF}"
        -t "$IMAGE")

if [ "$NATIVE" = 1 ]; then
  docker buildx build --load "${COMMON[@]}" .
  exit 0
fi

OUT="$(cd .. && pwd)/results/images/${IMAGE}-amd64.tar"
# Remove first: an export that rewrites the file in place keeps its original
# creation date on macOS, so a fresh tar looks stale in a file picker and invites
# uploading the wrong one.
rm -f "$OUT"
echo ">> building linux/amd64 -> $OUT (OCI media types, no attestations)"
docker buildx build --platform linux/amd64 \
  --output "type=docker,oci-mediatypes=true,dest=$OUT" "${COMMON[@]}" .
echo ">> wrote $OUT ($(du -h "$OUT" | cut -f1))"

# Load the SAME tar back, so anything verified locally is verified against exactly
# what the testbed will receive.
echo ">> loading $OUT into the local daemon for verification"
docker load -i "$OUT" >/dev/null && echo ">> loaded ${IMAGE}:latest"

# ── prove the shape before anyone uploads it ─────────────────────────────────
python3 - "$OUT" <<'PY'
import json, sys, tarfile
path = sys.argv[1]
bad, note = [], []
with tarfile.open(path) as t:
    names = t.getnames()
    idx = json.load(t.extractfile("index.json"))
    ms = idx.get("manifests", [])
    if len(ms) != 1:
        bad.append(f"index.json holds {len(ms)} manifests, want exactly 1")
    for m in ms:
        plat = m.get("platform") or {}
        if plat.get("architecture") == "unknown" or plat.get("os") == "unknown":
            bad.append("an attestation manifest (platform unknown/unknown) is present")
        mt = m.get("mediaType", "")
        if "vnd.oci.image" not in mt:
            bad.append(f"manifest mediaType is {mt}, want an OCI type")
        else:
            note.append(f"manifest {mt}")
        d = m.get("digest", "").split(":")[-1]
        blob = f"blobs/sha256/{d}"
        if blob in names:
            im = json.load(t.extractfile(blob))
            cmt = im.get("config", {}).get("mediaType", "")
            if "vnd.oci.image" not in cmt:
                bad.append(f"config mediaType is {cmt}, want an OCI type")
            lts = {l.get("mediaType", "") for l in im.get("layers", [])}
            for lt in lts:
                if "vnd.oci.image.layer" not in lt:
                    bad.append(f"layer mediaType is {lt}, want an OCI type")
            note.append(f"{len(im.get('layers', []))} layers {'/'.join(sorted(lts))}")
    if "manifest.json" not in names:
        bad.append("no manifest.json compat entry (docker load will not read this)")
if bad:
    print(">> EXPORT SHAPE IS WRONG — do NOT upload this tar:")
    for b in bad:
        print(f"     - {b}")
    print(">>   see the header of build-platform.sh for what the shape must be")
    sys.exit(1)
print(">> export shape OK: " + "; ".join(note) + "; manifest.json present; no attestation")
PY
echo ">> sha256: $(shasum -a 256 "$OUT" | cut -d' ' -f1)"
