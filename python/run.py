#!/usr/bin/env python3
"""
run.py — run the USRP B210 SDR PHY from a JSON config file (clearer than a long
command line). Uses the auto-generated sdr.py, so any option name valid there is
valid here (hyphens or underscores).

    python3 run.py --list                   # list every config with scheme + gains
    python3 run.py config.json              # run it (both ends if it's a pair)
    python3 run.py config.json --dry-run    # just print the command(s)
    python3 run.py config.json --only rx    # run ONLY the RX side (own terminal)
    python3 run.py config.json --only tx    # run ONLY the TX side (own terminal)

Command-line options OVERRIDE the config (CLI wins) — handy for changing just a
couple of things without editing the file:
    python3 run.py config.json --scheme 16-QAM --tx-gain 82 --rx-gain 18
    python3 run.py config.json --scheme=8-PSK --waveform ofdm

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
import glob
import json
import os
import sys
import sdr


def _opts(d):
    """Drop comment keys (anything starting with '_') from an options dict."""
    return {k: v for k, v in d.items() if not k.startswith("_")}


def _summary_fields(cfg):
    """(shape, scheme, waveform, gains) for one config dict — used by --list."""
    def pick(d, *ks):
        for k in ks:
            if k in d:
                return d[k]
        return None
    if "rx" in cfg and "tx" in cfg:                     # paired config
        common = _opts(cfg.get("common", {}))
        rx = {**common, **_opts(cfg["rx"])}
        tx = {**common, **_opts(cfg["tx"])}
        scheme = pick(tx, "scheme") or pick(rx, "scheme") or "-"
        wave = pick(common, "waveform") or pick(tx, "waveform") or "sc"
        rg = pick(rx, "rx_gain", "rx-gain")
        tg = pick(tx, "tx_gain", "tx-gain")
        return ("pair", str(scheme), str(wave), "rx %s / tx %s" % (rg, tg))
    c = _opts({k: v for k, v in cfg.items()             # single-run config
               if k not in ("common", "rx", "tx", "pair")})
    role = pick(c, "role") or "-"
    scheme = pick(c, "scheme") or "-"
    wave = pick(c, "waveform") or "sc"
    g = pick(c, "tx_gain", "tx-gain", "rx_gain", "rx-gain")
    return (role, str(scheme), str(wave), "gain %s" % g if g is not None else "-")


def list_configs():
    """Print every configs/*.json with its scheme, waveform and gains."""
    cdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "configs")
    files = sorted(glob.glob(os.path.join(cdir, "*.json")))
    if not files:
        print("no configs found in", cdir)
        return
    rows = []
    for path in files:
        name = os.path.basename(path)
        try:
            with open(path) as f:
                rows.append((name,) + _summary_fields(json.load(f)))
        except Exception as e:
            rows.append((name, "?", "invalid JSON: %s" % e, "", ""))
    w = max(len(r[0]) for r in rows)
    print("%-*s  %-5s  %-8s  %-5s  %s" % (w, "config", "shape", "scheme",
                                          "wave", "gains"))
    print("%-*s  %-5s  %-8s  %-5s  %s" % (w, "-" * w, "-----", "-" * 8,
                                          "-----", "-----"))
    for name, shape, scheme, wave, gains in rows:
        print("%-*s  %-5s  %-8s  %-5s  %s" % (w, name, shape, scheme, wave, gains))
    print("\nrun one with:  python3 run.py configs/<config>  "
          "[--only rx|tx] [--<option> <value> ...]")


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


def run(cfg, dry_run=False, only=None, overrides=None):
    """cfg is a dict (or JSON path). overrides (from the command line) win over
    the config and apply to both ends of a pair. only='rx'/'tx' runs one side."""
    overrides = overrides or {}
    if isinstance(cfg, str):
        with open(cfg) as f:
            cfg = json.load(f)
    kind, *rest = _split(cfg)
    if kind == "single":
        if only:
            print("[run] note: --only ignored (config is a single run)")
        job = rest[0].set(**overrides)
        print("[run]", job.command())
        return None if dry_run else job.run()
    rx, tx, pair = rest
    rx.set(**overrides)                        # CLI overrides -> both ends
    tx.set(**overrides)
    if only == "rx":
        print("[run] RX:", rx.command())
        return None if dry_run else rx.run()
    if only == "tx":
        print("[run] TX:", tx.command())
        return None if dry_run else tx.run()
    print("[run] RX:", rx.command())
    print("[run] TX:", tx.command())
    return None if dry_run else sdr.run_pair(rx, tx, **pair)


def _parse(argv):
    """Return (config_path, dry_run, only, overrides). Any --option [value] not
    recognised as a run.py flag is a config override (CLI wins over the file);
    option arity is looked up in sdr.OPTIONS so bare flags need no value."""
    config, dry, only, lst, ov = None, False, None, False, {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--list":
            lst = True; i += 1
        elif a == "--dry-run":
            dry = True; i += 1
        elif a == "--only":
            only = argv[i + 1] if i + 1 < len(argv) else None; i += 2
        elif a.startswith("--only="):
            only = a.split("=", 1)[1]; i += 1
        elif a.startswith("--"):
            if "=" in a:
                k, v = a[2:].split("=", 1); ov[k] = v; i += 1
            else:
                k = a[2:]
                try:
                    cpp = sdr._resolve(k)       # validates the name
                except KeyError:
                    sys.exit("unknown option --%s (run: python3 sdr.py to list them)" % k)
                if not sdr.OPTIONS[cpp][0]:     # bool-switch flag → no value
                    ov[k] = True; i += 1
                else:
                    if i + 1 >= len(argv):
                        sys.exit("--%s needs a value" % k)
                    ov[k] = argv[i + 1]; i += 2
        elif config is None:
            config = a; i += 1
        else:
            sys.exit("unexpected argument: %s" % a)
    if only not in (None, "rx", "tx"):
        sys.exit("--only must be 'rx' or 'tx'")
    return config, dry, only, lst, ov


def main(argv):
    config, dry, only, lst, ov = _parse(argv)
    if lst:
        list_configs()
        return
    if not config:
        sys.exit("usage: python3 run.py <config.json> [--dry-run] [--only rx|tx] "
                 "[--<option> <value> ...]   |   python3 run.py --list")
    rc = run(config, dry_run=dry, only=only, overrides=ov)
    if isinstance(rc, tuple):
        print("[run] return codes (rx, tx):", rc)


if __name__ == "__main__":
    main(sys.argv[1:])
