#!/usr/bin/env python3
"""gateway.py — the LoRa gateway: a web page that runs point-to-point jobs on the Pis.

    python3 gateway.py                       # serve on this machine's Tailscale IP :8080
    python3 gateway.py --host 127.0.0.1      # local only, for development
    python3 gateway.py --ssh-key ~/.ssh/lora_gateway

WHY IT EXISTS. The Pis cannot join the UnionLabs platform, so they cannot be reserved or
logged into from it. Instead this one service, on a lab machine inside the tailnet, holds
the ONLY credential for the Pis (its SSH key) and offers jobs, not shells: a user picks a
TX Pi, an RX Pi and the radio settings; the gateway runs it and returns both logs. No
password or key ever reaches the user.

WHAT A JOB DOES
    1. both Pis are checked: visible to this user, online, right firmware, not busy
    2. pi_agent.py is shipped over SSH to the RX Pi, which starts listening
    3. once it reports ready, the same agent goes to the TX Pi, which sends `count` packets
    4. both logs stream back, labelled by Pi name; a summary is computed and saved
    5. each agent restores its board's own settings (the boards belong to LoRa_Sensing)

Standard library only, so the host needs python3 and `tailscale` and nothing else.
"""
import argparse
import base64
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.join(HERE, "pi_agent.py")
PI_NAME = re.compile(r"^raspberrypi-(\d+)$")

BWS = (62500, 125000, 250000, 500000)
LIMITS = dict(sf=(7, 12), cr=(5, 8), pow=(2, 17), size=(6, 255), count=(1, 5000),
              duration_s=(1, 600), freq_mhz=(902.0, 928.0), period_ms=(20, 10000))
MAX_JOB_S = 600             # one job holds two radios for at most this long
RX_TAIL_S = 2.0             # RX keeps listening this long after the TX stops

# ROLE SWAPS. A board's firmware either sends or receives, so using a Pi in the other role
# means flashing it for that job and flashing its own firmware back afterwards. ONLY the
# boards named here are ever flashed, and only with these exact files (--firmware, the
# LoRa_Sensing backup folder; checked by md5 at startup). Adding a Pi here needs its board
# type and its own firmware confirmed first: another MCU needs another build.
FLASHABLE = {"raspberrypi-07": ("TEENSY40", "rx"),      # pi name -> (board, its own role)
             "raspberrypi-08": ("TEENSY40", "tx")}
FIRMWARE = {"rx": ("pi07/rx_acc.hex", "9ea8f6c14cc36626a25ed70ffe44f55a"),
            "tx": ("pi08/sx1276_sweep_tx_period_t40.hex", "324eba491922f8fb376927ae9f4c349f")}
UNAVAILABLE = "unavailable for the test"


# ───────────────────────────────────────────── reservations (pluggable) ──
def visible_pis(request_ctx, pis):
    """Which Pis this user may see and use.

    TODAY: all of them. LATER: read the user's reservation from the AWS reservation
    record and keep only the Pis it names -- the same reservation-reading logic being
    built for the USRP devices. Everything else in the gateway goes through this one
    function, so that change is made here and nowhere else."""
    return pis


# ─────────────────────────────────────────────────────────── airtime ──
def airtime_ms(sf, bw, cr, size, preamble=8):
    """Semtech SX1276 time on air (explicit header, CRC on). Checked against the
    firmware's own measurement: SF9 / 125 kHz / 16 B -> 164.9 ms vs 165.1 ms measured."""
    tsym = (2 ** sf) / bw * 1000.0
    de = 1 if tsym > 16 else 0
    n = 8 + max(math.ceil((8 * size - 4 * sf + 28 + 16) / (4 * (sf - 2 * de))) * cr, 0)
    return (preamble + 4.25) * tsym + n * tsym


