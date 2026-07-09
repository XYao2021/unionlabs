#!/usr/bin/env python3
"""ber_monitor.py — long-term link BER monitor (no training, just characterize the
channel). Fires a KNOWN packet over the real B210 link at a fixed cadence for a long
run and records, per burst, a wall-clock timestamp + pre-FEC (channel) and post-FEC
(payload) BER + CRC + whether it was detected at all. Saves a time-series CSV and a
trajectory plot so you can SEE how the channel drifts: good/bad windows, detection
rate, delivery (CRC-pass) rate over minutes/hours.

This is `marl_phy.py ber` extended into time: same warm AP + known-payload probe
(the C++ `--ber-expected` sink), but it timestamps each burst (the sink fflushes each
[BER] line) instead of only printing end-of-run min/median/max.

    python3 ber_monitor.py --minutes 30                 # run 30 min, DQPSK, default gains
    python3 ber_monitor.py --bursts 200 --period 2      # or a fixed number of bursts
    python3 ber_monitor.py --scheme QPSK --tx-gain 85 --rx-gain 40 --tag qpsk_night
"""
import argparse
import os
import sys
import time

from marl_phy import (AccessPoint, transmit_once, WarmSource, PACKET_BYTES,
                      known_payload, _BER_LINE)

# results land next to the MARL experiments (easy to find, git-ignored)
DEFAULT_OUT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "applications", "MARL_RA_Union", "results"))


def _fmt_hms(sec):
    sec = int(sec)
    return "%d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


def monitor(minutes=None, bursts=None, period_s=2.0, scheme="DQPSK",
            tx_args="serial=30CD424", rx_args="serial=30CD3F7",
            tx_gain=85, rx_gain=40, out_dir=DEFAULT_OUT, tag="ber_longrun",
            ap_log="/tmp/ber_monitor_ap.log", binary=None, print_every=1, warm=False):
    """Fire known bursts for `minutes` (or `bursts` count) and record a per-burst
    time-series. Returns the list of row dicts. Also writes <tag>.csv and <tag>.png
    into out_dir and prints a running + final summary.

    warm=False: each burst re-inits the TX radio (transmit_once) — the LO restarts
      every burst, so the CFO jumps (worst case for coherent QPSK's carrier PLL).
    warm=True : one persistent WarmSource — the LO runs continuously, so the CFO is
      stable and trackable (mirrors a normal C++ source_arq run with --interval gaps).
      This is the fair way to test whether coherent QPSK holds on this link."""
    if minutes is None and bursts is None:
        minutes = 10.0                                    # sane default
    pkt = known_payload()                                 # varied known ground truth

    os.makedirs(out_dir, exist_ok=True)
    ap = AccessPoint(rx_args=rx_args, rx_gain=rx_gain, ber_expected=pkt,
                     scheme=scheme, binary=binary)
    print("[monitor] scheme=%s tx_gain=%s rx_gain=%s period=%ss src=%s  target=%s"
          % (scheme, tx_gain, rx_gain, period_s, "WARM" if warm else "cold-reinit",
             ("%.0f min" % minutes) if minutes else ("%d bursts" % bursts)))
    print("[monitor] warming AP (RX + ACK) ...")
    ap.start(log=ap_log)
    src = None
    if warm:
        print("[monitor] warming TX source (continuous LO) ...")
        src = WarmSource(payload=pkt, tx_args=tx_args, tx_gain=tx_gain, scheme=scheme,
                         binary=binary)

    # seek past the warmup log so we only read [BER] lines from OUR bursts.
    # errors="replace": the sink echoes received payload bytes, and a varied binary
    # ground-truth payload is not valid UTF-8 — decode leniently so it never crashes.
    logf = open(ap_log, "r", errors="replace")
    logf.seek(0, os.SEEK_END)

    rows = []
    t0 = time.time()
    i = 0
    try:
        while True:
            elapsed = time.time() - t0
            if minutes is not None and elapsed >= minutes * 60:
                break
            if bursts is not None and i >= bursts:
                break
            i += 1
            t_fire = time.time() - t0
            try:
                if warm:
                    acked = src.fire()
                else:
                    acked = transmit_once(payload=pkt, tx_args=tx_args, tx_gain=tx_gain,
                                          scheme=scheme, binary=binary)
            except RuntimeError:
                acked = False
            time.sleep(0.4)                               # let the flushed [BER] line land

            # read whatever [BER] lines appeared since the last burst
            new = [_BER_LINE.search(ln) for ln in logf.readlines()]
            new = [m for m in new if m]
            if new:                                       # take the most recent
                m = new[-1]
                row = dict(i=i, t=t_fire, detected=True,
                           pre_fec=float(m.group(1)), post_fec=float(m.group(2)),
                           crc=m.group(3), acked=bool(acked))
            else:                                         # fired but nothing decoded
                row = dict(i=i, t=t_fire, detected=False, pre_fec=None,
                           post_fec=None, crc=None, acked=bool(acked))
            rows.append(row)

            if print_every and i % print_every == 0:
                if row["detected"]:
                    print("[%s] #%-4d pre=%5.2f%% post=%5.2f%% CRC=%s%s"
                          % (_fmt_hms(elapsed), i, row["pre_fec"], row["post_fec"],
                             row["crc"], "  ACK" if row["acked"] else ""))
                else:
                    print("[%s] #%-4d  <no detect>" % (_fmt_hms(elapsed), i))

            slack = period_s - (time.time() - t0 - t_fire)
            if slack > 0:
                time.sleep(slack)
    except KeyboardInterrupt:
        print("\n[monitor] interrupted — saving what we have (%d bursts)" % len(rows))
    finally:
        if src is not None:
            src.close()
        ap.stop()
        logf.close()

    _save_csv(rows, out_dir, tag)
    _summary(rows, time.time() - t0)
    _plot(rows, out_dir, tag, scheme)
    return rows


