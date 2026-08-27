#!/usr/bin/env bash
# make-pdf.sh — render a doc to PDF on demand.
#
#   ./docs/make-pdf.sh BEGINNER_GUIDE        # -> BEGINNER_GUIDE.pdf here
#   ./docs/make-pdf.sh BEGINNER_GUIDE ~/Desktop
#   ./docs/make-pdf.sh all
#
# PDFs are NOT kept in the repo. They were, and they went stale: every one of
# them was rendered from markdown that had since been edited, so the PDF a
# newcomer opened contradicted the guide the repo actually shipped. A generated
# file that can disagree with its source is worse than no file. Generate one
# when you need to hand it to someone, and let it expire.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
command -v pandoc >/dev/null || {
  echo "pandoc not installed — brew install pandoc (and a LaTeX engine)"; exit 1; }

render() {
  local name="$1" out="$2" src=""
  for c in "$HERE/$name.md" "$HERE/../$name.md"; do [ -f "$c" ] && { src="$c"; break; }; done
  [ -n "$src" ] || { echo "no such doc: $name"; return 1; }
  local hdr=()
  [ -f "$HERE/.pandoc-header.tex" ] && hdr=(-H "$HERE/.pandoc-header.tex")
  pandoc "$src" "${hdr[@]}" -o "$out/$name.pdf" \
      --pdf-engine=xelatex -V geometry:margin=1in -V fontsize=10pt
  echo "wrote $out/$name.pdf"
}

OUT="${2:-$PWD}"
if [ "${1:-}" = "all" ]; then
  for f in "$HERE"/*.md; do render "$(basename "$f" .md)" "$OUT"; done
else
  render "${1:?usage: make-pdf.sh <DOC_NAME|all> [outdir]}" "$OUT"
fi