# ─────────────────────────────────────────────────────────── the Pis ──
class Fleet:
    def __init__(self, ssh_key=None, probe_ttl=60, enabled=None, firmware_dir=None):
        self.ssh_key = ssh_key
        self.firmware = {}                  # role -> hex bytes; empty = no role swaps
        if firmware_dir:
            for role, (path, md5) in FIRMWARE.items():
                with open(os.path.join(firmware_dir, path), "rb") as f:
                    data = f.read()
                if hashlib.md5(data).hexdigest() != md5:
                    raise SystemExit(f"{path} is not the expected build (md5 differs)")
                self.firmware[role] = data
        # Pis this gateway may touch. Others are listed but never probed or used: their
        # boards may belong to another project, and even a STATUS opens their radio.
        self.enabled = set(enabled) if enabled else None
        self.ttl = probe_ttl
        self.lock = threading.Lock()
        self.busy = {}                      # pi name -> job id
        self.cache = {}                     # pi name -> probe result
        self.cache_t = 0.0

    def ssh_cmd(self, pi, args):
        num = PI_NAME.match(pi["name"]).group(1)
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
               "-o", "StrictHostKeyChecking=accept-new"]
        if self.ssh_key:
            cmd += ["-i", self.ssh_key]
        # The agent travels as a COMMAND ARGUMENT, not on stdin, so stdin stays free as the
        # job's stop channel (see pi_agent.STOP). base64 sidesteps every quoting issue;
        # the file is re-read per call so an edit takes effect on the next job.
        with open(AGENT, "rb") as f:
            code = base64.b64encode(f.read()).decode()
        boot = (f"import base64;exec(compile(base64.b64decode('{code}'),"
                f"'pi_agent.py','exec'))")
        # the remote side runs through a shell: every argument is validated, and quoted
        remote = "python3 -c " + shlex.quote(boot) + " " + " ".join(shlex.quote(str(a)) for a in args)
        return cmd + [f"pi{num}@{pi['ip']}", remote]

    def tailnet(self):
        out = subprocess.run(["tailscale", "status", "--json"], capture_output=True,
                             text=True, timeout=10).stdout
        d = json.loads(out)
        pis = []
        for p in (d.get("Peer") or {}).values():
            # The tailnet MACHINE name is the first label of DNSName. HostName is the
            # OS hostname, which is plain "raspberrypi" on every Pi.
            name = (p.get("DNSName") or "").split(".")[0]
            if PI_NAME.match(name):
                pis.append({"name": name, "ip": (p.get("TailscaleIPs") or [""])[0],
                            "online": bool(p.get("Online"))})
        return sorted(pis, key=lambda x: int(PI_NAME.match(x["name"]).group(1)))

    def probe(self, pi):
        """Ask the board what it is (read-only STATUS)."""
        try:
            p = subprocess.run(self.ssh_cmd(pi, ["status"]), stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=20)
        except subprocess.TimeoutExpired:
            return {"access": "timeout"}
        if p.returncode == 255:
            return {"access": "no ssh access"}
        for line in p.stdout.decode(errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("event") == "status":
                return {"access": "ok", "role": ev.get("role"),
                        "board": {k: ev.get(k) for k in ("freq", "sf", "bw", "cr", "pow")
                                  if ev.get(k) is not None}}
            if ev.get("event") == "error":
                return {"access": "ok", "role": None, "error": ev.get("error")}
        return {"access": "ok", "role": None, "error": "no reply from the board"}

    def list(self, refresh=False):
        pis = self.tailnet()
        stale = refresh or time.time() - self.cache_t > self.ttl
        if stale:
            todo = [p for p in pis if p["online"] and p["name"] not in self.busy
                    and self.allowed(p["name"])]
            res = {}
            ts = [threading.Thread(target=lambda p=p: res.__setitem__(p["name"], self.probe(p)))
                  for p in todo]
            for t in ts:
                t.start()
            for t in ts:
                t.join(25)
            with self.lock:
                self.cache.update(res)
                self.cache_t = time.time()
        for p in pis:
            if not self.allowed(p["name"]):
                p["access"] = UNAVAILABLE
            elif not p["online"]:
                p["access"] = "offline"
            else:
                p.update(self.cache.get(p["name"], {}))
            p["busy"] = self.busy.get(p["name"])
            p["can_swap"] = self.can_swap(p["name"])
            p["home_role"] = FLASHABLE.get(p["name"], (None, None))[1]
        return pis

    def can_swap(self, name):
        return bool(self.firmware) and name in FLASHABLE and self.allowed(name)

    def agent(self, pi, args, log, stdin=None, timeout=30):
        """One short agent run (status, flash, apply): its events go to `log`."""
        p = subprocess.run(self.ssh_cmd(pi, args), input=stdin or b"",
                           capture_output=True, timeout=timeout)
        evs = []
        for line in p.stdout.decode(errors="replace").splitlines():
            try:
                evs.append(json.loads(line))
            except ValueError:
                pass
        log.extend(e for e in evs if e.get("event") != "done")
        bad = [e["error"] for e in evs if e.get("event") == "error"]
        if bad or p.returncode:
            raise RuntimeError(f"{pi['name']}: " + (bad[0] if bad else f"agent exit {p.returncode}"))
        return evs

    def read_status(self, pi, log):
        evs = self.agent(pi, ["status"], log)
        st = next((e for e in evs if e.get("event") == "status"), None)
        if not st:
            raise RuntimeError(f"{pi['name']}: no STATUS")
        return {k: v for k, v in st.items() if k not in ("event", "t")}

    def flash(self, pi, role, log):
        mcu = FLASHABLE[pi["name"]][0]      # KeyError = refuse: never flash an unlisted Pi
        self.agent(pi, ["flash", "--role", role, "--mcu", mcu], log,
                   stdin=self.firmware[role], timeout=120)

    def apply(self, pi, state, log):
        evs = self.agent(pi, ["apply", "--state", json.dumps(state)], log)
        return any(e.get("event") == "restored" and e.get("ok") for e in evs)

    def allowed(self, name):
        return self.enabled is None or name in self.enabled

    def claim(self, names, job_id):
        with self.lock:
            taken = [n for n in names if n in self.busy]
            if taken:
                return taken
            for n in names:
                self.busy[n] = job_id
            return []

    def release(self, names):
        with self.lock:
            for n in names:
                self.busy.pop(n, None)


# ──────────────────────────────────────────────────────────── jobs ──
class Job:
    def __init__(self, spec, tx, rx):
        self.id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.spec, self.tx, self.rx = spec, tx, rx
        self.state = "queued"
        self.error = None
        self.logs = {tx["name"]: [], rx["name"]: []}
        self.summary = None
        self.created = time.time()
        self.procs = {}                     # pi name -> its ssh process (stdin = stop channel)
        self.stop_requested = False

    def tell(self, name, line):
        p = self.procs.get(name)
        if p and p.poll() is None:
            try:
                p.stdin.write((line + "\n").encode())
                p.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass

    def stop(self):
        """The Stop button: the TX stops sending; the RX is stopped by run_job after its
        tail, so the last packets in flight are still counted."""
        self.stop_requested = True
        self.tell(self.tx["name"], "stop")

    def view(self, full=True, since=None):
        v = {"id": self.id, "state": self.state, "error": self.error, "spec": self.spec,
             "tx": self.tx["name"], "rx": self.rx["name"], "summary": self.summary,
             "created": self.created, "stop_requested": self.stop_requested}
        if full:
            # `since` = how many events of each log the caller already has, so a page
            # polling a long run fetches only what is new
            since = since or {}
            v["logs"] = {n: evs[since.get(n, 0):] for n, evs in self.logs.items()}
            v["log_len"] = {n: len(evs) for n, evs in self.logs.items()}
        return v


def validate(body, fleet, request_ctx):
    def num(k, cast=int):
        try:
            v = cast(body[k])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"{k}: missing or not a number")
        lo, hi = LIMITS[k]
        if not lo <= v <= hi:
            raise ValueError(f"{k} must be {lo}..{hi}, got {v}")
        return v

    spec = {"sf": num("sf"), "cr": num("cr"), "pow": num("pow"), "size": num("size"),
            "freq_mhz": num("freq_mhz", float)}
    # STOP AFTER: a packet count, or a duration. One of the two, never both.
    spec["stop_by"] = "seconds" if body.get("stop_by") == "seconds" else "packets"
    if spec["stop_by"] == "seconds":
        spec["duration_s"] = num("duration_s", float)
    else:
        spec["count"] = num("count")
    try:
        spec["bw"] = int(body["bw"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("bw: missing or not a number")
    if spec["bw"] not in BWS:
        raise ValueError(f"bw must be one of {BWS}")
    air = airtime_ms(spec["sf"], spec["bw"], spec["cr"], spec["size"])
    spec["airtime_ms"] = round(air, 1)
    if body.get("period_ms") in (None, "", 0, "0"):
        spec["period_ms"] = int(max(air * 1.5 + 50, 100))
    else:
        spec["period_ms"] = num("period_ms")
        if spec["period_ms"] < air + 20:
            raise ValueError(f"period {spec['period_ms']} ms is shorter than one packet on "
                             f"air ({air:.0f} ms) plus 20 ms -- raise it or lower SF/size")
    if spec["stop_by"] == "packets":
        spec["duration_s"] = round(spec["count"] * spec["period_ms"] / 1000, 1)
        if spec["duration_s"] > MAX_JOB_S:
            raise ValueError(f"{spec['count']} packets x {spec['period_ms']} ms is "
                             f"{spec['duration_s']:.0f} s, over the {MAX_JOB_S} s limit -- "
                             f"send fewer packets or use a shorter interval")
    else:
        spec["count_expected"] = int(spec["duration_s"] * 1000 // spec["period_ms"]) + 1

    pis = {p["name"]: p for p in visible_pis(request_ctx, fleet.list())}
    tx, rx = pis.get(body.get("tx")), pis.get(body.get("rx"))
    if not tx or not rx:
        raise ValueError("choose a TX Pi and an RX Pi from the list")
    if tx["name"] == rx["name"]:
        raise ValueError("TX and RX must be different Pis")
    spec["swap"] = []
    for p, want in ((tx, "tx"), (rx, "rx")):
        if p.get("access") != "ok":
            raise ValueError(f"{p['name']}: {p.get('access', 'unknown')}")
        role, home = p.get("role"), p.get("home_role")
        swappable = fleet.can_swap(p["name"]) and role in ("tx", "rx")
        if role != want and not swappable:
            raise ValueError(f"{p['name']} runs the {role} firmware and cannot be the "
                             f"{want.upper()} end")
        # a board found away from home (a job cut off mid-way) is taken home afterwards too
        if swappable and (role != want or role != home):
            spec["swap"].append(p["name"])
    spec["tx"], spec["rx"] = tx["name"], rx["name"]
    return spec, tx, rx


def run_job(job, fleet, results_dir):
    s = job.spec
    freq = int(round(s["freq_mhz"] * 1e6))
    common = ["--sf", s["sf"], "--bw", s["bw"], "--cr", s["cr"], "--freq", freq]
    tx_s = s["duration_s"]
    # RX --seconds is only a backstop: run_job stops it RX_TAIL_S after the TX ends
    rx_args = ["rx", *common, "--seconds", round(tx_s + 60, 1)]
    tx_args = ["tx", *common, "--pow", s["pow"], "--size", s["size"],
               "--period-us", s["period_ms"] * 1000]
    tx_args += (["--seconds", s["duration_s"]] if s["stop_by"] == "seconds"
                else ["--count", s["count"]])
    ready = threading.Event()

    def pump(pi, args):
        proc = subprocess.Popen(fleet.ssh_cmd(pi, args), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        job.procs[pi["name"]] = proc
        for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            try:
                ev = json.loads(line)
            except ValueError:
                ev = {"event": "text", "text": line, "t": round(time.time(), 3)}
            job.logs[pi["name"]].append(ev)
            if ev.get("event") == "ready":
                ready.set()
            if ev.get("event") == "error":
                ready.set()                 # unblock; the error is reported below
        proc.wait()
        return proc.returncode

    saved = {}                              # pi name -> its settings before the swap
    state, error = "failed", "interrupted"
    try:
        for pi, want in ((job.rx, "rx"), (job.tx, "tx")):
            if pi["name"] in s["swap"]:
                log = job.logs[pi["name"]]
                saved[pi["name"]] = st = fleet.read_status(pi, log)
                if st["role"] != want:
                    job.state = f"loading {want.upper()} firmware on {pi['name']}"
                    fleet.flash(pi, want, log)
        job.state = "starting receiver"
        rx_t = threading.Thread(target=pump, args=(job.rx, rx_args))
        rx_t.start()
        if not ready.wait(25) or not any(e.get("event") == "ready" for e in job.logs[job.rx["name"]]):
            raise RuntimeError(f"{job.rx['name']} did not start listening")
        job.state = "transmitting"
        if not job.stop_requested:
            pump(job.tx, tx_args)
        job.state = "collecting"
        time.sleep(RX_TAIL_S)               # let the last packet in flight land
        job.tell(job.rx["name"], "stop")
        rx_t.join(30)
        job.summary = summarize(job)
        errs = [f"{n}: {e['error']}" for n, evs in job.logs.items()
                for e in evs if e.get("event") == "error"]
        state, error = ("failed" if errs else "done"), "; ".join(errs) or None
    except Exception as e:                                    # noqa: BLE001
        state, error = "failed", str(e)
    finally:
        for p in job.procs.values():        # closing stdin = EOF = the agent stops too
            try:
                p.stdin.close()
            except OSError:
                pass
        for p in job.procs.values():        # the serial port must be free before flashing
            try:
                p.wait(15)
            except subprocess.TimeoutExpired:
                p.kill()
        # Back to its own firmware and settings, on every path out of the job. Only then
        # is the job finished: the page must not offer a board that is still being flashed.
        for name, st in saved.items():
            pi = job.tx if name == job.tx["name"] else job.rx
            home = FLASHABLE[name][1]
            job.state = f"restoring {name}"
            ok = False
            try:
                fleet.flash(pi, home, job.logs[name])
                # a board found away from home: its own settings are gone, the firmware is back
                ok = fleet.apply(pi, st, job.logs[name]) if st["role"] == home else True
            except Exception as e:                            # noqa: BLE001
                state = "failed"
                error = "; ".join(x for x in (error, f"{name} NOT restored: {e}") if x)
            if job.summary:
                job.summary["boards_restored"][name] = ok
        job.state, job.error = state, error
        fleet.release([job.tx["name"], job.rx["name"]])
        fleet.cache_t = 0.0                 # roles may have changed: ask the boards again
        os.makedirs(results_dir, exist_ok=True)
        with open(os.path.join(results_dir, f"job-{job.id}.json"), "w") as f:
            json.dump(job.view(), f, indent=1)


def summarize(job):
    tx_ev = next((e for e in job.logs[job.tx["name"]] if e.get("event") == "sent"), None)
    pk = [e for e in job.logs[job.rx["name"]] if e.get("event") == "packet"]
    if tx_ev:
        lo, hi = tx_ev["seq_first"], tx_ev["seq_end"]
        mine = [e for e in pk if e.get("cfg_match") and lo <= e["seq"] < hi]
    else:
        mine = [e for e in pk if e.get("cfg_match")]
    ok = [e for e in mine if e["crc_ok"]]
    uniq = {e["seq"] for e in ok}
    sent = tx_ev["sent"] if tx_ev else None

    def stat(key):
        v = [e[key] for e in ok]
        return {"mean": round(sum(v) / len(v), 2), "min": min(v), "max": max(v)} if v else None

    restored = {n: next((e.get("ok") for e in evs if e.get("event") == "restored"), None)
                for n, evs in job.logs.items()}
    return {"sent": sent, "received_crc_ok": len(uniq),
            "crc_fail": sum(1 for e in mine if not e["crc_ok"]),
            "other_packets": len(pk) - len(mine),
            "pdr": round(len(uniq) / sent, 3) if sent else None,
            "rssi_dbm": stat("rssi_dbm"), "snr_db": stat("snr_db"), "fei_hz": stat("fei_hz"),
            "airtime_ms": tx_ev.get("airtime_us", 0) / 1000 if tx_ev else None,
            "boards_restored": restored}


# ───────────────────────────────────────────────────────────── http ──
class Gateway:
    def __init__(self, fleet, results_dir):
        self.fleet, self.results_dir = fleet, results_dir
        self.jobs = {}

    def submit(self, body, ctx):
        spec, tx, rx = validate(body, self.fleet, ctx)
        job = Job(spec, tx, rx)
        taken = self.fleet.claim([tx["name"], rx["name"]], job.id)
        if taken:
            raise ValueError(f"busy with another job: {', '.join(taken)}")
        self.jobs[job.id] = job
        threading.Thread(target=run_job, args=(job, self.fleet, self.results_dir),
                         daemon=True).start()
        return job


def make_handler(gw):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            pass

        def send(self, code, body, ctype="application/json", extra=None):
            data = body if isinstance(body, bytes) else (
                json.dumps(body).encode() if ctype == "application/json" else body.encode())
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def ctx(self):
            return {"client": self.client_address[0]}

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/":
                with open(PAGE_PATH, encoding="utf-8") as f:   # re-read: edits need no restart
                    return self.send(200, f.read(), "text/html")
            if u.path == "/api/pis":
                pis = visible_pis(self.ctx(), gw.fleet.list(refresh="refresh" in q))
                return self.send(200, {"pis": pis, "limits": LIMITS, "bws": BWS})
            if u.path == "/api/airtime":
                try:
                    a = airtime_ms(int(q["sf"][0]), int(q["bw"][0]), int(q["cr"][0]),
                                   int(q["size"][0]))
                    return self.send(200, {"airtime_ms": round(a, 1)})
                except (KeyError, ValueError, ZeroDivisionError):
                    return self.send(400, {"error": "sf, bw, cr, size required"})
            if u.path == "/api/jobs":
                js = sorted(gw.jobs.values(), key=lambda j: -j.created)
                return self.send(200, {"jobs": [j.view(full=False) for j in js]})
            m = re.match(r"^/api/jobs/([\w-]+)(/download)?$", u.path)
            if m and m.group(1) in gw.jobs:
                j = gw.jobs[m.group(1)]
                if m.group(2):
                    return self.send(200, json.dumps(j.view(), indent=1), "application/json",
                                     {"Content-Disposition": f'attachment; filename="lora-job-{j.id}.json"'})
                since = {}
                for part in (q.get("since") or [""])[0].split(","):
                    if ":" in part:
                        n, _, k = part.rpartition(":")
                        if k.isdigit():
                            since[n] = int(k)
                return self.send(200, j.view(since=since))
            return self.send(404, {"error": "not found"})

        def do_POST(self):
            m = re.match(r"^/api/jobs/([\w-]+)/stop$", urlparse(self.path).path)
            if m:
                j = gw.jobs.get(m.group(1))
                if not j:
                    return self.send(404, {"error": "no such job"})
                j.stop()
                return self.send(200, {"id": j.id, "stop_requested": True})
            if urlparse(self.path).path != "/api/jobs":
                return self.send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(min(n, 10000)) or b"{}")
                job = gw.submit(body, self.ctx())
                return self.send(200, {"id": job.id, "spec": job.spec})
            except ValueError as e:
                return self.send(400, {"error": str(e)})
    return H


PAGE_PATH = os.path.join(HERE, "page.html")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default=None,
                    help="address to bind (default: this machine's Tailscale IPv4, so the page "
                         "is reachable only over the tailnet, not the LAN)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--ssh-key", default=None, help="private key for the Pis (default: ssh's own)")
    ap.add_argument("--pis", default=None,
                    help="comma-separated Pis this gateway may use, e.g. raspberrypi-07,raspberrypi-08 "
                         "(default: every Pi in the tailnet). Others are listed, never touched.")
    ap.add_argument("--firmware", default=None,
                    help="the LoRa_Sensing firmware_backup folder; enables role swaps on the "
                         "boards in FLASHABLE. Absent = each board keeps its fixed role.")
    ap.add_argument("--results", default=os.path.expanduser("~/lora-gateway/results"))
    a = ap.parse_args()
    host = a.host or subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                                    text=True).stdout.split()[0]
    if not os.path.exists(PAGE_PATH):
        raise SystemExit(f"missing {PAGE_PATH}")
    enabled = [x.strip() for x in a.pis.split(",") if x.strip()] if a.pis else None
    gw = Gateway(Fleet(ssh_key=a.ssh_key, enabled=enabled, firmware_dir=a.firmware), a.results)
    srv = ThreadingHTTPServer((host, a.port), make_handler(gw))
    print(f"[gateway] http://{host}:{a.port}/   results -> {a.results}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
