#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  build-vnc.sh — build the sdr-phy-vnc (noVNC) image for the lab's x86_64 hosts
#  and export a portable tarball you can upload to the platform.
#
#  Builds BOTH layers for linux/amd64 (this Mac is arm64, so it emulates via QEMU
#  — slow; a native build on a Linux host is much faster):
#     1. sdr-phy:22.04        (PHY: UHD + sdr_system, rebuilt from current source)
#     2. sdr-phy-vnc:22.04    (FROM the above + the noVNC desktop)
#  then `docker save | gzip` -> sdr-phy-vnc_amd64.tgz
#
#  Usage:
#     docker/build-vnc.sh                 # build + export tarball
#     PLATFORM=linux/amd64 docker/build-vnc.sh
#  On the platform / a Linux host, load it with:
#     gunzip -c sdr-phy-vnc_amd64.tgz | docker load
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")/.."                        # -> unionlabs (build context)

PLATFORM="${PLATFORM:-linux/amd64}"
BASE="${BASE:-sdr-phy:22.04}"
VNC="${VNC:-sdr-phy-vnc:22.04}"
OUT="${OUT:-sdr-phy-vnc_amd64.tgz}"

echo ">> [1/3] Building base PHY image $BASE ($PLATFORM) — compiles sdr_system..."
docker build --platform "$PLATFORM" -t "$BASE" -f Dockerfile .

echo ">> [2/3] Building noVNC image $VNC ($PLATFORM) — layers the desktop on top..."
docker build --platform "$PLATFORM" -t "$VNC" -f Dockerfile.novnc .

echo ">> [3/3] Exporting $VNC -> $OUT ..."
docker save "$VNC" | gzip > "$OUT"
ls -lh "$OUT"
echo ">> Done. Upload $OUT to the platform, then:  gunzip -c $OUT | docker load"
echo ">> Run with ONLY the N210 interface exposed (not --network host):"
echo "     N210_IFACE=<nic> docker compose -f docker/docker-compose.n210-vnc.yml up -d viz"
echo "     open http://<host>:6080/vnc.html"
