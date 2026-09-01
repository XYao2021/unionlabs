#!/usr/bin/env python3
"""
range_ber.py — two-host RANGE test logger. Runs the ARQ sink (this Mac, RX radio)
with the per-burst BER diagnostic, and turns each batch of `[BER]` lines produced
by the Dell's source fires into ONE CSV row per distance:

    detection rate = bursts seen / fires expected      (SNR / range gauge)
    delivery rate  = CRC=PASS bursts / fires expected   (clean-decode fraction)
    BER            = pre-FEC / post-FEC median/min/max over the batch

Every PHY knob (scheme, waveform, symbol_rate, U/D, roll-off, filter, preamble,
phase PLL, detector, eq) is exposed AND recorded in each CSV row, so you can run
one walk per config, concatenate the CSVs, and compare distance-vs-knob. The knobs
that must match on the TX are printed as a ready Dell command at start-up.

Companion to power_monitor.py. The Mac sink is the ACK server; the Dell source
fires N single-shot (`--max-attempts 1`) known packets per distance. Use DQPSK:
each Dell fire is a fresh process, so the TX LO cold-restarts per fire and coherent
QPSK can't lock — differential is immune. Keep the payload SYMBOL-VARIED
(marl_phy.known_payload, auto-generated here); all-zeros fakes ~50 % coherent BER.

Workflow (one persistent, warm sink for the whole walk):

    python3 range_ber.py --tag rolloff025 --fires 30 --roll_off 0.25
    # -> prints the matching Dell command, then brings the sink up. At each distance:
    #    1. place the RX     2. type the distance (e.g. "5m")
    #    3. fire N packets from the Dell (the printed command, in a seq loop)
    #    4. it logs a row and prompts for the next.   'q' finishes.

    python3 range_ber.py --plot range_rolloff025.csv     # detection & BER vs distance (no radio)

CSV lands in ../experiments/marl_ra/results/ (or --out).
"""
import argparse
import csv
import os
import queue
import re
import shlex
import socket
import statistics as st
import subprocess
import sys
import threading

_PHY = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _PHY)
import marl_phy  # noqa: E402  (known_payload)

_BIN = os.path.abspath(os.path.join(_PHY, "..", "build", "sdr_system"))
DEFAULT_OUT = os.path.abspath(os.path.join(_PHY, "..", "experiments", "marl_multi", "results"))
_BER = re.compile(
    r"\[BER\]\s+pre-FEC=([\d.]+)%.*?post-FEC payload=([\d.]+)%.*?CRC=(PASS|FAIL)")

# result columns + the PHY config columns (constant per walk, so a row is self-describing)
_RESULT = ["distance", "fires", "bursts", "detection_rate", "crc_ok", "delivery_rate",
           "pre_fec_med", "pre_fec_min", "pre_fec_max", "post_fec_med"]
_PHY_COLS = ["scheme", "waveform", "fec", "symbol_rate", "U", "D", "roll_off",
             "filter_type", "preamble", "m", "add_preamble", "ofdm_fft", "ofdm_cp",
             "bytes_length", "freq", "rx_gain", "rx_bw", "det_mult", "sync_threshold",
             "phase_loop_bw", "phase_damping", "eq_type", "tx_gain_hint"]
_FIELDS = _RESULT + _PHY_COLS


def _phy_pairs(a):
    """The PHY flags that MUST MATCH on both ends (order = readable)."""
    p = [("--scheme", a.scheme), ("--waveform", a.waveform), ("--fec", "true" if a.fec else "false"),
         ("--symbol_rate", int(a.symbol_rate)), ("--U", a.U), ("--D", a.D),
         ("--filter_type", a.filter_type), ("--roll_off", a.roll_off),
         ("--preamble", a.preamble), ("--m", a.m), ("--add_preamble", a.add_preamble),
         ("--bytes-length", a.bytes_length)]
    if a.waveform == "ofdm":
        p += [("--ofdm-fft", a.ofdm_fft), ("--ofdm-cp", a.ofdm_cp)]
    return p


