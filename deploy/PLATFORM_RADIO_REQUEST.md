# Attaching a USRP N210 to a platform-launched container

**To the platform operators.** Our container image is built from your SDR template and
runs correctly on the platform (TurboVNC desktop, code verified). The one missing piece
is the radio: the **N210 at `192.168.10.2`** is not reachable from inside the container.

This is a launcher-side setting, not an image property — no Dockerfile directive can
attach a host NIC. We have run this exact image family against N210s on plain Docker
hosts; the working runtime configuration is in this repo
(`deploy/docker/docker-compose.n210.yml`, `deploy/docker/launch.py`) and translates to
Kubernetes as follows.

## What the pod needs

**Option A — a dedicated interface on the radio subnet (preferred; what we use with
Docker macvlan).** With Multus, a macvlan attachment on the host NIC that is wired to
the N210:

```yaml
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
  name: n210-net
spec:
  config: '{
    "cniVersion": "0.3.1",
    "type": "macvlan",
    "master": "<host NIC wired to the N210, e.g. eth1>",
    "mode": "bridge",
    "ipam": { "type": "static",
              "addresses": [ { "address": "192.168.10.100/24" } ] }
  }'
```
and on the pod: `k8s.v1.cni.cncf.io/networks: n210-net`.

**Option B — host networking** (`hostNetwork: true`). Simpler, exposes every host
interface; acceptable on a single-tenant node.

## UHD also needs (all host/pod-spec side)

| Setting | Why |
|---|---|
| `securityContext: {capabilities: {add: [SYS_NICE]}}` | UHD sets SCHED_FIFO thread priority |
| rtprio 99 / memlock unlimited (CRI defaults or node limits) | same; UHD DMA buffers |
| host sysctls `net.core.rmem_max=50000000`, `wmem_max=2500000` | N210 sample streaming |
| MTU: standard 1500 is fine for N210 | (X310 would want jumbo) |

## How we verify, once attached

Inside the session desktop:
```bash
~/Desktop/check-radio.sh              # interfaces, route, ping, uhd_find_devices
```
Expected: `192.168.10.2` pings, and `uhd_find_devices --args addr=192.168.10.2`
reports the N210. The image already contains UHD 4.1, the compiled modem
(`sdr_system`) and the Python bindings, so no further container-side steps exist.

## Reference

The same attachment on another testbed of this platform family already works with this
image lineage — we are asking for the equivalent configuration here. Our plain-Docker
equivalent, for comparison, is `deploy/docker/docker-compose.n210.yml` in
https://github.com/XYao2021/unionlabs.
