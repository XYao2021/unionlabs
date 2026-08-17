#!/usr/bin/env python3
"""
tx_repeats_viz.py — visualize the ARQ transmitter's "[SOURCE] Per-chunk
transmission summary" (how many times each chunk was sent before it was ACKed).

The C++ source_arq prints, per chunk:
    [SOURCE]   chunk #1/2 : tried 3 times  ->  ACKed
plus a line:
    [SOURCE] Done. Sent=5  Retransmissions=3  Unacked chunks=0

This parses that (from a log file, or piped live) and draws a bar chart of
attempts-per-chunk — 1 = clean first-try, taller = a worse link on that chunk,
red/hatched = gave up. A retransmission-count picture is a direct link-quality gauge.

Usage:
    # live: tee keeps the console output AND feeds the plot
    ./sdr_system --role source_arq ... --scheme QPSK | python3 tx_repeats_viz.py --out qpsk_repeats.png

    # from a saved log
    ./sdr_system --role source_arq ... | tee tx.log
    python3 tx_repeats_viz.py --log tx.log

PNG lands next to --out (default ../experiments/marl_ra/results/tx_repeats.png).
"""
import argparse
import os
import re
import sys

_CHUNK = re.compile(r"chunk #(\d+)/(\d+)\s*:\s*tried\s+(\d+)\s+time.*?->\s*(ACKed|NOT ACKed)", re.I)
_DONE = re.compile(r"Done\.\s*Sent=(\d+)\s+Retransmissions=(\d+)\s+Unacked chunks=(\d+)", re.I)
DEFAULT_OUT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           "..", "marl_multi",
                                           "results", "tx_repeats.png"))


def parse(lines):
    chunks, totals = {}, None
    for ln in lines:
        m = _CHUNK.search(ln)
        if m:
            idx, tot, tries, ok = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4).upper() == "ACKED"
            chunks[idx] = (tries, ok, tot)
        d = _DONE.search(ln)
        if d:
            totals = dict(sent=int(d.group(1)), retx=int(d.group(2)), unacked=int(d.group(3)))
    rows = [(i, chunks[i][0], chunks[i][1]) for i in sorted(chunks)]  # (chunk, tries, acked)
    return rows, totals


def plot(rows, totals, out):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Patch
    except Exception as e:
        sys.exit("[tx-viz] matplotlib needed: %s" % e)
    xs = [r[0] for r in rows]
    tries = [r[1] for r in rows]
    # accessible encoding: ACKed = teal, gave-up = red + hatch (distinct in grayscale too)
    colors = ["#2a9d8f" if r[2] else "#e63946" for r in rows]
    hatch = [None if r[2] else "//" for r in rows]

    fig, ax = plt.subplots(figsize=(max(6, 0.5 * len(xs) + 2), 4.5))
    bars = ax.bar(xs, tries, color=colors, edgecolor="black", linewidth=0.6)
    for b, h in zip(bars, hatch):
        if h:
            b.set_hatch(h)
    for b, t in zip(bars, tries):
        ax.text(b.get_x() + b.get_width() / 2, t + 0.05, str(t),
                ha="center", va="bottom", fontsize=9, fontweight="bold")
    ax.axhline(1, color="gray", ls="--", lw=1, zorder=0)
    ax.text(xs[0] - 0.4, 1.02, "ideal = 1 try", color="gray", fontsize=8, va="bottom")
    ax.set_xlabel("chunk #")
    ax.set_ylabel("transmission attempts")
    ax.set_xticks(xs)
    ax.set_ylim(0, max(tries) + 1)
    title = "ARQ retransmissions per chunk"
    if totals:
        title += "   (Sent %d · Retx %d · Unacked %d)" % (totals["sent"], totals["retx"], totals["unacked"])
    ax.set_title(title)
    ax.legend(handles=[Patch(facecolor="#2a9d8f", edgecolor="black", label="ACKed"),
                       Patch(facecolor="#e63946", edgecolor="black", hatch="//", label="gave up")],
              loc="upper right", fontsize=9)
    fig.tight_layout()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    fig.savefig(out, dpi=120)
    print("[tx-viz] wrote %s" % out)


def main(argv):
    a = argparse.ArgumentParser(description="Visualize ARQ per-chunk retransmission summary")
    a.add_argument("--log", help="source_arq log file (default: read stdin, echoing it through)")
    a.add_argument("--out", default=DEFAULT_OUT, help="output PNG path")
    args = a.parse_args(argv)

    if args.log:
        with open(args.log) as f:
            lines = f.readlines()
    else:  # live pipe: echo each line so the console still shows the TX output
        lines = []
        for ln in sys.stdin:
            sys.stdout.write(ln)
            lines.append(ln)

    rows, totals = parse(lines)
    if not rows:
        sys.exit("[tx-viz] no '[SOURCE] chunk #.. tried .. ' lines found — "
                 "run --role source_arq (the ARQ path prints this table).")
    # tidy text table too
    print("\n=== per-chunk repeats ===")
    for c, t, ok in rows:
        print("  chunk %-3d attempts=%-3d %s" % (c, t, "ACKed" if ok else "GAVE UP"))
    if totals:
        print("  totals: sent=%(sent)d retx=%(retx)d unacked=%(unacked)d" % totals)
    plot(rows, totals, args.out)


if __name__ == "__main__":
    main(sys.argv[1:])