def _sink_cmd(a):
    cmd = [_BIN, "--role", "sink_arq",
           "--rx-args", "serial=%s" % a.rx_serial, "--tx-args", "serial=%s" % a.rx_serial,
           "--rx-subdev", "A:A", "--rx-ant", "RX2", "--rx-freq", repr(a.freq),
           "--rx-rate", repr(a.symbol_rate * a.U / a.D)]
    for k, v in _phy_pairs(a):
        cmd += [k, str(v)]
    # RX-only tuning
    cmd += ["--rx-gain", repr(a.rx_gain), "--rx-bw", repr(a.rx_bw),
            "--det-mult", str(a.det_mult), "--det-adaptive", "1", "--det-continuous", "1",
            "--sync_threshold", str(a.sync_threshold),
            "--phase_loop_bw", str(a.phase_loop_bw), "--phase_damping", str(a.phase_damping),
            "--eq_type", a.eq_type]
    # ARQ + BER
    cmd += ["--ack-transport", "tcp", "--ack-port", str(a.ack_port),
            "--serve-forever", "--ber-expected", a.payload, "--viz", "false"]
    return cmd


def _tx_hint(a, ip):
    """The matching Dell source_arq command — same PHY flags, TX-side RF, ACK to this Mac."""
    lines = ["./sdr_system --role source_arq --tx-args serial=%s \\" % a.tx_serial,
             "  --tx-subdev A:A --tx-ant TX/RX --tx-freq %s --tx-rate %s \\"
             % (repr(a.freq), repr(a.symbol_rate * a.U / a.D))]
    phy = " ".join("%s %s" % (k, v) for k, v in _phy_pairs(a))
    lines.append("  %s \\" % phy)
    tx_extra = "--tx-gain %s --tx-bw %s" % (repr(a.tx_gain), repr(a.rx_bw))
    if a.waveform == "ofdm":
        tx_extra += " --ofdm-tx-peak %s" % a.ofdm_tx_peak
    lines.append("  %s \\" % tx_extra)
    lines.append("  --ack-transport tcp --ack-host %s --ack-port %s \\" % (ip, a.ack_port))
    lines.append("  --payload-file known_payload.bin --max-attempts 1 --timeout 3000")
    loop = ("for i in $(seq 1 %d); do \\\n  %s\n  sleep 0.5\ndone"
            % (a.fires, "\n  ".join(lines)))
    return loop


def _mac_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))          # no packet sent; just picks the primary iface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "<Mac-IP>"


def _reader(proc, q):
    for line in iter(proc.stdout.readline, ""):
        m = _BER.search(line)
        if m:
            q.put((float(m.group(1)), float(m.group(2)), m.group(3) == "PASS"))


def _collect_batch(q, fires, first_wait, idle):
    bursts = []
    try:
        bursts.append(q.get(timeout=first_wait))
    except queue.Empty:
        return bursts  # nothing detected -> detection 0 (dead at this distance)
    while len(bursts) < fires:
        try:
            bursts.append(q.get(timeout=idle))
        except queue.Empty:
            break  # Dell loop finished / link dropped mid-batch
    return bursts


def _row(distance, fires, bursts, a):
    pre = [b[0] for b in bursts]
    post = [b[1] for b in bursts]
    ok = sum(1 for b in bursts if b[2])
    n = len(bursts)
    row = {
        "distance": distance, "fires": fires, "bursts": n,
        "detection_rate": round(n / fires, 4) if fires else 0.0,
        "crc_ok": ok, "delivery_rate": round(ok / fires, 4) if fires else 0.0,
        "pre_fec_med": round(st.median(pre), 3) if pre else "",
        "pre_fec_min": round(min(pre), 3) if pre else "",
        "pre_fec_max": round(max(pre), 3) if pre else "",
        "post_fec_med": round(st.median(post), 3) if post else "",
    }
    row.update({
        "scheme": a.scheme, "waveform": a.waveform, "fec": int(a.fec),
        "symbol_rate": int(a.symbol_rate), "U": a.U, "D": a.D, "roll_off": a.roll_off,
        "filter_type": a.filter_type, "preamble": a.preamble, "m": a.m,
        "add_preamble": a.add_preamble, "ofdm_fft": a.ofdm_fft, "ofdm_cp": a.ofdm_cp,
        "bytes_length": a.bytes_length, "freq": a.freq, "rx_gain": a.rx_gain,
        "rx_bw": a.rx_bw, "det_mult": a.det_mult, "sync_threshold": a.sync_threshold,
        "phase_loop_bw": a.phase_loop_bw, "phase_damping": a.phase_damping,
        "eq_type": a.eq_type, "tx_gain_hint": a.tx_gain,
    })
    return row


