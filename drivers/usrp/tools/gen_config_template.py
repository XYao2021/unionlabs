#!/usr/bin/env python3
"""
gen_config_template.py — generate a fully-defaulted config template (phy.cfg)
from `sdr_system --help`.

Every PHY option is written with its default value and a one-line description, so
you can change only what you need and pass the file with `sdr_system --config phy.cfg`
(command-line args still override the file). Options whose default is empty (device
args you must fill in, e.g. tx-args/rx-args) are written commented-out.

Wired into the CMake build (POST_BUILD) so it regenerates when sdr_system rebuilds.
Usage: python3 tools/gen_config_template.py --binary <path> --out phy.cfg
Always exits 0 (a hiccup must never fail the C++ build).
"""
import argparse, re, subprocess, sys

# reuse the exact --help parser from the python-api generator
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from gen_python_api import parse_help  # (name, has_arg, help, default) in decl order


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    try:
        help_txt = subprocess.run([a.binary, "--help"], capture_output=True,
                                  text=True, timeout=30).stdout
        opts = parse_help(help_txt)
    except Exception as e:
        print(f"[gen_config_template] skipped ({e})")
        return

    lines = [
        "# ==========================================================================",
        "#  phy.cfg — sdr_system PHY options, every one set to its DEFAULT value.",
        "#  Change only what you need, then run:  ./sdr_system --config phy.cfg",
        "#  Anything on the COMMAND LINE overrides this file, e.g.:",
        "#     ./sdr_system --config phy.cfg --scheme QPSK --rx-gain 22",
        "#  Lines starting with '#' are ignored. Use the long name without '--'.",
        "#  Auto-generated from `sdr_system --help` — edit values, not the layout.",
        "# ==========================================================================",
        "",
    ]
    for name, has_arg, help_txt1, default in opts:
        if name in ("help", "config"):
            continue
        desc = re.sub(r"\s+", " ", help_txt1).strip()
        # wrap the description as comment lines (~76 cols)
        words, line = desc.split(), "#"
        for w in words:
            if len(line) + 1 + len(w) > 76:
                lines.append(line); line = "#   " + w
            else:
                line += " " + w
        lines.append(line)
        d = default if default is not None else ""
        if d == "":
            # empty default (device args etc.) — comment out so you fill it in
            lines.append(f"# {name} =")
        else:
            lines.append(f"{name} = {d}")
        lines.append("")

    with open(a.out, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[gen_config_template] wrote {a.out} ({len(opts)} options)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[gen_config_template] error: {e}")
    sys.exit(0)
