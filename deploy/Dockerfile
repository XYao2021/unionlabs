# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────────────────────────────
#  sdr-phy — the C++ USRP PHY (build/sdr_system) + UHD + the thin Python API.
#
#  PHY ONLY: no torch / MARL brain (that layers on top later as a separate image).
#  Base is pinned to Ubuntu 22.04 to MATCH the lab hosts (UHD 4.1.0.5, g++ 11,
#  Boost 1.74) so the container's UHD matches the N210 you already have working.
#
#  Build for the lab's x86_64 hosts (this Mac is arm64) — see docker/build.sh:
#      docker buildx build --platform linux/amd64 -t sdr-phy:22.04 --load .
#  Run on a LINUX host with --network host (N210) and/or --device /dev/bus/usb (B210).
# ─────────────────────────────────────────────────────────────────────────────
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# ── System + build dependencies (mirrors initialization.sh, apt branch) ──
#   libuhd-dev/uhd-host: UHD + tools (uhd_find_devices, uhd_images_downloader)
#   libboost-all-dev   : program_options (CLI parsing)
#   libfftw3-dev       : single-precision FFTW (fftw3f) used by the DSP
#   libvolk2-dev       : VOLK (UHD's SIMD kernels)
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake pkg-config ca-certificates \
      libuhd-dev uhd-host \
      libboost-all-dev libfftw3-dev libvolk2-dev \
      python3 python3-pip \
      iproute2 iputils-ping \
    && rm -rf /var/lib/apt/lists/*

# ── UHD FPGA/firmware images ──
#   A B210 loads its FX3/FPGA image from here every time it is opened; the N210
#   keeps its image on-device, so this is effectively a no-op for the N210 but is
#   required the moment a USB B210 is used.
RUN uhd_images_downloader

# ── numpy for the Python PHY helpers (channel sensing, sdr.py). No torch here. ──
RUN pip3 install --no-cache-dir numpy==2.1.3

# ── Build sdr_system ──
WORKDIR /opt/sdr/Hardware_update
COPY . .
# .dockerignore drops the host (macOS) build/; rebuild clean for linux/amd64.
RUN rm -rf drivers/usrp_uhd/build \
 && cmake -S drivers/usrp_uhd -B drivers/usrp_uhd/build -DCMAKE_BUILD_TYPE=Release \
 && cmake --build drivers/usrp_uhd/build -j"$(nproc)" \
 && test -x drivers/usrp_uhd/build/sdr_system

# The Python wrapper (python/sdr.py) finds the binary via this env var; also put
# sdr_system + the UHD tools on PATH.
ENV SDR_SYSTEM_BIN=/opt/sdr/Hardware_update/drivers/usrp_uhd/build/sdr_system
ENV PATH=/opt/sdr/Hardware_update/drivers/usrp_uhd/build:${PATH}

# Documentation only (EXPOSE does NOT restrict interfaces — that is a `docker run`
# network choice). TCP control ports used by the AP/agents:
#   5599 = multi-agent ACK server (ap_multi)   5600 = slot clock (slot_sync)
# The N210's UHD traffic is UDP straight over the radio NIC (see docker-compose.n210.yml
# for exposing ONLY that interface via macvlan instead of --network host).
EXPOSE 5599/tcp 5600/tcp

WORKDIR /opt/sdr/Hardware_update
CMD ["bash"]