def _append(path, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def walk(a):
    if not os.path.exists(_BIN):
        sys.exit("[range] binary not found: %s (build it first)" % _BIN)
    # integer samples/symbol guard (the C++ enforces it too, but fail fast + clearer).
    # os = rx_rate/symbol_rate = U/D must be an integer, so D must divide U.
    if a.D == 0 or a.U % a.D != 0:
        sys.exit("[range] U/D = %s/%s is not an integer samples/symbol (os = U/D). "
                 "Pick U divisible by D (e.g. 2/1, 4/1, 4/2)." % (a.U, a.D))
    if a.waveform == "ofdm" and a.scheme.startswith("D"):
        print("[range] WARNING: differential scheme on OFDM — OFDM pilots already track phase; "
              "this fights per-symbol CPE. Use a non-differential scheme with OFDM.")
    os.makedirs(a.out, exist_ok=True)
    if not os.path.exists(a.payload):
        with open(a.payload, "wb") as f:
            f.write(marl_phy.known_payload(a.bytes_length))
        print("[range] wrote varied known payload %s (copy this SAME file to the Dell)\n"
              % a.payload)
    csv_path = os.path.join(a.out, "range_%s.csv" % a.tag)

    ip = _mac_ip()
    print("=" * 74)
    print("MATCHING DELL COMMAND (same PHY; fires %d packets/distance; ACK -> %s):" % (a.fires, ip))
    print("-" * 74)
    print(_tx_hint(a, ip))
    print("=" * 74 + "\n")

    cmd = _sink_cmd(a)
    if a.dry_run:
        print("SINK:\n" + " ".join(shlex.quote(c) for c in cmd)); return
    print("[range] starting sink (warm, --serve-forever). CSV -> %s" % csv_path)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, bufsize=1)
    q = queue.Queue()
    threading.Thread(target=_reader, args=(proc, q), daemon=True).start()

    try:
        while True:
            d = input("\ndistance (label, e.g. 5m) or 'q' to finish > ").strip()
            if d.lower() in ("q", "quit", "exit", ""):
                break
            while not q.empty():
                q.get_nowait()          # drop stragglers from the previous batch
            print("[range]  fire %d packets from the Dell now. Collecting..." % a.fires)
            bursts = _collect_batch(q, a.fires, a.first_wait, a.idle)
            row = _row(d, a.fires, bursts, a)
            _append(csv_path, row)
            print("[range]  %s: detected %d/%d (%.0f%%)  clean %d/%d (%.0f%%)  "
                  "pre-FEC med %s%%  post-FEC med %s%%"
                  % (d, row["bursts"], a.fires, 100 * row["detection_rate"],
                     row["crc_ok"], a.fires, 100 * row["delivery_rate"],
                     row["pre_fec_med"], row["post_fec_med"]))
    except (KeyboardInterrupt, EOFError):
        print("\n[range] stopping.")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    print("[range] wrote %s" % csv_path)


