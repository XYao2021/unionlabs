#!/usr/bin/env python3
"""
link_setup — turn one hand-written link file into the radio.sh command THIS box
should run, with the receive-side detector settings resolved from its survey.

A point-to-point ARQ link has ~15 coupled parameters, and RF-ACK doubles them (a
data path AND an ACK path, each with its own freq / subdev / antenna / gain). Typed
by hand per box they drift — a source dials 5330 while the sink listens on 5340, or
the ACK RX carries no det-mult because radio.sh only resolves the PRIMARY path's
profile, never the appended ACK path. This reads the link once and emits each box's
whole command line, so neither box can disagree with the other.

    /workspace/experiments/settings/link.json   (hand-written for now)
    {
      "phy":  {"scheme":"QPSK","waveform":"sc","rate":2e6,"symbol_rate":1e6,
               "fec":"true","interval_ms":1000},
      "data": {"freq_hz": 5330e6, "band": "vert2450-5g"},
      "ack":  {"transport":"rf","freq_hz": 5430e6, "band": "vert2450-5g"},
      "roles": {
        "source": {"serial":"315F2FB","device":"b210",
                   "data":{"subdev":"A:A","ant":"TX/RX","gain":78},
                   "ack": {"subdev":"A:B","ant":"RX2","gain":60}},
        "sink":   {"serial":"327D82F","device":"x310","args":"addr=192.168.30.2",
                   "data":{"subdev":"A:0","ant":"RX2","gain":25},
                   "ack": {"subdev":"A:0","ant":"TX/RX","gain":31.5}}
      }
    }

Role is decided by RADIO SERIAL, the identifier that stays with the hardware
(a pod hostname does not) -- the same rule calibration_plan uses. The source
TRANSMITS data and RECEIVES the ACK; the sink RECEIVES data and TRANSMITS the ACK.
So each box's RECEIVE path is the one that needs a survey:

    sink   receives DATA -> survey the data band, det-mult/sync-threshold apply there
    source receives ACK  -> survey the ack  band, det-mult/sync-threshold apply there

    link_setup.py --role                 # source | sink | none
    link_setup.py --rx-band              # the band THIS box must survey
    link_setup.py --emit shell           # LINK_* vars for auto_link.sh
    link_setup.py --emit radio-cmd       # the radio.sh args for THIS box
    link_setup.py --self-test
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEARCH = ("$UNION_SETTINGS_DIR", "/workspace/experiments/settings",
          os.path.join(REPO, "deploy", "workspace", "settings"))
LINK = "link.json"


def dirs():
    out = []
    for d in SEARCH:
        d = os.path.expandvars(os.path.expanduser(d))
        if d and "$" not in d and d not in out:
            out.append(d)
    return out


def load_link():
    for d in dirs():
        p = os.path.join(d, LINK)
        if os.path.exists(p):
            try:
                return json.load(open(p)), p, None
            except Exception as e:
                return None, p, f"{p}: {e}"
    return None, None, ("no link.json — copy "
                        "deploy/workspace/settings/link.template.json to "
                        "/workspace/experiments/settings/link.json and fill it in")


# ── this box's radios (serial), for role detection ────────────────────────────
def local_serials(_probe=None):
    if _probe is not None:
        return set(_probe)
    fake = os.environ.get("UNION_FAKE_RADIOS")
    if fake:
        try:
            return {str(r.get("serial")).strip() for r in json.loads(fake)
                    if r.get("serial")}
        except Exception:
            return set()
    try:
        out = subprocess.run(["uhd_find_devices"], capture_output=True,
                             text=True, timeout=30).stdout
    except Exception:
        return set()
    return {m.group(1).strip() for m in re.finditer(r"serial:\s*(\S+)", out)}


def role_of(link, serials):
    """'source' | 'sink' | 'none', by which planned serial this box can see."""
    for role in ("source", "sink"):
        s = str((link.get("roles", {}).get(role, {}) or {}).get("serial") or "").strip()
        if s and s in serials:
            return role
    return "none"


# ── which path each role transmits / receives ─────────────────────────────────
def _paths(role):
    """(data_dir, ack_dir) for this role: 'tx' or 'rx' for each."""
    return ("tx", "rx") if role == "source" else ("rx", "tx")   # source TX data, RX ack


def rx_band(link, role):
    """The band THIS box RECEIVES on -> the one it must survey."""
    data_dir, _ = _paths(role)
    return (link.get("data") if data_dir == "rx" else link.get("ack") or {}).get("band")


def _radio_args(side):
    if side.get("args"):
        return side["args"]
    if side.get("addr"):
        return f"addr={side['addr']}"
    if side.get("serial"):
        return f"serial={side['serial']}"
    return ""


def resolve_rx_detector(args, band, subdev, ant, freq_hz):
    """(det_mult, sync_threshold) for THIS box's RX path, from its survey profile.
    radio.sh resolves the PRIMARY path this way; for the ACK RX (an appended path)
    nothing does, so we do it here and the caller appends the flags."""
    try:
        sys.path.insert(0, os.path.join(REPO, "union"))
        import phy_profile
    except Exception:
        return None, None
    try:
        vals, path, why = phy_profile.load(
            args=args, band=band, subdev=subdev, ant=ant,
            near_mhz=(float(freq_hz) / 1e6 if freq_hz else None))
    except Exception:
        return None, None
    return vals.get("det_mult"), vals.get("sync_threshold")


def radio_cmd(link, role, resolve=True):
    """The radio.sh argument list for THIS box. The RX path carries the resolved
    det-mult/sync-threshold; the ACK path is added only when transport == rf."""
    r = link["roles"][role]
    phy = link.get("phy", {})
    data, ack = link.get("data", {}), link.get("ack", {})
    data_dir, ack_dir = _paths(role)
    args = _radio_args(r)
    rf = str(ack.get("transport", "tcp")).lower() == "rf"

    # radio.sh mode = the DATA direction (its primary path)
    mode = "tx" if data_dir == "tx" else "rx"
    cmd = [mode, "--device", str(r.get("device", "")), "--args", args]

    dp, ap = r.get("data", {}), r.get("ack", {})
    # DATA path -> the wrapper's primary flags
    cmd += [f"--{data_dir}-freq", _hz(data.get("freq_hz")),
            f"--{data_dir}-subdev", dp.get("subdev", ""),
            f"--{data_dir}-ant", dp.get("ant", ""),
            f"--{data_dir}-gain", str(dp.get("gain", ""))]
    for k, v in (("scheme", phy.get("scheme")), ("waveform", phy.get("waveform")),
                 ("fec", phy.get("fec"))):
        if v is not None:
            cmd += [f"--{k}", str(v)]
    if phy.get("rate") is not None:
        cmd += ["--tx-rate", _hz(phy["rate"]), "--rx-rate", _hz(phy["rate"])]
    if phy.get("symbol_rate") is not None:
        cmd += ["--symbol_rate", _hz(phy["symbol_rate"])]
    if phy.get("interval_ms") is not None:
        cmd += ["--interval", str(phy["interval_ms"])]

    # ARQ role + ACK transport
    cmd += ["--role", ("source_arq" if role == "source" else "sink_arq")]
    if rf:
        cmd += ["--ack-transport", "rf",
                f"--{ack_dir}-args", args,
                f"--{ack_dir}-freq", _hz(ack.get("freq_hz")),
                f"--{ack_dir}-subdev", ap.get("subdev", ""),
                f"--{ack_dir}-ant", ap.get("ant", ""),
                f"--{ack_dir}-gain", str(ap.get("gain", ""))]
    else:
        cmd += ["--ack-transport", "tcp"]
        if role == "source":
            cmd += ["--ack-host", str(ack.get("host", "127.0.0.1")),
                    "--ack-port", str(ack.get("port", 5599))]
        else:
            cmd += ["--ack-port", str(ack.get("port", 5599))]

    # det-mult / sync-threshold for whichever path RECEIVES, resolved from its survey
    if resolve:
        if ack_dir == "rx" and rf:              # source: ACK RX needs the profile
            band, side, freq = ack.get("band"), ap, ack.get("freq_hz")
        elif data_dir == "rx":                  # sink: DATA RX
            band, side, freq = data.get("band"), dp, data.get("freq_hz")
        else:
            band, side, freq = None, {}, None
        if band:
            dm, st = resolve_rx_detector(args, band, side.get("subdev"),
                                         side.get("ant"), freq)
            if dm is not None:
                cmd += ["--det-mult", _num(dm)]
            if st is not None:
                cmd += ["--sync-threshold", _num(st)]
    return cmd


def _hz(v):
    return "" if v is None else f"{float(v):g}"


def _num(v):
    return f"{float(v):g}"


def self_test():
    link = {
        "phy": {"scheme": "QPSK", "waveform": "sc", "rate": 2e6, "symbol_rate": 1e6,
                "fec": "true", "interval_ms": 1000},
        "data": {"freq_hz": 5330e6, "band": "vert2450-5g"},
        "ack": {"transport": "rf", "freq_hz": 5430e6, "band": "vert2450-5g"},
        "roles": {
            "source": {"serial": "315F2FB", "device": "b210",
                       "data": {"subdev": "A:A", "ant": "TX/RX", "gain": 78},
                       "ack": {"subdev": "A:B", "ant": "RX2", "gain": 60}},
            "sink": {"serial": "327D82F", "device": "x310", "args": "addr=192.168.30.2",
                     "data": {"subdev": "A:0", "ant": "RX2", "gain": 25},
                     "ack": {"subdev": "A:0", "ant": "TX/RX", "gain": 31.5}}},
    }
    assert role_of(link, {"315F2FB"}) == "source"
    assert role_of(link, {"327D82F"}) == "sink"
    assert role_of(link, {"NOPE"}) == "none"
    # the band each box must survey is its RECEIVE band
    assert rx_band(link, "sink") == "vert2450-5g"       # sink receives data
    assert rx_band(link, "source") == "vert2450-5g"     # source receives ack

    src = radio_cmd(link, "source", resolve=False)
    assert src[0] == "tx" and "source_arq" in src and "--ack-transport" in src
    # source: data TX on RF A @5330, ACK RX on RF B @5430
    assert _after(src, "--tx-freq") == "5.33e+09"
    assert _after(src, "--rx-freq") == "5.43e+09"
    assert _after(src, "--tx-subdev") == "A:A" and _after(src, "--rx-subdev") == "A:B"
    assert _after(src, "--role") == "source_arq"

    snk = radio_cmd(link, "sink", resolve=False)
    assert snk[0] == "rx" and _after(snk, "--role") == "sink_arq"
    # sink: data RX on RF A @5330, ACK TX on RF B @5430 -> frequencies MIRROR the source
    assert _after(snk, "--rx-freq") == "5.33e+09"       # == source --tx-freq (data)
    assert _after(snk, "--tx-freq") == "5.43e+09"       # == source --rx-freq (ack)
    assert _after(snk, "--rx-ant") == "RX2" and _after(snk, "--tx-ant") == "TX/RX"

    # tcp ACK: source gets --ack-host, sink gets --ack-port, no rf flags
    link2 = json.loads(json.dumps(link))
    link2["ack"] = {"transport": "tcp", "host": "siteB", "port": 5599}
    s2 = radio_cmd(link2, "source", resolve=False)
    assert "--ack-transport" in s2 and _after(s2, "--ack-transport") == "tcp"
    assert _after(s2, "--ack-host") == "siteB"
    assert "rf" not in [s2[i+1] for i, t in enumerate(s2) if t == "--ack-transport"][1:]
    print("link_setup self-test: 6 scenarios checked")
    return 0


def _after(lst, opt):
    return lst[lst.index(opt) + 1] if opt in lst and lst.index(opt) + 1 < len(lst) else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--role", action="store_true")
    ap.add_argument("--rx-band", action="store_true")
    ap.add_argument("--emit", choices=["shell", "radio-cmd"])
    ap.add_argument("--no-resolve", action="store_true",
                    help="skip profile resolution of det-mult/sync-threshold")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()

    link, path, why = load_link()
    if not link:
        if a.emit == "shell":
            print("LINK_ROLE=none")
            print(f"LINK_WHY={_q(why)}")
            return 0
        print(f"[link] {why}", file=sys.stderr)
        return 1
    role = role_of(link, local_serials())

    if a.role:
        print(role)
        return 0
    if role == "none":
        if a.emit == "shell":
            print("LINK_ROLE=none")
            print(f"LINK_WHY={_q('no radio here matches a role serial in ' + str(path))}")
            return 0
        print("[link] no radio in this box matches a role in the link file",
              file=sys.stderr)
        return 1
    if a.rx_band:
        print(rx_band(link, role) or "")
        return 0

    r = link["roles"][role]
    if a.emit == "radio-cmd":
        print(" ".join(_q(t) for t in radio_cmd(link, role, resolve=not a.no_resolve)))
        return 0
    # default / --emit shell — everything auto_link.sh needs in one eval:
    # this box's role/radio, the band it must survey, the calibration-link facts
    # (both boxes agree on these, so either can author the same plan), and the
    # final radio.sh line with det-mult/sync-threshold already resolved.
    import shlex
    phy = link.get("phy", {})
    data, ack = link.get("data", {}), link.get("ack", {})
    roles = link.get("roles", {})
    cmd = radio_cmd(link, role, resolve=not a.no_resolve)
    print(f"LINK_ROLE={_q(role)}")
    print(f"LINK_DEVICE={_q(r.get('device',''))}")
    print(f"LINK_ARGS={_q(_radio_args(r))}")
    print(f"LINK_RX_BAND={_q(rx_band(link, role) or '')}")
    # the DATA link, source -> sink: the calibration pass measures its threshold
    print(f"LINK_DATA_TX_SERIAL={_q((roles.get('source') or {}).get('serial',''))}")
    print(f"LINK_DATA_RX_SERIAL={_q((roles.get('sink') or {}).get('serial',''))}")
    print(f"LINK_DATA_BAND={_q(data.get('band') or '')}")
    print(f"LINK_DATA_FREQ={_q(_hz(data.get('freq_hz')))}")
    print(f"LINK_ACK_TRANSPORT={_q(str(ack.get('transport','tcp')).lower())}")
    print(f"LINK_ACK_BAND={_q(ack.get('band') or '')}")
    print(f"LINK_ACK_FREQ={_q(_hz(ack.get('freq_hz')))}")
    print(f"LINK_SCHEME={_q(phy.get('scheme') or 'QPSK')}")
    print(f"LINK_RATE={_q(_hz(phy.get('rate')) or '2e6')}")
    print(f"LINK_SYM={_q(_hz(phy.get('symbol_rate')) or '1e6')}")
    print(f"LINK_RADIO_CMD={_q(' '.join(shlex.quote(t) for t in cmd))}")
    return 0


def _q(s):
    import shlex
    return shlex.quote(str(s))


if __name__ == "__main__":
    sys.exit(main())
