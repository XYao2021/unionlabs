#!/usr/bin/env python3
"""
calibration_plan — one shared file that lets two isolated sessions calibrate a
link between them, each driving only its OWN radio.

The two sessions may be separate pods that cannot reach each other over the
network, but they share /workspace. So calibration is coordinated the way a
multi-node experiment already is (see union/topology.py): a file names the
radios and the link, and each node asks it "which side am I" by looking at the
radios it can actually see. Nobody's radio is ever driven remotely — the plan
is read, not obeyed as a command.

    /workspace/experiments/settings/calibration-plan.json
    {
      "link": {"freq_hz": 915e6, "scheme": "QPSK", "rate": 2e6, "sym": 1e6},
      "rx":   {"serial": "3169C62", "device": "n210", "band": "vert900"},
      "tx":   {"serial": "30CD424", "device": "b210"}
    }

Role is decided by RADIO SERIAL, the one identifier that stays with the
hardware across sessions (a pod hostname does not). A session that sees the rx
serial is the receiver; the tx serial, the transmitter; both, a single host
that runs the whole thing itself; neither, a session with no part in this plan.

    calibration_plan.py --role            # rx | tx | both | none  (for a script)
    calibration_plan.py --emit shell      # CAL_ROLE=.. CAL_FREQ=.. CAL_RX_ARGS=..
    calibration_plan.py --show            # the plan and this node's place in it
    calibration_plan.py --write --rx-serial 3169C62 --tx-serial 30CD424 \\
                        --freq 915e6      # author the plan (fills addr/device
                                          # from discover-node inventory if present)
    calibration_plan.py --self-test
"""
import argparse
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEARCH = ("$UNION_SETTINGS_DIR", "/workspace/experiments/settings",
          os.path.join(REPO, "deploy", "workspace", "settings"))
PLAN = "calibration-plan.json"


def dirs():
    out = []
    for d in SEARCH:
        d = os.path.expandvars(os.path.expanduser(d))
        if d and "$" not in d and d not in out:
            out.append(d)
    return out


def settings_dir(for_write=False):
    """Where the plan and the ready-flag live. For a write, prefer the shared
    /workspace mount so the OTHER session sees it; fall back to the repo copy so
    a laptop still works."""
    cands = dirs()
    if for_write:
        for d in cands:
            parent = os.path.dirname(d.rstrip("/"))
            if os.path.isdir(parent) or os.path.isdir(d):
                return d
    return cands[0] if cands else "."


def find_plan():
    for d in dirs():
        p = os.path.join(d, PLAN)
        if os.path.exists(p):
            return p
    return None


def load_plan():
    p = find_plan()
    if not p:
        return None, None, ("no calibration plan — write one with "
                            "`calibration_plan.py --write ...`, or on the node")
    try:
        with open(p) as fh:
            return json.load(fh), p, None
    except Exception as e:
        return None, p, f"{p}: {e}"


# ── this node's radios ────────────────────────────────────────────────────────
def parse_find_devices(out):
    """uhd_find_devices has no machine mode; parse its 'Device Address' blocks —
    the same text-parse prepare_phy and discover-node use."""
    radios, cur = [], None
    for line in out.splitlines():
        if line.startswith("-- UHD Device"):
            cur = {}
            radios.append(cur)
            continue
        m = re.match(r"\s+(serial|addr|type|name|product|resource):\s*(.*)$", line)
        if m and cur is not None and m.group(2).strip():
            cur[m.group(1)] = m.group(2).strip()
    return [r for r in radios if r]


def _own_discover_record():
    """This pod's discover-node record from the shared workspace, if present.
    discover-node.py publishes it seconds after the session starts, keyed by
    radio serial (or host-<pod> when radioless), so it resolves a role even
    before this process runs its own probe."""
    host = None
    try:
        import socket
        host = socket.gethostname()
    except Exception:
        return []
    for d in dirs():
        for cand in (os.path.join(d, f"host-{host}.json"),):
            try:
                rec = json.load(open(cand))
                return rec.get("radios") or []
            except Exception:
                pass
        # radio-serial-keyed records: any whose host field is this pod
        for pth in glob.glob(os.path.join(d, "*.json")):
            base = os.path.basename(pth)
            if base in (PLAN, "calibration-ready.json") or base.startswith(
                    ("ports-", "phy-")):
                continue
            try:
                rec = json.load(open(pth))
            except Exception:
                continue
            if rec.get("host") == host and rec.get("radios"):
                return rec["radios"]
    return []


def local_radios(_probe=None):
    """Radios THIS session can see. A test injects _probe; a UNION_FAKE_RADIOS
    env var (JSON list) does the same for the shell dry-run path. Otherwise probe
    uhd_find_devices, and fall back to this pod's shared discover-node record when
    no probe is possible (no UHD on the box, or the radio not up yet)."""
    if _probe is not None:
        return _probe
    fake = os.environ.get("UNION_FAKE_RADIOS")
    if fake:
        try:
            return json.loads(fake)
        except Exception:
            return []
    try:
        out = subprocess.run(["uhd_find_devices"], capture_output=True,
                             text=True, timeout=30).stdout
        radios = parse_find_devices(out)
        if radios:
            return radios
    except Exception:
        pass
    return _own_discover_record()


