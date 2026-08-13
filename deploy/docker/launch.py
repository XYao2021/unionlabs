#!/usr/bin/env python3
"""
launch.py — reservation-driven container launcher for the SDR testbed.

Reads a reservation JSON (what the testbed gave you: PHY codes + which radios) and
turns each reserved resource into the docker run flags it needs, launches the noVNC
image, and prints the browser link. Exposing an interface / USB is a docker-RUN-time
choice (it can't be baked into the image), so this runs on the host BEFORE the container.

Resource -> what it adds:
    "phy"          -> use the sdr-phy(-vnc) image (already has UHD + sdr_system). no flags.
    N210 / X310    -> macvlan on that device's NIC (exposes ONLY that interface, not all);
                      container gets an IP on the radio subnet, reaches the device by addr.
                      X310 also sets the NIC MTU (jumbo frames) if "mtu" is given.
    B210 / B200    -> USB passthrough (--device /dev/bus/usb).
    (any radio)    -> UHD real-time scheduling: --cap-add=SYS_NICE --ulimit rtprio=99
                      --ulimit memlock=-1  (fixes "error in pthread_setschedparam").
    noVNC          -> publish <novnc_port>:6080 on the host; the sdr-phy-vnc entrypoint
                      auto-starts Xvfb+x11vnc+noVNC, so the desktop is up on launch.

Usage:
    python3 docker/launch.py docker/reservation.example.json            # launch
    python3 docker/launch.py my_resv.json --dry-run                      # just print the commands
    python3 docker/launch.py my_resv.json --sudo                         # prefix every docker call with sudo
    python3 docker/launch.py my_resv.json --stop                         # stop + remove this reservation's container
"""
import argparse
import json
import re
import socket
import subprocess
import sys


ETH_TYPES = {"N210", "X310", "N310", "USRP2"}
USB_TYPES = {"B210", "B200", "B205", "B205MINI"}


def sh(cmd, docker, dry, check=True, capture=False):
    """Run a docker command (list of args after the docker binary)."""
    full = list(docker) + cmd
    print("  $ " + " ".join(full))
    if dry:
        return ""
    r = subprocess.run(full, text=True,
                       stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.STDOUT if capture else None)
    if check and r.returncode != 0:
        if capture:
            print(r.stdout)
        sys.exit("[launch] command failed (rc=%d)" % r.returncode)
    return (r.stdout or "") if capture else ""


def host_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def sanitize(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(name))


def net_exists(name, docker, dry):
    if dry:
        return False
    r = subprocess.run(list(docker) + ["network", "inspect", name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("reservation", help="path to the reservation JSON")
    ap.add_argument("--dry-run", action="store_true", help="print the docker commands, run nothing")
    ap.add_argument("--sudo", action="store_true", help="prefix docker calls with sudo")
    ap.add_argument("--stop", action="store_true", help="stop + remove this reservation's container")
    args = ap.parse_args()

    with open(args.reservation) as f:
        rv = json.load(f)

    docker = (["sudo", "docker"] if args.sudo else ["docker"])
    dry = args.dry_run
    name = "sdrlab_" + sanitize(rv.get("reservation", "default"))
    image = rv.get("image", "sdr-phy-vnc:22.04")
    novnc_port = int(rv.get("novnc_port", 6080))
    devices = rv.get("devices", [])

    if args.stop:
        print("[launch] stopping reservation '%s' (container %s)" % (rv.get("reservation"), name))
        sh(["rm", "-f", name], docker, dry, check=False)
        return

    print("[launch] reservation '%s' -> container '%s' (image %s)"
          % (rv.get("reservation"), name, image))

    # ── classify devices ──
    eth = [d for d in devices if d.get("type", "").upper() in ETH_TYPES]
    usb = [d for d in devices if d.get("type", "").upper() in USB_TYPES]
    for d in devices:
        t = d.get("type", "").upper()
        if t not in ETH_TYPES and t not in USB_TYPES:
            print("  [warn] unknown device type %r — ignored" % d.get("type"))

    # ── 1) create a macvlan per Ethernet radio NIC (idempotent) ──
    for d in eth:
        iface = d["iface"]
        subnet = d.get("subnet", "192.168.10.0/24")
        mvname = "mv_" + sanitize(iface)
        if d.get("mtu"):     # X310 jumbo frames — set the NIC MTU (best effort)
            sh(["run", "--rm", "--net=host", "--privileged", image,
                "ip", "link", "set", iface, "mtu", str(d["mtu"])], docker, dry, check=False)
        if not net_exists(mvname, docker, dry):
            # macvlan inherits the parent NIC's MTU (jumbo frames set on the NIC above).
            sh(["network", "create", "-d", "macvlan", "--subnet=" + subnet,
                "-o", "parent=" + iface, mvname], docker, dry, check=False)
        else:
            print("  [ok] macvlan %s already exists" % mvname)

    # ── 2) docker run on the default bridge (published noVNC port) ──
    run = ["run", "-d", "--name", name,
           "-p", "%d:6080" % novnc_port,
           "--cap-add=SYS_NICE", "--ulimit", "rtprio=99", "--ulimit", "memlock=-1",
           "-e", "VNC_RESOLUTION=" + str(rv.get("vnc_resolution", "1440x900"))]
    for k, v in (rv.get("env") or {}).items():
        run += ["-e", "%s=%s" % (k, v)]
    if usb:
        run += ["--device", "/dev/bus/usb"]      # B210 over USB
    run += [image]
    sh(run, docker, dry)

    # ── 3) attach each Ethernet radio's macvlan as an extra interface ──
    for d in eth:
        mvname = "mv_" + sanitize(d["iface"])
        connect = ["network", "connect"]
        if d.get("container_ip"):
            connect += ["--ip", d["container_ip"]]
        connect += [mvname, name]
        sh(connect, docker, dry)

    # ── 4) optional: install extra apt libs the reservation asked for ──
    apt = rv.get("apt") or []
    if apt:
        sh(["exec", name, "bash", "-lc",
            "apt-get update && apt-get install -y " + " ".join(sanitize(p) for p in apt)],
           docker, dry, check=False)

    # ── summary + the noVNC link ──
    ip = host_ip()
    print("\n[launch] READY — reservation '%s'" % rv.get("reservation"))
    for d in eth:
        print("   %-6s addr=%s  via iface %s (macvlan, isolated)" % (d.get("type"), d.get("addr"), d["iface"]))
    for d in usb:
        print("   %-6s serial=%s  (USB passthrough)" % (d.get("type"), d.get("serial")))
    print("\n   noVNC desktop:  http://%s:%d/vnc.html" % (ip, novnc_port))
    print("   shell into it:  %s exec -it %s bash" % (" ".join(docker), name))
    print("   stop/cleanup :  python3 %s %s --stop%s"
          % (sys.argv[0], args.reservation, " --sudo" if args.sudo else ""))


if __name__ == "__main__":
    main()
