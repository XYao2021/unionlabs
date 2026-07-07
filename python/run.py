#!/usr/bin/env python3
"""
run.py — run the USRP B210 SDR PHY from a JSON config file (clearer than a long
command line). Uses the auto-generated sdr.py, so any option name valid there is
valid here (hyphens or underscores).

    python3 run.py config.json              # run it (both ends if it's a pair)
    python3 run.py config.json --dry-run    # just print the command(s)
    python3 run.py config.json --only rx    # run ONLY the RX side (own terminal)
    python3 run.py config.json --only tx    # run ONLY the TX side (own terminal)

Run TX and RX in separate terminals from one paired config:
    # terminal 1:
    python3 run.py configs/qpsk_arq_pair.json --only rx
    # terminal 2:
    python3 run.py configs/qpsk_arq_pair.json --only tx

Two config shapes
-----------------
1) Single run — an object with a "role" (plus any options):

    {
      "role": "source_arq",
      "tx_args": "serial=30CD424",
      "scheme": "QPSK", "tx_gain": 78, "fec": true
    }

2) Paired run (BOTH ends at once) — objects "rx" and "tx"; anything in "common"
   is merged into both; optional "pair" tunes run_pair():

    {
      "common": { "rx_freq": 915e6, "tx_freq": 915e6, "rx_rate": 1.6e6,
                  "tx_rate": 1.6e6, "scheme": "QPSK", "fec": true },
      "rx": { "role": "sink_arq",   "rx_args": "serial=30CD3F7", "rx_gain": 20 },
      "tx": { "role": "source_arq", "tx_args": "serial=30CD424", "tx_gain": 78 },
      "pair": { "rx_head_start": 4, "rx_grace": 30 }
    }

An optional top-level "binary" overrides the sdr_system path in either shape.
"""
import json
import sys
import sdr


def _opts(d):
    """Drop comment keys (anything starting with '_') from an options dict."""
    return {k: v for k, v in d.items() if not k.startswith("_")}


def _split(cfg):
    """Return ('single', SDR) or ('pair', rx_SDR, tx_SDR, pair_opts)."""
    cfg = dict(cfg)
    binary = cfg.pop("binary", None)
    if "rx" in cfg and "tx" in cfg:
        common = _opts(cfg.get("common", {}))
        rx = sdr.SDR(binary=binary, **{**common, **_opts(cfg["rx"])})
        tx = sdr.SDR(binary=binary, **{**common, **_opts(cfg["tx"])})
        return ("pair", rx, tx, _opts(cfg.get("pair", {})))
    # single run
    for k in ("common", "rx", "tx", "pair"):
        cfg.pop(k, None)
    cfg = _opts(cfg)
    if "role" not in cfg:
        raise ValueError('single-run config needs a "role" (or use "rx"+"tx" for a pair)')
    return ("single", sdr.SDR(binary=binary, **cfg))


def run(cfg, dry_run=False, only=None):
    """cfg is a dict (or JSON path). only=None runs both ends of a pair via
    run_pair; only='rx'/'tx' runs just that side (for a separate terminal)."""
    if isinstance(cfg, str):
        with open(cfg) as f:
            cfg = json.load(f)
    kind, *rest = _split(cfg)
    if kind == "single":
        if only:
            print("[run] note: --only ignored (config is a single run)")
        job = rest[0]
        print("[run]", job.command())
        return None if dry_run else job.run()
    rx, tx, pair = rest
    if only == "rx":
        print("[run] RX:", rx.command())
        return None if dry_run else rx.run()
    if only == "tx":
        print("[run] TX:", tx.command())
        return None if dry_run else tx.run()
    print("[run] RX:", rx.command())
    print("[run] TX:", tx.command())
    return None if dry_run else sdr.run_pair(rx, tx, **pair)


def main(argv):
    dry = "--dry-run" in argv
    only = None
    if "--only" in argv:                       # "--only rx"
        i = argv.index("--only")
        only = argv[i + 1] if i + 1 < len(argv) else None
    for a in argv:                             # "--only=rx"
        if a.startswith("--only="):
            only = a.split("=", 1)[1]
    if only not in (None, "rx", "tx"):
        sys.exit("--only must be 'rx' or 'tx'")
    files = [a for a in argv if not a.startswith("--") and a != only]
    if not files:
        sys.exit("usage: python3 run.py <config.json> [--dry-run] [--only rx|tx]")
    rc = run(files[0], dry_run=dry, only=only)
    if isinstance(rc, tuple):
        print("[run] return codes (rx, tx):", rc)


if __name__ == "__main__":
    main(sys.argv[1:])
