#!/usr/bin/env bash
# Render the repo's guides from Markdown -> PDF with pandoc/xelatex. Every PDF lands in
# docs/, which is where all documentation lives, even for the two Markdown files that sit
# at the repo root:
#
#   README.md                -> docs/README.pdf
#   HOW_TO_ADD_ALGORITHM.md  -> docs/HOW_TO_ADD_ALGORITHM.pdf
#   docs/BEGINNER_GUIDE.md   -> docs/BEGINNER_GUIDE.pdf   (install, run, add an experiment)
#   docs/MANIFEST.md         -> docs/MANIFEST.pdf         (file index + CLI <-> sdr.py map)
#
# Companion to build_reference_pdf.sh (SYSTEM_REFERENCE) and build_applications_pdf.sh.
# Needs: pandoc, xelatex (MacTeX), fonts "Charter" + "Menlo" (stock on macOS).
# Script lives in drivers/usrp/tools/; runs from the repo root.
#
#   drivers/usrp/tools/build_guide_pdf.sh            # build all four
#   drivers/usrp/tools/build_guide_pdf.sh readme     # just README
#   drivers/usrp/tools/build_guide_pdf.sh howto      # just HOW_TO_ADD_ALGORITHM
#   drivers/usrp/tools/build_guide_pdf.sh beginner   # just BEGINNER_GUIDE
#   drivers/usrp/tools/build_guide_pdf.sh manifest   # just MANIFEST
set -euo pipefail
cd "$(dirname "$0")/../../.."              # repo root (drivers/usrp/tools/ -> .)
export PATH="/Library/TeX/texbin:$PATH"

command -v pandoc  >/dev/null || { echo "pandoc not found — deploy/initialization.sh --docs" >&2; exit 1; }
command -v xelatex >/dev/null || { echo "xelatex not found — deploy/initialization.sh --docs" >&2; exit 1; }

build() {   # <src.md> <title> <toc-depth>
  # NOTE: -V title=... is substituted into the LaTeX template verbatim, so a raw '&'
  # in the title becomes an alignment tab and the build dies with "Misplaced alignment
  # tab character &". Escape the LaTeX specials rather than banning them from titles.
  local src="$1" title="${2//&/\\&}" depth="$3"
  local out="docs/$(basename "${src%.md}").pdf"      # every PDF lives in docs/
  local tmp; tmp="$(mktemp -t guide_XXXX).md"
  python3 drivers/usrp/tools/md_glyph_fix.py "$src" "$tmp"
  pandoc "$tmp" --pdf-engine=xelatex \
    --from gfm+tex_math_dollars+implicit_figures --toc --toc-depth="$depth" \
    --resource-path=.:docs:results -V geometry:margin=0.9in \
    -V mainfont="Charter" -V monofont="Menlo" -V monofontoptions="Scale=0.78" \
    -V colorlinks=true -V linkcolor=RoyalBlue -V urlcolor=RoyalBlue \
    -V fontsize=10pt --highlight-style=tango \
    --include-in-header=docs/.pandoc-header.tex \
    -V title="$title" -o "$out"
  rm -f "$tmp"
  echo "wrote $out"
}

what="${1:-all}"
case "$what" in
  all|readme|howto|beginner|manifest) ;;
  *) echo "usage: $(basename "$0") [all|readme|howto|beginner|manifest]" >&2; exit 2 ;;
esac

if [ "$what" = all ] || [ "$what" = readme ]; then
  build README.md "UnionLabs SDR Platform" 2
fi
if [ "$what" = all ] || [ "$what" = howto ]; then
  build HOW_TO_ADD_ALGORITHM.md "UnionLabs SDR Platform — How to Add Your Algorithm" 3
fi
if [ "$what" = all ] || [ "$what" = beginner ]; then
  build docs/BEGINNER_GUIDE.md "UnionLabs SDR Platform — Beginner's Guide" 3
fi
if [ "$what" = all ] || [ "$what" = manifest ]; then
  build docs/MANIFEST.md "UnionLabs SDR Platform — File Index & Command Mapping" 2
fi
