# Dockerizing the SDR PHY — build & usage guide

This documents the **`sdr-phy`** container: a reproducible Linux image that builds and
runs the C++ USRP PHY (`build/sdr_system`) with UHD and the thin Python API. It is the
single entry point for building on your Mac and running on the lab's Linux hosts.

> **Scope:** PHY only — the C++ radio stack + UHD + the SDR Python wrapper. It does **not**
> include torch or the MARL brain; that will be a separate `sdr-marl` image layered on top
> (`FROM sdr-phy`) later.

---

## 0. The one-command image (`unionlabs`)

**If you just want it running, use this one.** A single self-contained image that
**downloads the platform itself**, runs `deploy/initialization.sh`, and serves a
desktop over **noVNC with no password**. No local checkout needed to build it.

```bash
docker/build-unionlabs.sh            # build (clones the repo inside the image)
docker/run-unionlabs.sh              # start it; prints the link
```

Then open the URL it prints:

```
http://<host>:6080/vnc.html?autoconnect=1&resize=scale
```

It opens straight into a desktop with a terminal already in `/opt/unionlabs` — no
password prompt, because the VNC server runs with `-nopw`.

| | |
|---|---|
| `build-unionlabs.sh --minimal` | skip torch/networkx/opencv — a much smaller image |
| `build-unionlabs.sh --with-phy` | also compile `sdr_system` + `pyphy` (slow; for a real radio) |
| `build-unionlabs.sh --ref <branch>` | build a branch, tag or commit instead of `main` |
| `build-unionlabs.sh --amd64` | cross-build for x86_64 lab hosts from an arm64 Mac |
| `run-unionlabs.sh --usb` | pass through a B210 |
| `run-unionlabs.sh --host-net` | host networking, for an N210 (Linux) |
| `run-unionlabs.sh --port 6081` | serve the browser on another port |
| `run-unionlabs.sh --refresh` | `git pull` inside the container at start |
| `run-unionlabs.sh --stop` | stop and remove |

Inside, `./run.sh selftest` confirms the install, exactly as on a laptop.

**How it differs from the two images below:** they `COPY` your local working tree and
build in two steps (`sdr-phy`, then `sdr-phy-vnc` on top). This one clones from GitHub
in a single step, so it is reproducible from nothing and does not depend on the state
of your checkout — which is what you want for a shared testbed or a demo machine.

> **No password is deliberate.** Anyone who can reach port 6080 gets a full desktop in
> the container. Keep it on a trusted network or a Tailscale interface; do not publish
> 6080 to the open internet.

### If the browser says "Failed to connect to server"

That message comes from the **noVNC client**, so the page itself was served — the HTTP
half worked and only the WebSocket to `/websockify` failed. Two very different causes
produce it, so start by reading the container:

```bash
docker logs unionlabs | tail -20
```

The startup script now refuses to serve a desktop it cannot back: if Xvfb or x11vnc
never comes up it exits with `[unionlabs] FATAL: …` and the tail of the relevant log
(`/tmp/xvfb.log`, `/tmp/x11vnc.log`, `/tmp/websockify.log`) rather than leaving a page
that can only fail in the browser.

- **A `FATAL` line** — the fault is inside the container. `Xvfb never created
  /tmp/.X11-unix/X0` means it cannot write `/tmp`: a read-only rootfs, a volume mounted
  over `/tmp`, or a platform that started the container as a different user.
- **No `FATAL`, everything listening** — the fault is the hop in between. Confirm the
  bridge is healthy on the host, where `101` is the answer you want:

  ```bash
  curl -si -o /dev/null -w '%{http_code}\n' \
    -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
    -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
    http://localhost:6080/websockify
  ```

  `101` locally but a failure through a portal means that front end is not forwarding the
  WebSocket upgrade (an AWS ALB does; CloudFront needs the right policy; an API Gateway
  HTTP API cannot). Under a path prefix, tell noVNC where the socket really is:
  `…/vnc.html?path=<prefix>/websockify&autoconnect=1`. To take the proxy out of the
  picture entirely, tunnel instead: `ssh -N -L 6080:localhost:6080 <user>@<host>`.

