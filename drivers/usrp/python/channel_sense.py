#!/usr/bin/env python3
"""
channel_sense.py — callable channel-occupancy sensing over the USRP SDR.

Import these from any script:

    from channel_sense import sense_channel, calibrate_floor, should_transmit

    # one occupancy measurement
    r = sense_channel(rx_args="serial=30CD3F7", threshold_db=-25)
    print(r["busy"], r["power_db"])       # -> True/False, e.g. -11.4

    # auto-set a threshold from the *current* (assumed-idle) noise floor
    thr = calibrate_floor(rx_args="serial=30CD3F7")

    # NEXT STEP: p-persistent transmit decision (sense -> maybe TX)
    if should_transmit(p=0.5, rx_args="serial=30CD3F7", threshold_db=thr):
        ...  # go ahead and transmit

It drives `sdr_system --role sense`, which integrates received power over a
window and prints one machine-parseable line:

    [SENSE] busy=1 power_db=-11.33 peak_db=-9.41 power=0.0736 window_ms=10.0 samples=16384 threshold_db=-30.00

Each call launches the binary once (~1-2 s radio init). For many windows at once
(e.g. calibration) it uses a single invocation with --sense-count, so the init
cost is paid only once.

CLI:
    python3 channel_sense.py --calibrate                 # measure the idle floor
    python3 channel_sense.py --count 10                  # auto-calibrate then sense 10x
    python3 channel_sense.py --threshold-db -20 --count 10
"""
import os
import random
import re
import shlex
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sdr  # noqa: E402

_LINE = re.compile(
    r"\[SENSE\] busy=(\d+) power_db=([-\d.]+) peak_db=([-\d.]+) "
    r"power=([-\d.eE+]+) window_ms=([-\d.]+) samples=(\d+)")

# Defaults match the validated RX radio; override per call.
_DEF = dict(rx_args="serial=30CD3F7", rx_freq=915e6, rx_rate=1.6e6, rx_gain=30)


def _run_sense(window_ms, count, threshold_db, binary=None, **radio):
    """Run one sdr_system --role sense invocation; return a list of window dicts."""
    opts = {**_DEF, **radio}
    cmd = sdr.SDR(role="sense", sense_window=window_ms, sense_count=count,
                  sense_threshold_db=threshold_db, viz=False, binary=binary,
                  **opts).command()
    p = subprocess.run(shlex.split(cmd), capture_output=True, text=True)
    rows = [{"busy": m.group(1) == "1", "power_db": float(m.group(2)),
             "peak_db": float(m.group(3)), "power": float(m.group(4)),
             "window_ms": float(m.group(5)), "samples": int(m.group(6))}
            for m in _LINE.finditer(p.stdout)]
    if not rows:
        raise RuntimeError("no [SENSE] output (radio error?)\n" + (p.stderr or p.stdout)[-600:])
    return rows


def sense_channel(window_ms=10, threshold_db=-30, **radio):
    """One occupancy measurement. Returns {busy, power_db, peak_db, power, ...}.
    `busy` is power_db > threshold_db. Pass rx_args / rx_gain / rx_freq / rx_rate
    to override the radio; `binary=` to point at a specific sdr_system."""
    return _run_sense(window_ms, 1, threshold_db, **radio)[0]


