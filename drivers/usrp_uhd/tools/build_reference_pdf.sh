#!/usr/bin/env bash
# Render SYSTEM_REFERENCE.md -> SYSTEM_REFERENCE.pdf (+ .tex) with pandoc/xelatex.
# Needs: pandoc, xelatex (MacTeX), fonts "Charter" + "Menlo" (stock on macOS).
# Script lives in phy/tools/; runs from the repo root. Figures live in results/figures/.
set -euo pipefail
cd "$(dirname "$0")/../.."                 # repo root (script is phy/tools/)
export PATH="/Library/TeX/texbin:$PATH"
TMP="$(mktemp -t ref_pre_XXXX).md"
python3 phy/tools/md_glyph_fix.py SYSTEM_REFERENCE.md "$TMP"
OPTS=(--from gfm+tex_math_dollars+implicit_figures --toc --toc-depth=3
      --resource-path=.:docs:results -V geometry:margin=0.9in
      -V mainfont="Charter" -V monofont="Menlo" -V monofontoptions="Scale=0.80"
      -V colorlinks=true -V linkcolor=RoyalBlue -V urlcolor=RoyalBlue
      -V fontsize=10pt --highlight-style=tango
      -V title="USRP B210 SDR Link — System Reference")
pandoc "$TMP" --pdf-engine=xelatex "${OPTS[@]}" -o SYSTEM_REFERENCE.pdf
pandoc "$TMP" -s "${OPTS[@]}" -o SYSTEM_REFERENCE.tex
rm -f "$TMP"
echo "wrote SYSTEM_REFERENCE.pdf and SYSTEM_REFERENCE.tex"
