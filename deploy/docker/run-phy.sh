#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  run-phy.sh — run the sdr-phy container on a LINUX host with USRP access.
#
#  * N210 (Ethernet): needs --network host so UHD discovery/UDP reach the device.
#  * B210 (USB):      also pass USB=1 to add --device /dev/bus/usb.
#
#  NOTE: run this on a LINUX host, not the Mac — Docker Desktop on macOS runs a VM
#  that can't reach the lab LAN or the USRPs with host networking.
#
#  Usage:
#      docker/run-phy.sh                                   # interactive shell
#      docker/run-phy.sh uhd_find_devices --args addr=192.168.20.2
#      docker/run-phy.sh python3 ap_multi.py --sim-test    # radio-free smoke test
#      USB=1 docker/run-phy.sh uhd_usrp_probe              # B210 over USB
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

IMAGE="${IMAGE:-sdr-phy:22.04}"

# UHD wants large socket buffers; net.core.* are NOT namespaced, so set them on the
# host (shared into the container via --network host). Best-effort — ignore if no sudo.
sudo sysctl -w net.core.rmem_max=50000000 net.core.wmem_max=2500000 >/dev/null 2>&1 \
  || echo "(note: could not raise net.core.rmem_max/wmem_max — do it once on the host for best UHD perf)"

# --cap-add SYS_NICE + rtprio/memlock ulimits let UHD set real-time thread priority
# inside the container (otherwise: "error in pthread_setschedparam").
ARGS=(--rm -it --network host --cap-add=SYS_NICE --ulimit rtprio=99 --ulimit memlock=-1)
[ "${USB:-0}" = "1" ] && ARGS+=(--device /dev/bus/usb)

if [ "$#" -eq 0 ]; then set -- bash; fi
exec docker run "${ARGS[@]}" "$IMAGE" "$@"