Long-lived sessions get a `--heartbeat=30` ping, because load balancers drop silent
WebSockets — an AWS ALB idles out at 60 s — which otherwise looks like the desktop
freezing for no reason.

---

## 1. What we built

| Artifact | Path | What it is |
|---|---|---|
| Image definition | `Dockerfile` | Ubuntu 22.04 + UHD + build deps → compiles `sdr_system` in-image; bakes the UHD FPGA/firmware images; installs numpy (no torch). |
| Context filter | `.dockerignore` | Keeps the build context small and — critically — excludes the host `build/` so the binary is compiled for the image's arch (no "Exec format error"). |
| Cross-build script | `docker/build.sh` | Builds `linux/amd64` from the arm64 Mac via buildx+QEMU; `--export` writes a transferable tarball. |
| Run wrapper | `docker/run-phy.sh` | Runs the container on a Linux host with USRP access (`--network host` for N210, `USB=1` for B210) and sets UHD socket buffers. |
| Compose (host net) | `docker/docker-compose.yml` | A single long-lived `phy` service you `exec` into (`--network host`). |
| Compose (isolated) | `docker/docker-compose.n210.yml` | Exposes **only** the N210 NIC (macvlan) + default bridge; also sets UHD RT scheduling. See §6b. |
| Visualization image | `Dockerfile.novnc` | `FROM sdr-phy` + a GUI desktop served over **noVNC** (browser at `:6080`) to view figures / run matplotlib; adds matplotlib. See §8. |
| VNC run wrapper | `docker/run-vnc.sh` | Runs the `sdr-phy-vnc` image and prints the browser URL. |
| Reservation launcher | `docker/launch.py` + `docker/reservation.example.json` | Reads a testbed reservation JSON, maps each resource to docker flags (macvlan/USB/caps), launches the noVNC image, prints the link. See §8b. |
| Quick reference | `docker/README.md` | Short version of this guide, inside the docker/ folder. |

### What's inside the image
- **Base:** `ubuntu:22.04` — pinned to match the lab hosts (**UHD 4.1.0.5, g++ 11, Boost 1.74**),
  so the container's UHD matches the N210's on-device image.
- **Build/runtime deps (apt):** `build-essential cmake pkg-config libuhd-dev uhd-host
  libboost-all-dev libfftw3-dev libvolk2-dev python3 python3-pip iproute2 iputils-ping`.
- **UHD images:** `uhd_images_downloader` runs at build time. A **B210 loads its FX3/FPGA
  image from here every open**; the N210 keeps its image on-device (so it's a no-op for N210
  but required for USB B210s).
- **Python:** `numpy==2.1.3` only (for the sensing / `sdr.py` helpers). **No torch.**
- **Built binary:** `sdr_system` at `/opt/sdr/unionlabs/build/sdr_system`, also on `PATH`
  and pointed to by `SDR_SYSTEM_BIN` (the Python wrapper reads that env var).

### Why containerize
It removes the exact problems we hit bare-metal:
- No `/opt/anaconda3/.../cmake not found` — a clean apt toolchain, **no conda**.
- No **"Exec format error"** — the binary is compiled for the image, never copied across arches.
- One command bakes in the UHD firmware images.
- Reproducible: same UHD/Boost/FFTW on every host.

---

## 2. Prerequisites
- **Build:** Docker Desktop on your Mac (arm64). It cross-builds `linux/amd64` via buildx+QEMU.
- **Run:** a **Linux** host with the USRP attached. Host networking and USB passthrough do **not**
  work under Docker Desktop's macOS VM, so all real runs happen on the lab boxes.

---

## 3. Build (on the Mac)