def plot(csv_path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        sys.exit("[range] matplotlib needed for --plot: %s" % e)
    xs, det, deliv, ber = [], [], [], []
    label = ""
    with open(csv_path) as f:
        for r in csv.DictReader(f):
            xs.append(r["distance"])
            det.append(float(r["detection_rate"]) * 100)
            deliv.append(float(r["delivery_rate"]) * 100)
            ber.append(float(r["pre_fec_med"]) if r["pre_fec_med"] else float("nan"))
            label = "%s %s U/D=%s/%s roll=%s" % (r.get("scheme", ""), r.get("waveform", ""),
                                                 r.get("U", ""), r.get("D", ""), r.get("roll_off", ""))
    fig, ax1 = plt.subplots(figsize=(8, 4.5))
    ax1.plot(xs, det, "o-", color="tab:blue", label="detection %")
    ax1.plot(xs, deliv, "s-", color="tab:green", label="delivery (CRC-clean) %")
    ax1.set_xlabel("distance"); ax1.set_ylabel("rate (%)"); ax1.set_ylim(-2, 102)
    ax2 = ax1.twinx()
    ax2.plot(xs, ber, "^--", color="tab:red", label="pre-FEC BER median %")
    ax2.set_ylabel("pre-FEC BER (%)", color="tab:red")
    ax1.legend(loc="lower left"); ax2.legend(loc="upper right")
    ax1.set_title("Range vs distance  [%s]" % label.strip())
    fig.tight_layout()
    out = os.path.splitext(csv_path)[0] + ".png"
    fig.savefig(out, dpi=120)
    print("[range] wrote %s" % out)


def main(argv):
    a = argparse.ArgumentParser(description="Two-host B210 range test logger (BER vs distance, PHY-sweepable)")
    a.add_argument("--plot", metavar="CSV", help="plot a prior range CSV and exit (no radio)")
    a.add_argument("--tag", default="walk", help="CSV label (range_<tag>.csv) — use one per PHY config")
    a.add_argument("--fires", type=int, default=30, help="packets the Dell fires per distance")
    a.add_argument("--dry-run", action="store_true", help="print the sink + Dell commands and exit")
    # ---- PHY (MUST MATCH the Dell; recorded per row) ----
    a.add_argument("--scheme", default="QPSK", help="QPSK (system default) / DQPSK (robust to per-fire cold LO) / BPSK / ...")
    a.add_argument("--waveform", default="sc", choices=["sc", "ofdm"])
    a.add_argument("--fec", type=lambda s: s.lower() in ("1", "true", "yes"), default=True)
    a.add_argument("--symbol_rate", type=float, default=800000)
    a.add_argument("--U", type=int, default=2, help="pulse-shaper upsample (wire sps = U/D)")
    a.add_argument("--D", type=int, default=1, help="pulse-shaper downsample")
    a.add_argument("--roll_off", type=float, default=0.25)
    a.add_argument("--filter_type", default="rrc", choices=["rrc", "rc", "lp"])
    a.add_argument("--preamble", default="m-sequence", choices=["m-sequence", "zadoff"])
    a.add_argument("--m", type=int, default=5, help="m-sequence order (len 2^m-1)")
    a.add_argument("--add_preamble", type=int, default=1, choices=[0, 1])
    a.add_argument("--ofdm-fft", type=int, default=64, dest="ofdm_fft")
    a.add_argument("--ofdm-cp", type=int, default=16, dest="ofdm_cp")
    a.add_argument("--ofdm-tx-peak", type=float, default=0.5, dest="ofdm_tx_peak", help="TX-only (Dell)")
    a.add_argument("--bytes-length", type=int, default=marl_phy.PACKET_BYTES, dest="bytes_length")
    a.add_argument("--freq", type=float, default=915e6)
    # ---- RX-only tuning ----
    a.add_argument("--rx-gain", type=float, default=40, dest="rx_gain")
    a.add_argument("--rx-bw", type=float, default=1000000, dest="rx_bw")
    a.add_argument("--det-mult", type=float, default=3, dest="det_mult")
    a.add_argument("--sync_threshold", type=float, default=15)
    a.add_argument("--phase_loop_bw", type=float, default=0.02)
    a.add_argument("--phase_damping", type=float, default=0.707)
    a.add_argument("--eq_type", default="None", choices=["None", "LMS", "RLS", "DFE"])
    # ---- addressing / hint ----
    a.add_argument("--tx-gain", type=float, default=88, dest="tx_gain", help="recorded + put in the Dell hint")
    a.add_argument("--rx-serial", default="30CD3F7", dest="rx_serial")
    a.add_argument("--tx-serial", default="30CD424", dest="tx_serial", help="for the Dell hint")
    a.add_argument("--ack-port", type=int, default=5599, dest="ack_port")
    a.add_argument("--payload", default=os.path.join(os.path.dirname(_BIN), "known_payload.bin"))
    a.add_argument("--first-wait", type=float, default=25.0, dest="first_wait",
                   help="s to wait for the FIRST burst before calling the distance dead")
    a.add_argument("--idle", type=float, default=8.0, help="s of no new burst that ends the batch")
    a.add_argument("--out", default=DEFAULT_OUT)
    args = a.parse_args(argv)
    if args.plot:
        plot(args.plot)
    else:
        walk(args)


if __name__ == "__main__":
    main(sys.argv[1:])
