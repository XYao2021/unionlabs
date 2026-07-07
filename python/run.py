#!/usr/bin/env python3
"""
run.py — run the USRP B210 SDR PHY from a JSON config file (clearer than a long
command line). Uses the auto-generated sdr.py, so any option name valid there is
valid here (hyphens or underscores).

    python3 run.py config.json              # run it
    python3 run.py config.json --dry-run    # just print the command(s)

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


def run(cfg, dry_run=False):
    """cfg is a dict (or JSON path). Returns the process/return codes."""
    if isinstance(cfg, str):
        with open(cfg) as f:
            cfg = json.load(f)
    kind, *rest = _split(cfg)
    if kind == "single":
        job = rest[0]
        print("[run]", job.command())
        return None if dry_run else job.run()
    rx, tx, pair = rest
    print("[run] RX:", rx.command())
    print("[run] TX:", tx.command())
    return None if dry_run else sdr.run_pair(rx, tx, **pair)


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    dry = "--dry-run" in argv
    if not args:
        sys.exit("usage: python3 run.py <config.json> [--dry-run]")
    rc = run(args[0], dry_run=dry)
    if isinstance(rc, tuple):
        print("[run] return codes (rx, tx):", rc)


if __name__ == "__main__":
    main(sys.argv[1:])
