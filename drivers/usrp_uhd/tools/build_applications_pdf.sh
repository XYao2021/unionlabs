#!/usr/bin/env bash
# Render the applications/ docs from Markdown -> PDF (+ .tex) with pandoc/xelatex:
#   applications/APPLICATIONS_INTRO.md  -> the applications introduction
#   applications/EXPERIMENT_GUIDE.md    -> the step-by-step operating runbook
# Needs: pandoc, xelatex (MacTeX), fonts "Charter" + "Menlo" (stock on macOS).
# Script lives in phy/tools/; runs from the repo root. Uses phy/tools/md_glyph_fix.py
# + docs/.pandoc-header.tex.
#
#   phy/tools/build_applications_pdf.sh          # build both
#   phy/tools/build_applications_pdf.sh intro    # just APPLICATIONS_INTRO
#   phy/tools/build_applications_pdf.sh guide     # just EXPERIMENT_GUIDE
set -euo pipefail
cd "$(dirname "$0")/../.."                 # repo root (script is phy/tools/)
export PATH="/Library/TeX/texbin:$PATH"

build() {   # <src.md> <title> <margin> <mono-scale> <fontsize>
  local src="$1" title="$2" margin="$3" mono="$4" fs="$5"
  local base="${src%.md}"
  local tmp; tmp="$(mktemp -t appdoc_XXXX).md"
  python3 phy/tools/md_glyph_fix.py "$src" "$tmp"
  local OPTS=(--from gfm+tex_math_dollars+implicit_figures --toc --toc-depth=2
              --resource-path=.:docs:results -V geometry:margin="$margin"
              -V mainfont="Charter" -V monofont="Menlo" -V monofontoptions="Scale=$mono"
              -V colorlinks=true -V linkcolor=RoyalBlue -V urlcolor=RoyalBlue
              -V fontsize="$fs" --highlight-style=tango
              --include-in-header=docs/.pandoc-header.tex -V title="$title")
  pandoc "$tmp" --pdf-engine=xelatex "${OPTS[@]}" -o "${base}.pdf"
  pandoc "$tmp" -s "${OPTS[@]}" -o "${base}.tex"
  rm -f "$tmp"
  echo "wrote ${base}.pdf and ${base}.tex"
}

what="${1:-all}"
case "$what" in
  all|intro|guide) ;;
  *) echo "usage: $(basename "$0") [all|intro|guide]" >&2; exit 2 ;;
esac

if [ "$what" = all ] || [ "$what" = intro ]; then
  build applications/APPLICATIONS_INTRO.md "SDR Platform — Applications Introduction" 0.9in  0.80 10pt
fi
if [ "$what" = all ] || [ "$what" = guide ]; then
  build applications/EXPERIMENT_GUIDE.md   "SDR Platform — Experiment Operating Guide"  0.85in 0.72 9.5pt
fi
