#!/usr/bin/env python3
"""pi_agent.py — runs ON a Pi, drives its Teensy, prints one JSON object per line.

The gateway ships this file over SSH on every job (`ssh <pi> python3 - <args>` with the
file on stdin), so a Pi needs nothing installed beyond python3 + pyserial and there is
no copy on the Pi to drift out of date.

It speaks the LoRa_Sensing firmware that is already flashed on the boards, NOT the
unionlabs lora_phy protocol — that project is live, so the boards are driven as they
are rather than reflashed:

    sx1276_sweep_tx   transmit-only   SET CFG <sf> <bw> <cr> <pow>, SET SIZE, SET PERIOD,
                                      START / STOP, STATUS (... txc= seq= ...)
    sx1276_sweep_rx   receive-only    SET CFG <sf> <bw> <cr>, SET RATE, START / STOP,
                                      P <micros> <crc> <rssi> <snr> <fei> ... <seq> <cfg> <pow> ...

LEAVE IT AS YOU FOUND IT. Every mode reads STATUS first and, in a `finally`, puts back
the frequency, PHY config, packet size/period/rate and the run state it found — so a job
never leaves a sensing board on someone else's settings.

Modes:
    status                       print the board's STATUS, change nothing
    flash --role --mcu           load the firmware arriving on stdin, check it answers as --role
    apply --state <json>         put back settings saved from an earlier STATUS
    rx --sf --bw --cr --freq --seconds
    tx --sf --bw --cr --pow --freq --size --period-us --count
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

PORT = "/dev/ttyACM0"
PORT_SERVICES = ("blora-hub",)      # services that use the port themselves
BAUD = 115200
HARD_MAX_S = 900            # no job holds a radio longer than this, whatever it asks for

# STOP CHANNEL. The gateway sends this file as a command argument, which leaves stdin
# free: a line "stop" ends the job early, and EOF -- the gateway or the SSH connection
# dying -- ends it too. Either way the run loop exits and `finally` restores the board,
# so a lost gateway can never leave a radio transmitting.
STOP = threading.Event()


def _watch_stdin():
    try:
        for line in sys.stdin:
            if line.strip().lower() == "stop":
                break
    except (OSError, ValueError):
        pass
    STOP.set()

BW_CODE = {7800: 0, 10400: 1, 15600: 2, 20800: 3, 31250: 4, 41700: 5,
           62500: 6, 125000: 7, 250000: 8, 500000: 9}


def emit(**kw):
    kw.setdefault("t", round(time.time(), 3))
    print(json.dumps(kw), flush=True)


def port_holders(port):
    """Processes that already have `port` open. Two openers would split the serial
    stream between them, silently corrupting both -- so a held port is refused."""
    me, held = os.getpid(), []
    # Asked directly, because the scan below skips any process it cannot read, and it
    # missed blora-hub on pi09. That service also comes back by itself on a reboot.
    for svc in PORT_SERVICES:
        try:
            state = subprocess.run(["systemctl", "is-active", svc], capture_output=True,
                                   text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if state in ("active", "activating", "reloading"):
            held.append(f"service {svc}")
    for fd in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            if os.readlink(fd) != port:
                continue
            pid = int(fd.split("/")[2])
            if pid == me:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode(errors="replace").strip()
            held.append(f"{pid} {cmd}")
        except (OSError, ValueError):
            continue
    return held


class Board:
    def __init__(self, port=PORT):
        held = port_holders(port)
        if held:
            raise RuntimeError(f"{port} is in use by another process ({'; '.join(held)}) -- "
                               f"not touching it")
        import serial                                   # pyserial, present on the Pis
        self.s = serial.Serial(port, BAUD, timeout=0.1)
        time.sleep(0.3)
        self.s.reset_input_buffer()

    def send(self, cmd, expect="S OK", wait=2.0):
        """Send one command; return the first reply line starting with `expect`.
        Stream lines (R/P/T/S TXRATE/S SMPRATE) arriving meanwhile are skipped."""
        self.s.write((cmd + "\n").encode())
        end = time.time() + wait
        while time.time() < end:
            line = self.readline()
            if line is None:
                continue
            if line.startswith("S ERR"):
                raise RuntimeError(f"{cmd!r} -> {line}")
            if line.startswith(expect):
                return line             # S CFGDONE / S TXRATE / S SMPRATE are not replies
        raise TimeoutError(f"no reply to {cmd!r}")

    def readline(self):
        raw = self.s.readline()
        if not raw:
            return None
        return raw.decode(errors="replace").strip() or None

    def status(self):
        line = self.send("STATUS", expect="S STATUS")
        st = {}
        for tok in line.split()[2:]:
            if "=" in tok:
                k, v = tok.split("=", 1)
                st[k] = v
        # which firmware is this? the TX build reports pow/txc, the RX build smp/pkt
        st["role"] = "tx" if "txc" in st else ("rx" if "smp" in st else "unknown")
        return st

    def close(self):
        self.s.close()


def _cr(v):                       # "4/5" -> 5
    return int(str(v).split("/")[-1])


def restore(b, st):
    """Put back what STATUS said before the job. Best effort, every step reported."""
    steps = []
    try:
        b.send("STOP")
        if st.get("freq"):
            steps.append(b.send(f"SET FREQ {st['freq']}"))
        if st["role"] == "tx":
            steps.append(b.send(f"SET CFG {st['sf']} {st['bw']} {_cr(st['cr'])} {st['pow']}"))
            steps.append(b.send(f"SET SIZE {st['size']}"))
            steps.append(b.send(f"SET RATE {st['rate']}"))
            steps.append(b.send(f"SET PERIOD {st['period']}"))
            if st.get("pre"):
                steps.append(b.send(f"SET PRE {st['pre']}"))
            if st.get("temp"):
                steps.append(b.send(f"SET TEMP {st['temp']}"))
        elif st["role"] == "rx":
            steps.append(b.send(f"SET CFG {st['sf']} {st['bw']} {_cr(st['cr'])}"))
            steps.append(b.send(f"SET RATE {st['rate']}"))
            for key in ("lna", "acc", "errata", "temp"):      # a reflash resets these too
                if st.get(key) is not None:
                    steps.append(b.send(f"SET {key.upper()} {st[key]}"))
        if st.get("run") == "1":
            steps.append(b.send("START", expect="S OK started"))
        # Prove it: compare the settings that define the board's state, before vs now.
        now = b.status()
        keys = ("freq", "sf", "bw", "cr", "pow", "size", "rate", "period", "pre", "run",
                "lna", "acc", "errata", "temp")
        diff = {k: [st[k], now.get(k)] for k in keys if k in st and now.get(k) != st[k]}
        emit(event="restored", ok=not diff, mismatch=diff, steps=steps)
    except Exception as e:                                    # noqa: BLE001
        emit(event="restored", ok=False, error=str(e), steps=steps)


def set_freq(b, st, freq):
    if str(st.get("freq")) != str(freq):
        b.send(f"SET FREQ {freq}")


def run_rx(b, st, a):
    if st["role"] != "rx":
        raise RuntimeError(f"this board runs the {st['role']} firmware, it cannot receive")
    set_freq(b, st, a.freq)
    b.send("SET RATE 1")                         # wideband RSSI stream down to 1 Hz
    b.send(f"SET CFG {a.sf} {a.bw} {a.cr}")
    b.send("START", expect="S OK started")
    emit(event="ready", role="rx")
    want_cfg = (a.sf << 4) | BW_CODE.get(a.bw, -1)
    # Runs until told to stop (the gateway does, once the TX is done); --seconds is
    # only the backstop if that message never comes.
    end = time.time() + min(a.seconds, HARD_MAX_S)
    while time.time() < end and not STOP.is_set():
        line = b.readline()
        if not line or not line.startswith("P "):
            continue
        f = line.split()
        if len(f) < 15:
            continue
        crc, rssi_raw, snr_raw, fei_raw = int(f[2]), int(f[3]), int(f[4]), int(f[5])
        seq, cfg, pw = int(f[10]), int(f[11]), int(f[12])
        snr = snr_raw / 4.0
        rssi = -157 + rssi_raw + (snr if snr < 0 else 0)
        fei_hz = fei_raw * (2 ** 24) / 32e6 * (a.bw / 1000.0 / 500.0)
        emit(event="packet", crc_ok=bool(crc), rssi_dbm=round(rssi, 1),
             snr_db=snr, fei_hz=round(fei_hz), seq=seq, pow_dbm=pw,
             cfg_match=(cfg == want_cfg), micros=int(f[1]))


def run_tx(b, st, a):
    if st["role"] != "tx":
        raise RuntimeError(f"this board runs the {st['role']} firmware, it cannot transmit")
    set_freq(b, st, a.freq)
    b.send(f"SET CFG {a.sf} {a.bw} {a.cr} {a.pow}")
    b.send(f"SET SIZE {a.size}")
    b.send("SET RATE 0")
    b.send(f"SET PERIOD {a.period_us}")
    seq0 = int(b.status()["seq"])
    b.send("START", expect="S OK started")
    emit(event="started", role="tx", seq_first=seq0)
    # The firmware has no packet count. The first packet leaves AT START, so `count`
    # packets take (count - 1) periods; stop half a period after the last one.
    # --seconds instead runs for a fixed time; whichever limit comes first wins.
    dur = HARD_MAX_S
    if a.count:
        dur = min(dur, (a.count - 0.5) * a.period_us / 1e6)
    if a.seconds:
        dur = min(dur, a.seconds)
    end = time.time() + dur
    while time.time() < end and not STOP.is_set():
        line = b.readline()
        if line and line.startswith("S TXRATE"):
            _, _, txc, seq, air = line.split()
            emit(event="progress", sent=int(seq) - seq0, airtime_us=int(air))
    b.send("STOP")
    after = b.status()
    emit(event="sent", seq_first=seq0, seq_end=int(after["seq"]),
         sent=int(after["seq"]) - seq0, airtime_us=int(after.get("air", 0)),
         stopped_early=STOP.is_set())


def find_loader():
    """teensy_loader_cli, and the library path the Arduino-installed copy needs."""
    tools = sorted(glob.glob(os.path.expanduser("~/.arduino15/packages/teensy/tools/teensy-tools/*/")))
    env = dict(os.environ)
    if tools:
        env["LD_LIBRARY_PATH"] = tools[-1]
    for c in (shutil.which("teensy_loader_cli"), os.path.expanduser("~/fastrssi/teensy_loader_cli"),
              *(os.path.join(t, "teensy_loader_cli") for t in tools)):
        if c and os.access(c, os.X_OK):
            return c, env
    raise RuntimeError("no teensy_loader_cli on this Pi")


def flash(a):
    """Load the firmware on stdin, then prove it runs: a STATUS must answer as a.role.
    A Teensy keeps its bootloader, so a failed load leaves it recoverable, not dead."""
    held = port_holders(a.port)
    if held:
        raise RuntimeError(f"{a.port} is in use ({'; '.join(held)}) -- not flashing")
    data = sys.stdin.buffer.read()
    if not data.startswith(b":"):
        raise RuntimeError("what arrived is not an Intel hex file")
    loader, env = find_loader()
    import serial
    with tempfile.NamedTemporaryFile(suffix=".hex") as f:
        f.write(data)
        f.flush()
        serial.Serial(a.port, 134).close()      # 134 baud = "enter the bootloader"
        out = ""
        for attempt in range(1, 5):             # the first try usually fails; the next one lands
            p = subprocess.run([loader, f"--mcu={a.mcu}", "-w", "-v", f.name], env=env,
                               capture_output=True, text=True, timeout=45)
            out = (p.stdout + p.stderr).strip()[-300:]
            if p.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"loader failed 4 times: {out}")
    end = time.time() + 20                      # the port returns once the new firmware boots
    while time.time() < end:
        if os.path.exists(a.port):
            try:
                time.sleep(1)
                b = Board(a.port)
                try:
                    st = b.status()
                finally:
                    b.close()
                if st["role"] != a.role:
                    raise RuntimeError(f"board answers as {st['role']}, wanted {a.role}")
                emit(event="flashed", role=a.role, attempts=attempt)
                return 0
            except (OSError, TimeoutError):
                pass
        time.sleep(0.5)
    raise RuntimeError("the board did not come back after flashing")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["status", "rx", "tx", "flash", "apply"])
    ap.add_argument("--role", choices=["tx", "rx"], help="flash: the firmware being loaded")
    ap.add_argument("--mcu", choices=["TEENSY40", "TEENSY41"], help="flash: the board type")
    ap.add_argument("--state", help="apply: JSON from an earlier STATUS")
    ap.add_argument("--port", default=PORT)
    ap.add_argument("--sf", type=int, default=9)
    ap.add_argument("--bw", type=int, default=125000)
    ap.add_argument("--cr", type=int, default=5)
    ap.add_argument("--pow", type=int, default=14)
    ap.add_argument("--freq", type=int, default=915000000)
    ap.add_argument("--size", type=int, default=16)
    ap.add_argument("--period-us", type=int, default=500000)
    ap.add_argument("--count", type=int, default=0, help="tx: packets to send (0 = use --seconds)")
    ap.add_argument("--seconds", type=float, default=0,
                    help="rx: backstop listening time; tx: transmit for this long")
    a = ap.parse_args()
    if a.mode == "rx" and not a.seconds:
        a.seconds = HARD_MAX_S
    if a.mode == "tx" and not (a.count or a.seconds):
        a.count = 20
    if a.mode == "flash":                       # stdin carries the firmware, not a stop channel
        try:
            return flash(a)
        except Exception as e:                                # noqa: BLE001
            emit(event="error", error=f"flash: {e}")
            return 1
    threading.Thread(target=_watch_stdin, daemon=True).start()

    try:
        b = Board(a.port)
    except Exception as e:                                    # noqa: BLE001
        emit(event="error", error=f"cannot open {a.port}: {e}")
        return 1
    if a.mode == "apply":
        try:
            restore(b, json.loads(a.state))
            return 0
        finally:
            b.close()
    st = None
    try:
        st = b.status()
        emit(event="status", **st)
        if a.mode == "rx":
            run_rx(b, st, a)
        elif a.mode == "tx":
            run_tx(b, st, a)
        return 0
    except Exception as e:                                    # noqa: BLE001
        emit(event="error", error=str(e))
        return 1
    finally:
        if st is not None and a.mode != "status":
            restore(b, st)
        b.close()
        emit(event="done")


if __name__ == "__main__":
    sys.exit(main())