def calibrate_floor(windows=20, margin_db=6.0, window_ms=10, **radio):
    """Measure the idle noise floor (assumes the channel is IDLE right now) over
    `windows` windows in a single invocation, and return a suggested busy
    threshold = median(power_db) + margin_db. Calibrate once per gain setting."""
    rows = _run_sense(window_ms, windows, -999.0, **radio)   # -999 => nothing flagged busy
    p = sorted(r["power_db"] for r in rows)
    floor = p[len(p) // 2]                                    # median, robust to a stray burst
    thr = floor + margin_db
    print("[calibrate] idle floor ~%.1f dB over %d windows -> busy threshold "
          "%.1f dB (+%.0f dB margin)" % (floor, len(p), thr, margin_db))
    return thr


def should_transmit(p, threshold_db, window_ms=10, **radio):
    """NEXT STEP — p-persistent access: sense the channel; if it's idle, return
    True with probability `p` (else defer); if busy, always defer. Returns a bool.
    One-shot (re-inits the radio); for a tight loop use SenseStream instead."""
    r = sense_channel(window_ms=window_ms, threshold_db=threshold_db, **radio)
    if r["busy"]:
        print("[decide] channel BUSY (%.1f dB) -> defer" % r["power_db"])
        return False
    go = random.random() < p
    print("[decide] channel idle (%.1f dB), p=%.2f -> %s"
          % (r["power_db"], p, "TRANSMIT" if go else "defer"))
    return go


class SenseStream:
    """Persistent channel-sensing feed — ONE radio init, live readings.

    Runs `sdr_system --role sense --sense-count 0` (stream forever) and keeps the
    latest measurement fresh via a background reader thread. Use it for a tight
    sense->decide loop so you don't pay ~1-2 s of radio init on every decision:

        with SenseStream(rx_args="serial=30CD3F7") as s:
            thr = s.calibrate()                    # idle floor + margin, from the feed
            while ...:
                go, r = s.should_transmit(p=0.5, threshold_db=thr)
                if go: ...transmit...

    Thresholding is done in Python (the C++ feed streams raw power_db), so one feed
    serves any threshold. `latest()` always returns the freshest window (the reader
    thread drains the pipe, so a slow consumer never reads stale/backed-up data).
    """

    def __init__(self, window_ms=10, binary=None, **radio):
        opts = {**_DEF, **radio}
        cmd = sdr.SDR(role="sense", sense_window=window_ms, sense_count=0,
                      sense_threshold_db=-999.0, viz=False, binary=binary,
                      **opts).command()                       # -999 => Python owns 'busy'
        self._p = subprocess.Popen(shlex.split(cmd), stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self._win_s = window_ms / 1000.0
        self._last = None
        self._lock = threading.Lock()
        self._stop = False
        self._t = threading.Thread(target=self._reader, daemon=True)
        self._t.start()

    def _reader(self):
        for line in self._p.stdout:
            m = _LINE.search(line)
            if m:
                with self._lock:
                    self._last = {"busy": None, "power_db": float(m.group(2)),
                                  "peak_db": float(m.group(3)), "power": float(m.group(4)),
                                  "window_ms": float(m.group(5)), "samples": int(m.group(6))}
            if self._stop:
                break

    def latest(self, timeout=8.0):
        """Freshest measurement dict. Waits up to `timeout` s for the first one
        (covers radio init); afterwards returns immediately."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._lock:
                if self._last is not None:
                    return dict(self._last)
            time.sleep(0.01)
        raise RuntimeError("no [SENSE] reading within %.1f s (radio error?)" % timeout)

    def calibrate(self, samples=20, margin_db=6.0, settle=0.3):
        """Estimate the idle floor from the live feed; return busy threshold."""
        time.sleep(settle)
        vals = []
        for _ in range(samples):
            vals.append(self.latest()["power_db"])
            time.sleep(self._win_s)
        vals.sort()
        thr = vals[len(vals) // 2] + margin_db
        print("[calibrate] idle floor ~%.1f dB -> busy threshold %.1f dB"
              % (vals[len(vals) // 2], thr))
        return thr

    def busy(self, threshold_db):
        r = self.latest()
        return r["power_db"] > threshold_db, r

    def should_transmit(self, p, threshold_db):
        busy, r = self.busy(threshold_db)
        if busy:
            return False, r
        return (random.random() < p), r

    def close(self):
        self._stop = True
        if self._p.poll() is None:
            self._p.terminate()
            try:
                self._p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._p.kill()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def p_persistent_demo(trials=10, p=0.5, threshold_db=None, window_ms=10,
                      period_s=0.5, **radio):
    """Run the p-persistent sense->decide loop over ONE persistent feed (no per-
    decision radio init). Prints the decision each trial. Returns the decisions."""
    out = []
    with SenseStream(window_ms=window_ms, **radio) as s:
        thr = threshold_db if threshold_db is not None else s.calibrate()
        for i in range(trials):
            go, r = s.should_transmit(p, thr)
            busy = r["power_db"] > thr
            print("[%2d/%d] power=%6.1f dB  busy=%-5s  ->  %s"
                  % (i + 1, trials, r["power_db"], busy,
                     "TRANSMIT" if go else ("defer(busy)" if busy else "defer(p)")))
            out.append(go)
            time.sleep(period_s)
    return out


def main(argv):
    import argparse
    a = argparse.ArgumentParser(description="Channel occupancy sensing over the SDR")
    a.add_argument("--rx-args", default=_DEF["rx_args"])
    a.add_argument("--rx-gain", type=float, default=_DEF["rx_gain"])
    a.add_argument("--rx-freq", type=float, default=_DEF["rx_freq"])
    a.add_argument("--window-ms", type=float, default=10)
    a.add_argument("--count", type=int, default=5)
    a.add_argument("--threshold-db", type=float, default=None,
                   help="busy threshold; omit to auto-calibrate the idle floor first")
    a.add_argument("--calibrate", action="store_true", help="only measure the idle floor")
    a.add_argument("--p", type=float, default=None,
                   help="run the p-persistent sense->decide loop (over a persistent feed)")
    args = a.parse_args(argv)
    radio = dict(rx_args=args.rx_args, rx_gain=args.rx_gain, rx_freq=args.rx_freq)

    if args.calibrate:
        calibrate_floor(window_ms=args.window_ms, **radio)
        return
    if args.p is not None:
        # persistent feed: one radio init, `--count` p-persistent decisions
        p_persistent_demo(trials=args.count, p=args.p, threshold_db=args.threshold_db,
                          window_ms=args.window_ms, **radio)
        return
    thr = args.threshold_db if args.threshold_db is not None \
        else calibrate_floor(window_ms=args.window_ms, **radio)
    # sense N windows in one invocation (one radio init)
    for i, r in enumerate(_run_sense(args.window_ms, args.count, thr, **radio), 1):
        print("[sense %d/%d] busy=%-5s power=%6.1f dB  peak=%6.1f dB"
              % (i, args.count, r["busy"], r["power_db"], r["peak_db"]))


if __name__ == "__main__":
    main(sys.argv[1:])
