#!/usr/bin/env python3
"""
gen_python_api.py — generate python/sdr.py from `sdr_system --help`.

The C++ CLI (Boost program_options) is the single source of truth: this parses
its --help output and emits a Python wrapper exposing EVERY option as a keyword
argument, so the two never drift. Wired into the CMake build (POST_BUILD), so it
regenerates automatically whenever sdr_system is rebuilt.

Usage: python3 tools/gen_python_api.py --binary <path> --out <path>
Always exits 0 (a generation hiccup must never fail the C++ build).
"""
import argparse, os, re, subprocess, sys, textwrap


def parse_help(text):
    """Return list of (name, has_arg, default_or_None, help) in declaration order."""
    opts, cur = [], None
    for raw in text.splitlines():
        line = raw.rstrip()
        m = re.match(r'^  (--\S.*?)(?:\s{2,}(.*\S))?\s*$', line)
        if m:
            if cur:
                opts.append(cur)
            tok, desc = m.group(1), (m.group(2) or "")
            om = re.match(r'^--([A-Za-z0-9_-]+)(?:\s+arg)?(?:\s+\(=(.*)\))?$', tok)
            if not om:                      # unparseable token — skip
                cur = None
                continue
            name = om.group(1)
            has_arg = bool(re.search(r'\barg\b', tok))
            default = om.group(2)
            cur = [name, has_arg, default, desc]
        elif cur is not None and re.match(r'^\s{10,}\S', line):
            cur[3] = (cur[3] + " " + line.strip()).strip()
        elif not line.strip():
            if cur:
                opts.append(cur); cur = None
    if cur:
        opts.append(cur)
    # de-dup (Boost sometimes repeats), drop --help
    seen, out = set(), []
    for name, ha, d, h in opts:
        if name == "help" or name in seen:
            continue
        seen.add(name)
        out.append((name, ha, re.sub(r'\s+', ' ', h).strip(), d))
    return out


def py_ident(name):
    return name.replace("-", "_")