```bash
cd unionlabs
docker/build.sh                 # -> image sdr-phy:22.04 (linux/amd64)
docker/build.sh --export        # also writes sdr-phy_*.tgz to transfer
```

Override the tag/platform if needed:
```bash
IMAGE=sdr-phy:dev PLATFORM=linux/amd64 docker/build.sh
```

If you're already **on a Linux host**, a native build is much faster:
```bash
docker build -t sdr-phy:22.04 .     # run from the unionlabs/ dir
```

> The cross-build compiles `sdr_system` under emulation and downloads the UHD images, so the
> first build takes a while. `uhd_images_downloader` needs internet during the build.

---

## 4. Ship to a lab host (if built on the Mac)

```bash
# on the Mac (after --export):
scp sdr-phy_sdr-phy__22.04.tgz  user@ece-d6114-lnx01:~/

# on the Linux host:
gunzip -c sdr-phy_sdr-phy__22.04.tgz | docker load
```
(Or push/pull through a registry if you have one.)

---

## 5. Run on the Linux host

The wrapper handles the flags; run it on the **host that has the radio**.

```bash
# N210 (Ethernet) — host networking:
docker/run-phy.sh uhd_find_devices --args addr=192.168.20.2

# Interactive shell (lands in the PHY dir):
docker/run-phy.sh

# B210 (USB) — add USB passthrough:
USB=1 docker/run-phy.sh uhd_usrp_probe
```

Or keep it up with compose and exec in:
```bash
docker compose -f docker/docker-compose.yml up -d phy
docker compose -f docker/docker-compose.yml exec phy bash
```

**UHD socket buffers** (`net.core.*` are not namespaced) — set once on the **host**
(`run-phy.sh` attempts this automatically):
```bash
sudo sysctl -w net.core.rmem_max=50000000 net.core.wmem_max=2500000
```

### Example — the N210 RX warm decoder (same as bare-metal)
```bash
docker/run-phy.sh ./sdr_system --role rx --rx-args addr=192.168.20.2 \
  --rx-subdev A:0 --rx-ant RX2 --tx-rate 2e6 --rx-rate 2e6 --symbol_rate 1e6 \
  --rx-freq 915e6 --rx-gain 20 --scheme DQPSK --fec true --marl-report true \
  --stop-on-complete false --rx-idle-timeout 0 --viz false --bytes-length 125
```

---

## 6. Validate (in order)

```bash
# a) Radio-free — proves the build + Python env, needs no hardware:
docker/run-phy.sh python3 python/ap_multi.py --sim-test

# b) UHD sees the N210 from inside the container:
docker/run-phy.sh uhd_find_devices --args addr=192.168.20.2

# c) A real PHY link (see the RX example above).
```
A green (a) means the image is sound; (b) proves device reachability; (c) is the real radio.

---

## 6b. Restrict network exposure (expose only the N210 NIC) + UHD RT scheduling

`--network host` is the simplest way to reach the N210, but it exposes **every** host
interface to the container. To expose **only** the N210's Ethernet NIC plus the default
bridge, use a **macvlan** network (a run-time choice — the Dockerfile's `EXPOSE` is just
documentation, it does not bind interfaces). Encoded in `docker/docker-compose.n210.yml`:

```bash
# 1) find the NIC wired to the N210 (holds a 192.168.20.x address):
ip -o -4 addr show | grep 192.168.20            # e.g. enp3s0

# 2) bring it up with ONLY that NIC (macvlan) + the default bridge:
N210_IFACE=enp3s0 docker compose -f docker/docker-compose.n210.yml up -d phy
docker compose -f docker/docker-compose.n210.yml exec phy uhd_find_devices --args addr=192.168.20.2
docker compose -f docker/docker-compose.n210.yml exec phy bash
```
The container gets an IP on the N210 subnet (default `192.168.20.100`) and reaches the radio
by unicast (we always address `addr=192.168.20.2`, so broadcast discovery isn't needed); the
control ports `5599`/`5600` are published via the bridge; the host's other interfaces are not
visible. Equivalent manual form:
```bash
sudo docker network create -d macvlan --subnet=192.168.20.0/24 -o parent=enp3s0 n210net
sudo docker run -d --name sdrphy -p 5599:5599 -p 5600:5600 \
  --cap-add=SYS_NICE --ulimit rtprio=99 --ulimit memlock=-1 sdr-phy:22.04 sleep infinity
sudo docker network connect --ip 192.168.20.100 n210net sdrphy
sudo docker exec -it sdrphy bash
```
Caveats: macvlan needs the parent NIC in promiscuous mode (usual on a dedicated radio NIC),
and by design the **host cannot talk to the container over that subnet** (fine — the radio
does). Test `uhd_find_devices --args addr=192.168.20.2` first.

### UHD real-time thread priority (`pthread_setschedparam`)
Inside a container UHD can't get RT scheduling by default, so you'll see
`error in pthread_setschedparam`. Fix it by granting the privilege on `docker run`
(the `run-phy.sh` wrapper and both compose files already do this):
```bash
--cap-add=SYS_NICE --ulimit rtprio=99 --ulimit memlock=-1
```
That eliminates the error. To merely **silence** it instead (not recommended over fixing it):
```bash
-e UHD_LOG_CONSOLE_LEVEL=error
```

## 7. Troubleshooting (issues we already hit)

| Symptom | Cause / fix |
|---|---|
| `Exec format error: .../sdr_system` | Wrong-arch binary. In-container it's always rebuilt for the image; make sure `.dockerignore` excludes `build/` and you didn't `docker cp` a Mac binary in. |
| `/opt/anaconda3/.../cmake: not found` | Bare-metal conda leak — **does not happen in the container** (clean apt cmake). |
| `incomplete type 'std::atomic<bool>'` at build | The `<atomic>` force-include must be in `CMakeLists.txt` (portability fix). If it recurs, add it there and rebuild. |
| `uhd_find_devices` finds nothing | The container isn't on the device's network. Use `--network host` (the wrapper does) and run on the host cabled to the N210. |
| N210 rate "snapped" / `[CONSISTENCY] FAIL` | Use an N210-exact rate: `--tx-rate 2e6 --rx-rate 2e6 --symbol_rate 1e6` (1.6e6 snaps on the N210's 100 MHz clock). Every process needs `tx_rate` set. |
| UHD overflow / underrun spam | Raise host socket buffers (section 5). |
| `error in pthread_setschedparam` | UHD can't get RT scheduling in the container. Add `--cap-add=SYS_NICE --ulimit rtprio=99 --ulimit memlock=-1` (§6b). To just silence it: `-e UHD_LOG_CONSOLE_LEVEL=error`. |
| Want to expose only the N210 NIC, not all interfaces | Use `docker/docker-compose.n210.yml` (macvlan on the radio NIC + default bridge) instead of `--network host` (§6b). |
| B210 not detected over USB | Add `USB=1` (or `--device /dev/bus/usb`, or `privileged: true` in compose); confirm the `uhd_images_downloader` layer is present. |

---

## 8. Visualization variant — `sdr-phy-vnc` (noVNC in the browser)

`Dockerfile.novnc` layers a lightweight GUI desktop on the PHY image and serves it over
**noVNC**, so you can view the PHY's figures (constellation / spectrum PNGs from
`tools/plot_viz.py`) and run matplotlib in a browser — useful when the container is on a
headless lab host. It also adds **matplotlib** (the base PHY image only has numpy), which the
`--viz` auto-plot needs.

Stack: `Xvfb` (virtual display) → `fluxbox` (WM) → `x11vnc` → `websockify`/`novnc` on port
**6080**. `feh` views figures; `python3-tk` enables interactive matplotlib.

**Build** (needs `sdr-phy:22.04` first, since it's `FROM` it):
```bash
docker build -t sdr-phy:22.04 .                                # base, if not built
docker build -f Dockerfile.novnc -t sdr-phy-vnc:22.04 .        # native
# from the Mac (arm64 -> amd64):
docker buildx build --platform linux/amd64 -f Dockerfile.novnc -t sdr-phy-vnc:22.04 --load .
```

**Run** (Linux host), then open `http://<host>:6080/vnc.html` (no password):
```bash
docker/run-vnc.sh                 # figures only (publishes :6080)
USRP=1 docker/run-vnc.sh          # + N210 (host networking; 6080 on the host)
USRP=1 USB=1 docker/run-vnc.sh    # + B210 over USB
```

**Use it:**
```bash
# generate a figure (matplotlib now present), then view it in the browser desktop:
docker exec -it sdrviz ./sdr_system --role rx --rx-args addr=192.168.20.2 \
  --rx-subdev A:0 --rx-ant RX2 --tx-rate 2e6 --rx-rate 2e6 --symbol_rate 1e6 \
  --rx-freq 915e6 --rx-gain 20 --scheme DQPSK --fec true --bytes-length 125   # writes viz/DQPSK/figure.png
docker exec -it sdrviz feh viz/DQPSK/figure.png
```
Tune the desktop size with `-e VNC_RESOLUTION=1920x1080` on `docker run`.

## 8b. Reservation-driven launch (`launch.py` + JSON) — noVNC in one command

Instead of hand-writing docker flags, declare what the testbed reserved in a JSON file and
let `docker/launch.py` map each resource to the flags it needs, start the **noVNC** image,
and print the browser link. Exposing an interface / USB is a `docker run`-time choice, so
this runs on the host.

`docker/reservation.example.json`:
```json
{
  "reservation": "demo-001", "image": "sdr-phy-vnc:22.04", "novnc_port": 6080,
  "components": ["phy"],
  "devices": [
    {"type": "N210", "addr": "192.168.20.2", "iface": "enx144fd7da6276",
     "subnet": "192.168.20.0/24", "container_ip": "192.168.20.100"},
    {"type": "B210", "serial": "30CD424"},
    {"type": "X310", "addr": "192.168.40.2", "iface": "ens6",
     "subnet": "192.168.40.0/24", "container_ip": "192.168.40.100", "mtu": 9000}
  ],
  "env": {"UHD_LOG_CONSOLE_LEVEL": "info"}, "apt": []
}
```

What each entry maps to:
| reservation entry | docker config added |
|---|---|
| `"phy"` | use the `sdr-phy(-vnc)` image (already has UHD + `sdr_system`) |
| N210 / X310 | **macvlan on that NIC** (exposes ONLY that interface); container IP on the radio subnet; X310 sets jumbo MTU |
| B210 / B200 | `--device /dev/bus/usb` |
| any radio | `--cap-add=SYS_NICE --ulimit rtprio=99 --ulimit memlock=-1` (UHD RT scheduling) |
| noVNC | `-p <novnc_port>:6080`; the image auto-starts the desktop |
| `"env"` / `"apt"` | `-e KEY=VAL`; optional apt installs post-start |

Run it (on the Linux host):
```bash
python3 docker/launch.py my_reservation.json --dry-run --sudo   # preview the exact commands
python3 docker/launch.py my_reservation.json --sudo             # launch
python3 docker/launch.py my_reservation.json --stop --sudo      # stop + remove
```
It prints, on success, the desktop link `http://<host>:6080/vnc.html`, a shell-in command, and the
stop command. (`--sudo` prefixes every docker call, since docker needs root on the lab hosts.)

## 9. Next step (not built yet)
The MARL layer (`agent_node.py` + training, which need torch) will be a separate
**`sdr-marl`** image `FROM sdr-phy:22.04`, so the N210 AP/PHY side stays small. Scaffold it
when you return to the MARL work.