def _serials(radios):
    return {str(r.get("serial")).strip() for r in radios if r.get("serial")}


def role_of(plan, radios):
    """rx | tx | both | none, from which planned serials this node can see."""
    have = _serials(radios)
    rx_s = str((plan.get("rx") or {}).get("serial") or "").strip()
    tx_s = str((plan.get("tx") or {}).get("serial") or "").strip()
    is_rx = bool(rx_s) and rx_s in have
    is_tx = bool(tx_s) and tx_s in have
    if is_rx and is_tx:
        return "both"
    if is_rx:
        return "rx"
    if is_tx:
        return "tx"
    return "none"


# ── the ready flag: RX announces it is listening, TX waits for it ─────────────
def ready_path(plan_path):
    return os.path.join(os.path.dirname(plan_path or settings_dir()),
                        "calibration-ready.json")


def mark_ready(plan_path, note=""):
    p = ready_path(plan_path)
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "note": note}, fh)
    os.replace(tmp, p)
    return p


def clear_ready(plan_path):
    try:
        os.remove(ready_path(plan_path))
    except OSError:
        pass


def ready_seen(plan_path, max_age_s=600):
    p = ready_path(plan_path)
    try:
        rec = json.load(open(p))
    except Exception:
        return False
    # Fresh only: a flag left by a previous calibration must not launch this TX
    # into a receiver that is not up. os.path.getmtime is the wall clock the
    # writer stamped, close enough with a generous window.
    return (time.time() - os.path.getmtime(p)) <= max_age_s


# ── emit the plan as shell, so calibration.sh needs no JSON parsing ───────────
def _radio_args(side):
    """UHD args string for a plan side: an addr wins (network radio), else a
    serial (USB)."""
    if side.get("addr"):
        return f"addr={side['addr']}"
    if side.get("serial"):
        return f"serial={side['serial']}"
    return ""


def emit_shell(plan, role, plan_path):
    link = plan.get("link") or {}
    rx, tx = plan.get("rx") or {}, plan.get("tx") or {}
    kv = {
        "CAL_ROLE": role,
        "CAL_PLAN_PATH": plan_path or "",
        "CAL_READY_PATH": ready_path(plan_path),
        "CAL_FREQ": link.get("freq_hz", ""),
        "CAL_SCHEME": link.get("scheme", ""),
        "CAL_RATE": link.get("rate", ""),
        "CAL_SYM": link.get("sym", ""),
        "CAL_TARGET": plan.get("target", ""),
        "CAL_TIMEOUT": plan.get("timeout", ""),
        "CAL_REPS": plan.get("reps", ""),
        "CAL_RX_ARGS": _radio_args(rx),
        "CAL_RX_DEVICE": rx.get("device", ""),
        "CAL_RX_BAND": rx.get("band", ""),
        "CAL_TX_ARGS": _radio_args(tx),
        "CAL_TX_DEVICE": tx.get("device", ""),
    }
    for k, v in kv.items():
        print(f"{k}={shlex.quote(str(v))}")


# ── author a plan, filling addr/device from discover-node inventory ───────────
def inventory():
    """Every radio any live session has published, {serial: {addr, type, ...}}.
    Lets --write name radios by serial alone and fill in the rest."""
    seen = {}
    for d in dirs():
        for p in sorted(glob.glob(os.path.join(d, "*.json"))):
            base = os.path.basename(p)
            if base in (PLAN, "calibration-ready.json") or base.startswith(
                    ("ports-", "phy-")):
                continue
            try:
                rec = json.load(open(p))
            except Exception:
                continue
            for r in rec.get("radios") or []:
                s = str(r.get("serial") or "").strip()
                if s:
                    seen.setdefault(s, r)
    return seen


def _guess_device(radio):
    # uhd_find_devices reports `type` (usrp2 for N200/N210, x300 for X3x0, b200
    # for B2x0) and sometimes a finer `product`. Map the coarse type to the
    # --device the wrappers know; the lab's usrp2 is an N210.
    t = str(radio.get("product") or radio.get("type") or "").lower()
    for key, dev in (("b2", "b210"), ("n210", "n210"), ("n200", "n210"),
                     ("usrp2", "n210"), ("x310", "x310"), ("x300", "x310"),
                     ("n310", "n310")):
        if key in t:
            return dev
    return None


def build_plan(rx_serial, tx_serial, freq_hz, scheme, rate, sym,
               rx_band=None, target=None, timeout=None, reps=None, inv=None):
    inv = inv if inv is not None else inventory()

    def side(serial):
        r = inv.get(str(serial).strip(), {})
        s = {"serial": str(serial).strip()}
        if r.get("addr"):
            s["addr"] = r["addr"]
        dev = _guess_device(r)
        if dev:
            s["device"] = dev
        return s

    rx = side(rx_serial)
    if rx_band:
        rx["band"] = rx_band
    plan = {
        "schema": 1,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "link": {"freq_hz": float(freq_hz), "scheme": scheme,
                 "rate": float(rate), "sym": float(sym)},
        "rx": rx,
        "tx": side(tx_serial),
    }
    for k, v in (("target", target), ("timeout", timeout), ("reps", reps)):
        if v is not None:
            plan[k] = v
    return plan