def emit(opts, binary_hint):
    # Python-identifier -> cpp name, keeping the first when hyphen/underscore alias collide
    py2cpp, kw_order = {}, []
    for name, ha, h, d in opts:
        pid = py_ident(name)
        if pid not in py2cpp:
            py2cpp[pid] = name
            kw_order.append((pid, name, d, h))

    def q(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')

    L = []
    L.append('"""')
    L.append("sdr.py — Python wrapper for the USRP B210 SDR PHY (sdr_system).")
    L.append("")
    L.append("AUTO-GENERATED from `sdr_system --help` by tools/gen_python_api.py.")
    L.append("DO NOT EDIT — rerun the generator (or rebuild the C++) to refresh.")
    L.append("")
    L.append("Quick start:")
    L.append("    from sdr import SDR, tx, rx, sink_arq, source_arq, run_pair")
    L.append('    tx(scheme="QPSK", tx_gain=78, fec=True).run()          # one process')
    L.append('    run_pair(sink_arq(scheme="QPSK", fec=True),            # BOTH ends,')
    L.append('             source_arq(scheme="QPSK", fec=True))          #   RX then TX')
    L.append('"""')
    L.append("import os, shlex, subprocess, time")
    L.append("")
    L.append("_HERE = os.path.dirname(os.path.abspath(__file__))")
    L.append('DEFAULT_BINARY = os.environ.get("SDR_SYSTEM_BIN") or \\')
    L.append('    os.path.normpath(os.path.join(_HERE, "..", "build", "sdr_system"))')
    L.append("")
    L.append("# cpp-option-name -> (has_arg, default, help)")
    L.append("OPTIONS = {")
    for name, ha, h, d in opts:
        L.append(f'    "{name}": ({ha}, {("None" if d is None else repr(d))}, "{q(h)}"),')
    L.append("}")
    L.append("")
    L.append("# python-identifier -> cpp-option-name")
    L.append("PY2CPP = {")
    for pid, name, d, h in kw_order:
        L.append(f'    "{pid}": "{name}",')
    L.append("}")
    L.append("")
    L.append("_UNSET = object()")
    L.append("")
    L.append("")
    L.append("def _resolve(key):")
    L.append("    if key in OPTIONS: return key")
    L.append("    if key in PY2CPP:  return PY2CPP[key]")
    L.append('    alt = key.replace("_", "-")')
    L.append("    if alt in OPTIONS: return alt")
    L.append('    raise KeyError("unknown sdr_system option: %r" % key)')
    L.append("")
    L.append("")
    L.append("class SDR:")
    L.append('    """One sdr_system invocation. Set any --option as a keyword (hyphens or')
    L.append('    underscores both work); call .run() (blocking) or .popen() (background)."""')
    # explicit-kwarg constructor (great for autocomplete + discoverability)
    sig = ["    def __init__(self,"]
    for pid, name, d, h in kw_order:
        short = textwrap.shorten(h, width=62, placeholder="...")
        dd = "flag" if _flag(name, opts) else ("=" + str(d) if d is not None else "")
        sig.append(f"                 {pid}=_UNSET,{' ':1}# {dd + ('  ' if dd else '')}{short}")
    sig.append("                 binary=None, extra=None):")
    L += sig
    L.append("        self.binary = binary or DEFAULT_BINARY")
    L.append("        self.opts = {}")
    L.append("        self.extra = list(extra or [])")
    L.append("        _kw = dict(")
    for i, (pid, name, d, h) in enumerate(kw_order):
        comma = "," if i < len(kw_order) - 1 else ""
        L.append(f"            {pid}={pid}{comma}")
    L.append("        )")
    L.append("        for _k, _v in _kw.items():")
    L.append("            if _v is not _UNSET:")
    L.append("                self.set(**{_k: _v})")
    L.append("")
    L.append("    def set(self, **opts):")
    L.append("        for k, v in opts.items():")
    L.append("            self.opts[_resolve(k)] = v")
    L.append("        return self")
    L.append("")
    L.append("    def argv(self):")
    L.append("        cmd = [self.binary]")
    L.append("        for k, v in self.opts.items():")
    L.append("            has_arg = OPTIONS[k][0]")
    L.append("            if not has_arg:                         # bool_switch flag")
    L.append('                if v: cmd.append("--" + k)')
    L.append("            else:")
    L.append('                s = ("true" if v else "false") if isinstance(v, bool) else str(v)')
    L.append('                cmd += ["--" + k, s]')
    L.append("        return cmd + self.extra")
    L.append("")
    L.append("    def command(self):")
    L.append('        return " ".join(shlex.quote(a) for a in self.argv())')
    L.append("")
    L.append("    def run(self, **overrides):")
    L.append("        self.set(**overrides)")
    L.append("        return subprocess.run(self.argv())")
    L.append("")
    L.append("    def popen(self, **overrides):")
    L.append("        self.set(**overrides)")
    L.append("        return subprocess.Popen(self.argv())")
    L.append("")
    L.append("    def __repr__(self):")
    L.append('        return "SDR(%s)" % self.command()')
    L.append("")
    L.append("")
    L.append("# ── convenience constructors (pick a role) ───────────────────────────")
    L.append('def tx(**o):         return SDR(role="tx", **o)          # transmit only')
    L.append('def rx(**o):         return SDR(role="rx", **o)          # receive only')
    L.append('def sink_arq(**o):   return SDR(role="sink_arq", **o)    # ARQ receiver (2 boxes)')
    L.append('def source_arq(**o): return SDR(role="source_arq", **o) # ARQ sender   (2 boxes)')
    L.append('def both(**o):       return SDR(role="both", **o)        # 1 box: TX + RX at once')
    L.append('loopback = both                                          # alias')
    L.append("")
    L.append("")
    L.append("def run_pair(rx_side, tx_side, rx_head_start=4.0, rx_grace=20.0):")
    L.append('    """Drive BOTH ends: start the receiver, give it a head start, start the')
    L.append("    transmitter, wait for TX to finish, then wait (up to rx_grace s) for RX")
    L.append('    to self-terminate — else stop it. Returns (rx_returncode, tx_returncode)."""')
    L.append("    rxp = rx_side.popen()")
    L.append("    try:")
    L.append("        time.sleep(rx_head_start)")
    L.append("        txp = tx_side.popen()")
    L.append("        txp.wait()")
    L.append("        try:")
    L.append("            rxp.wait(timeout=rx_grace)")
    L.append("        except subprocess.TimeoutExpired:")
    L.append("            rxp.terminate()")
    L.append("            try: rxp.wait(timeout=5)")
    L.append("            except subprocess.TimeoutExpired: rxp.kill()")
    L.append("        return rxp.returncode, txp.returncode")
    L.append("    finally:")
    L.append("        if rxp.poll() is None:")
    L.append("            rxp.terminate()")
    L.append("")
    L.append("")
    L.append("def options():")
    L.append('    """Print every exposed option with its default and help."""')
    L.append("    for name, (ha, d, h) in OPTIONS.items():")
    L.append('        tag = "(flag)" if not ha else "= " + str(d)')
    L.append('        print("  --%-22s %-14s %s" % (name, tag, h))')
    L.append("")
    L.append("")
    L.append('if __name__ == "__main__":')
    L.append('    print("sdr_system binary:", DEFAULT_BINARY)')
    L.append('    print("%d options:\\n" % len(OPTIONS)); options()')
    L.append("")
    return "\n".join(L)


def _flag(name, opts):
    for n, ha, h, d in opts:
        if n == name:
            return not ha
    return False


def _group(name):
    if name.startswith("ofdm") or name in ("scheme", "fec", "waveform"):
        return "Modulation & waveform"
    if name.startswith("ack") or name in ("timeout", "timer_interval"):
        return "ARQ / ACK"
    if name in ("role", "mode", "tx-reps", "tx-mode", "rx-idle-timeout", "interval",
                "num_bits", "message-type", "message", "tone-freq", "tone-amp"):
        return "Mode, message & transmission"
    if name in ("preamble", "m", "add_preamble", "filter_type", "roll_off", "num_taps",
                "U", "D", "symbol_rate", "num_threads", "sps"):
        return "Preamble & pulse shaping"
    if name.startswith("det") or name.startswith("energy") or name.startswith("IIR") or name == "alpha":
        return "Energy detection"
    if name.startswith("sync") or name in ("sps_sync", "recv_msg_len",
                                           "samps_per_buff", "num_recv_request"):
        return "Synchronization"
    if name.startswith("timing") or name.startswith("phase"):
        return "Timing & phase recovery"
    if name.startswith("eq"):
        return "Equalizer"
    if name.startswith("tx-") or name.startswith("tx_"):
        return "TX radio"
    if name.startswith("rx-") or name.startswith("rx_"):
        return "RX radio"
    if name.startswith("viz"):
        return "Visualization"
    return "RF, clock & misc"


def emit_md(opts):
    order = ["Mode, message & transmission", "Modulation & waveform",
             "Preamble & pulse shaping", "TX radio", "RX radio", "ARQ / ACK",
             "Energy detection", "Synchronization", "Timing & phase recovery",
             "Equalizer", "Visualization", "RF, clock & misc"]
    buckets = {}
    for name, ha, h, d in opts:
        buckets.setdefault(_group(name), []).append((name, ha, h, d))
    L = []
    L.append("# All controllable options")
    L.append("")
    L.append("Every option below can be set three equivalent ways — all reach the same")
    L.append("`sdr_system` binary. Option names accept **hyphens or underscores**.")
    L.append("")
    L.append("| Where | How | Example |")
    L.append("|---|---|---|")
    L.append('| JSON config (`run.py`) | `"name": value` | `"scheme": "16-QAM"` |')
    L.append('| Command line (overrides JSON) | `--name value` | `--scheme 16-QAM` |')
    L.append('| Python (`sdr.py`) | `SDR(name=value)` | `SDR(scheme="16-QAM")` |')
    L.append("")
    L.append("In JSON: booleans are `true`/`false`, numbers are bare (`915e6`), strings are")
    L.append('quoted. **Flags** (Type = _flag_) take no value on the command line (just')
    L.append("`--name`); in JSON/Python set them to `true`.")
    L.append("")
    L.append("> Auto-generated from `sdr_system --help` — always lists every current "
             "option (**%d** total). Do not edit by hand." % len(opts))
    L.append("")
    for g in order + [k for k in buckets if k not in order]:
        rows = buckets.get(g)
        if not rows:
            continue
        L.append("## " + g)
        L.append("")
        L.append("| Option | Type | Default | Description |")
        L.append("|---|---|---|---|")
        for name, ha, h, d in rows:
            typ = "value" if ha else "flag"
            dflt = "—" if not ha else ("_(none)_" if d is None else "`%s`" % d)
            desc = h.replace("|", "\\|")
            L.append("| `--%s` | %s | %s | %s |" % (name, typ, dflt, desc))
        L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--md", help="also write a Markdown options reference here")
    a = ap.parse_args()
    try:
        help_txt = subprocess.run([a.binary, "--help"], capture_output=True,
                                  text=True, timeout=30).stdout
        opts = parse_help(help_txt)
        if len(opts) < 5:
            raise RuntimeError("parsed too few options (%d) — help format changed?" % len(opts))
        code = emit(opts, a.binary)
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w") as f:
            f.write(code)
        print("[gen_python_api] wrote %s (%d options)" % (a.out, len(opts)))
        if a.md:
            os.makedirs(os.path.dirname(os.path.abspath(a.md)), exist_ok=True)
            with open(a.md, "w") as f:
                f.write(emit_md(opts) + "\n")
            print("[gen_python_api] wrote %s" % a.md)
    except Exception as e:                              # never break the build
        sys.stderr.write("[gen_python_api] WARNING: skipped (%s)\n" % e)
    sys.exit(0)


if __name__ == "__main__":
    main()