def _save_csv(rows, out_dir, tag):
    path = os.path.join(out_dir, tag + ".csv")
    with open(path, "w") as f:
        f.write("i,t_s,detected,pre_fec_pct,post_fec_pct,crc,acked\n")
        for r in rows:
            f.write("%d,%.2f,%d,%s,%s,%s,%d\n" % (
                r["i"], r["t"], int(r["detected"]),
                "" if r["pre_fec"] is None else "%.2f" % r["pre_fec"],
                "" if r["post_fec"] is None else "%.2f" % r["post_fec"],
                r["crc"] or "", int(r["acked"])))
    print("[monitor] wrote %s" % path)


def _summary(rows, dur_s):
    n = len(rows)
    det = [r for r in rows if r["detected"]]
    npass = sum(r["crc"] == "PASS" for r in det)
    print("\n================  LINK SUMMARY  ================")
    print("duration        : %s  (%d bursts)" % (_fmt_hms(dur_s), n))
    if not n:
        print("no bursts fired"); return
    print("detection rate  : %d/%d = %.0f%%  (burst seen by the AP at all)"
          % (len(det), n, 100.0 * len(det) / n))
    print("delivery rate   : %d/%d = %.0f%%  (CRC pass = usable payload)"
          % (npass, n, 100.0 * npass / n))
    if det:
        import statistics as st
        pre = [r["pre_fec"] for r in det]
        post = [r["post_fec"] for r in det]
        print("pre-FEC  BER %%  : min/median/max = %.2f / %.2f / %.2f"
              % (min(pre), st.median(pre), max(pre)))
        print("post-FEC BER %%  : min/median/max = %.2f / %.2f / %.2f"
              % (min(post), st.median(post), max(post)))
        catastrophic = sum(r["post_fec"] > r["pre_fec"] for r in det)
        print("FEC catastrophic: %d/%d bursts had post-FEC > pre-FEC (code overwhelmed)"
              % (catastrophic, len(det)))
    print("===============================================")


def _plot(rows, out_dir, tag, scheme):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        print("[monitor] matplotlib unavailable (%s) — CSV only" % e)
        return
    det = [r for r in rows if r["detected"]]
    tm = [r["t"] / 60.0 for r in det]                     # minutes
    pre = [r["pre_fec"] for r in det]
    post = [r["post_fec"] for r in det]

    fig, ax = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    ax[0].plot(tm, pre, ".-", ms=4, lw=0.8, label="pre-FEC (channel)")
    ax[0].plot(tm, post, ".-", ms=4, lw=0.8, label="post-FEC (payload)")
    # mark bursts the AP never even detected
    miss = [r["t"] / 60.0 for r in rows if not r["detected"]]
    for x in miss:
        ax[0].axvline(x, color="0.85", lw=0.6, zorder=0)
    ax[0].set_ylabel("BER  (%)")
    ax[0].set_title("Link BER over time — %s (n=%d, %d not detected)"
                    % (scheme, len(rows), len(miss)))
    ax[0].legend(loc="upper right", fontsize=9)
    ax[0].grid(alpha=0.3)

    # rolling delivery (CRC pass) rate over a sliding window of bursts
    W = max(5, len(rows) // 20)
    xs, roll = [], []
    for k in range(len(rows)):
        lo = max(0, k - W + 1)
        win = rows[lo:k + 1]
        good = sum(r["crc"] == "PASS" for r in win)
        xs.append(rows[k]["t"] / 60.0)
        roll.append(100.0 * good / len(win))
    ax[1].plot(xs, roll, "-", color="tab:green", lw=1.2)
    ax[1].set_ylim(-2, 102)
    ax[1].set_ylabel("delivery rate (%%)\n(CRC pass, %d-burst window)" % W)
    ax[1].set_xlabel("time (minutes)")
    ax[1].grid(alpha=0.3)

    fig.tight_layout()
    path = os.path.join(out_dir, tag + ".png")
    fig.savefig(path, dpi=110)
    print("[monitor] wrote %s" % path)


def main(argv):
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    a = argparse.ArgumentParser(
        description="Long-term link BER monitor (no training — characterize the channel)")
    g = a.add_mutually_exclusive_group()
    g.add_argument("--minutes", type=float, help="run for N minutes")
    g.add_argument("--bursts", type=int, help="run for N bursts instead")
    a.add_argument("--period", type=float, default=2.0, help="seconds between bursts")
    a.add_argument("--scheme", default="DQPSK", help="modulation (must match both ends)")
    a.add_argument("--tx-args", default="serial=30CD424")
    a.add_argument("--rx-args", default="serial=30CD3F7")
    a.add_argument("--tx-gain", type=float, default=85)
    a.add_argument("--rx-gain", type=float, default=40)
    a.add_argument("--tag", default="ber_longrun", help="output file stem")
    a.add_argument("--out", default=DEFAULT_OUT, help="output directory")
    a.add_argument("--print-every", type=int, default=1)
    a.add_argument("--warm", action="store_true",
                   help="keep the TX radio warm (continuous LO, stable CFO) instead of "
                        "re-initializing per burst — the fair test for coherent QPSK")
    args = a.parse_args(argv)
    monitor(minutes=args.minutes, bursts=args.bursts, period_s=args.period,
            scheme=args.scheme, tx_args=args.tx_args, rx_args=args.rx_args,
            tx_gain=args.tx_gain, rx_gain=args.rx_gain, out_dir=args.out,
            tag=args.tag, print_every=args.print_every, warm=args.warm)


if __name__ == "__main__":
    main(sys.argv[1:])