def write_plan(plan):
    d = settings_dir(for_write=True)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, PLAN)
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(plan, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)
    return p


def self_test():
    plan = {"link": {"freq_hz": 915e6, "scheme": "QPSK", "rate": 2e6, "sym": 1e6},
            "rx": {"serial": "3169C62", "device": "n210", "addr": "192.168.20.2"},
            "tx": {"serial": "30CD424", "device": "b210"}}
    # role detection from a probed inventory
    assert role_of(plan, [{"serial": "3169C62", "addr": "192.168.20.2"}]) == "rx"
    assert role_of(plan, [{"serial": "30CD424"}]) == "tx"
    assert role_of(plan, [{"serial": "3169C62"}, {"serial": "30CD424"}]) == "both"
    assert role_of(plan, [{"serial": "OTHER"}]) == "none"
    assert role_of(plan, []) == "none"
    # a side with no serial in the plan never matches by accident
    assert role_of({"rx": {}, "tx": {"serial": "30CD424"}},
                   [{"serial": "3169C62"}]) == "none"
    # radio args: addr beats serial, serial when no addr
    assert _radio_args(plan["rx"]) == "addr=192.168.20.2"
    assert _radio_args(plan["tx"]) == "serial=30CD424"
    # build_plan fills addr/device from inventory, by serial alone
    inv = {"3169C62": {"serial": "3169C62", "addr": "192.168.20.2", "type": "usrp2"},
           "30CD424": {"serial": "30CD424", "type": "b200"}}
    p = build_plan("3169C62", "30CD424", 915e6, "QPSK", 2e6, 1e6,
                   rx_band="vert900", inv=inv)
    assert p["rx"]["addr"] == "192.168.20.2" and p["rx"]["device"] == "n210", p["rx"]
    assert p["tx"]["device"] == "b210" and "addr" not in p["tx"], p["tx"]
    assert p["rx"]["band"] == "vert900"
    # the parser reads real uhd_find_devices text
    sample = ("-- UHD Device 0\n"
              "--------------------------------------------------\n"
              "    Device Address:\n"
              "        serial: 3169C62\n"
              "        addr: 192.168.20.2\n"
              "        type: usrp2\n")
    got = parse_find_devices(sample)
    assert got == [{"serial": "3169C62", "addr": "192.168.20.2", "type": "usrp2"}], got
    print("calibration_plan self-test: 8 scenarios checked")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--role", action="store_true", help="print rx|tx|both|none")
    ap.add_argument("--emit", choices=["shell"], help="plan + role as shell assignments")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--write", action="store_true", help="author the plan")
    ap.add_argument("--rx-serial")
    ap.add_argument("--tx-serial")
    ap.add_argument("--freq", type=float, default=915e6)
    ap.add_argument("--scheme", default="QPSK")
    ap.add_argument("--rate", type=float, default=2e6)
    ap.add_argument("--sym", type=float, default=1e6)
    ap.add_argument("--band", default=None)
    ap.add_argument("--target", type=int, default=None)
    ap.add_argument("--timeout", type=float, default=None)
    ap.add_argument("--reps", type=int, default=None)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()

    if a.write:
        if not a.rx_serial or not a.tx_serial:
            ap.error("--write needs --rx-serial and --tx-serial")
        plan = build_plan(a.rx_serial, a.tx_serial, a.freq, a.scheme, a.rate,
                          a.sym, rx_band=a.band, target=a.target,
                          timeout=a.timeout, reps=a.reps)
        p = write_plan(plan)
        print(f"[calibration-plan] wrote {p}")
        for side in ("rx", "tx"):
            s = plan[side]
            filled = "addr/device from inventory" if s.get("addr") or s.get("device") \
                else "serial only — not seen in any live session's inventory yet"
            print(f"    {side}: {s.get('serial')}  ({filled})")
        return 0

    plan, path, why = load_plan()
    if not plan:
        if a.emit == "shell":
            print("CAL_ROLE=none")
            print(f"CAL_WHY={shlex.quote(why)}")
            return 0
        print(f"[calibration-plan] {why}")
        return 1
    role = role_of(plan, local_radios())

    if a.role:
        print(role)
        return 0
    if a.emit == "shell":
        emit_shell(plan, role, path)
        return 0
    # default / --show
    link = plan.get("link") or {}
    print(f"[calibration-plan] {path}")
    print(f"    link: {link.get('freq_hz')} Hz  {link.get('scheme')}  "
          f"{link.get('rate')} S/s")
    print(f"    rx:   {(plan.get('rx') or {}).get('serial')}  "
          f"tx: {(plan.get('tx') or {}).get('serial')}")
    print(f"    this session is: {role.upper()}"
          + {"none": "  (no radio here is in the plan)",
             "both": "  (both radios here — runs the whole thing)"}.get(role, ""))
    return 0 if role != "none" else 1


if __name__ == "__main__":
    sys.exit(main())
