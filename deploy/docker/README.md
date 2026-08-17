# sdr-phy container

A reproducible Linux image for the **C++ USRP PHY** (`build/sdr_system`) + UHD + the
thin Python API. **PHY only** — no torch / MARL brain (that layers on top later).

Pinned to **Ubuntu 22.04** to match the lab hosts (UHD 4.1.0.5, g++ 11, Boost 1.74),
so the container's UHD matches the N210 that already works there. Building in a clean
container also removes the Anaconda-cmake / stale-`build/` / arch-mismatch problems.

## Why a container helps here
- No more `/opt/anaconda3/.../cmake not found` — clean apt toolchain, no conda.
- No "Exec format error" — the binary is compiled for the image's arch, not copied.
- One `uhd_images_downloader` bakes the B210 FPGA/firmware images in.

## 0. Prereqs
- **Build:** Docker Desktop (this Mac, arm64) — cross-builds `linux/amd64` via buildx+QEMU.
- **Run:** a **Linux** host with the USRP attached. Host networking + USB passthrough
  do **not** work under Docker Desktop's macOS VM, so runs happen on the lab boxes.

## 1. Build (on the Mac)
```bash
docker/build.sh                 # -> sdr-phy:22.04 (linux/amd64)
docker/build.sh --export        # also writes sdr-phy_*.tgz to transfer
```
Prefer a **native** build if you're already on a Linux host (much faster):
```bash
docker build -t sdr-phy:22.04 .     # run from the unionlabs/ dir
```

## 2. Ship to a lab host (if built on the Mac)
```bash
scp sdr-phy_sdr-phy__22.04.tgz  user@ece-d6114-lnx01:~/
# on the host:
gunzip -c sdr-phy_sdr-phy__22.04.tgz | docker load
```
(or push/pull via a registry if you have one.)

## 3. Run on the Linux host
```bash
# N210 (Ethernet) — host networking:
docker/run-phy.sh uhd_find_devices --args addr=192.168.20.2

# Interactive shell in the PHY dir:
docker/run-phy.sh

# B210 (USB) — add USB passthrough:
USB=1 docker/run-phy.sh uhd_usrp_probe
```
Or via compose (stays up; exec in):
```bash
docker compose -f docker/docker-compose.yml up -d phy
docker compose -f docker/docker-compose.yml exec phy bash
```

UHD wants large socket buffers; `net.core.*` are not namespaced, so set them on the
**host** (run-phy.sh attempts this):
```bash
sudo sysctl -w net.core.rmem_max=50000000 net.core.wmem_max=2500000
```

## 4. Validate (in order)
```bash
# a) radio-free — proves the build + Python env, no hardware:
docker/run-phy.sh python3 python/ap_multi.py --sim-test

# b) UHD sees the N210 from inside the container:
docker/run-phy.sh uhd_find_devices --args addr=192.168.20.2

# c) a real PHY link (N210 RX warm decoder — same command we run bare-metal):
docker/run-phy.sh ./sdr_system --role rx --rx-args addr=192.168.20.2 \
  --rx-subdev A:0 --rx-ant RX2 --tx-rate 2e6 --rx-rate 2e6 --symbol_rate 1e6 \
  --rx-freq 915e6 --rx-gain 20 --scheme DQPSK --fec true --marl-report true \
  --stop-on-complete false --rx-idle-timeout 0 --viz false --bytes-length 125
```

## 5. Visualization variant — `sdr-phy-vnc` (noVNC in the browser)
`../Dockerfile.novnc` layers a lightweight GUI desktop on the PHY image and serves it over
**noVNC**, so you can view the PHY figures (`plot_viz.py` constellation / spectrum PNGs) and
run matplotlib in a browser — handy on a headless lab host. It also adds **matplotlib** (the
base image only has numpy), which the `--viz` auto-plot needs.

Stack: `Xvfb` → `fluxbox` → `x11vnc` → `websockify`/`novnc` on port **6080**; `feh` views
figures, `python3-tk` enables interactive matplotlib. matplotlib is installed via **pip** (not
apt) so it stays ABI-compatible with the base's pip `numpy 2.1.3`.

```bash
# build (needs sdr-phy:22.04 first — it's FROM it)
docker build -f ../Dockerfile.novnc -t sdr-phy-vnc:22.04 ..
# or from the Mac: docker buildx build --platform linux/amd64 -f ../Dockerfile.novnc -t sdr-phy-vnc:22.04 --load ..

# run (Linux host), then open  http://<host>:6080/vnc.html  (no password)
docker/run-vnc.sh                 # figures only (publishes :6080)
USRP=1 docker/run-vnc.sh          # + N210 (host networking; 6080 on the host)
USRP=1 USB=1 docker/run-vnc.sh    # + B210 over USB

# use it: generate a figure, then view it in the browser desktop
docker exec -it sdrviz ./sdr_system --role rx --rx-args addr=192.168.20.2 \
  --rx-subdev A:0 --rx-ant RX2 --tx-rate 2e6 --rx-rate 2e6 --symbol_rate 1e6 \
  --rx-freq 915e6 --rx-gain 20 --scheme DQPSK --fec true --bytes-length 125
docker exec -it sdrviz feh viz/DQPSK/figure.png
```
Desktop size: add `-e VNC_RESOLUTION=1920x1080` to `docker run`. Note the viz is file-based
(Agg → PNG), so noVNC is for browsing figures, not a live real-time window.

## Notes / next steps
- **Image size:** this PHY image has no torch. The MARL layer (`agent_node.py`,
  training) will be a separate `sdr-marl` image `FROM sdr-phy:22.04` adding torch.
- **UHD version pinning:** keep `ubuntu:22.04` unless you deliberately want a newer
  UHD — a mismatch with the N210's on-device image can break discovery/streaming.
- Files: `../Dockerfile`, `../Dockerfile.novnc`, `../.dockerignore`, `build.sh`,
  `run-phy.sh`, `run-vnc.sh`, `docker-compose.yml`.
