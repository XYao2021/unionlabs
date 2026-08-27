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

  # The docs use maths glyphs (superscripts, ×, ≈, box drawing) that the PDF
  # fonts do not all carry, and xelatex renders a missing glyph as nothing at
  # all -- silently dropping an exponent out of a formula. md_glyph_fix rewrites
  # them to safe equivalents first. Preprocess into a temp file so the source
  # doc is never modified.
  local fix="$HERE/../drivers/usrp/tools/md_glyph_fix.py" tmp=""
  if [ -f "$fix" ]; then
    tmp="$(mktemp -t unionlabs-doc).md"
    python3 "$fix" "$src" "$tmp" && src="$tmp"
  fi

  pandoc "$src" "${hdr[@]}" -o "$out/$name.pdf" \
      --pdf-engine=xelatex -V geometry:margin=1in -V fontsize=10pt
  [ -n "$tmp" ] && rm -f "$tmp"
  echo "wrote $out/$name.pdf"
}

OUT="${2:-$PWD}"
if [ "${1:-}" = "all" ]; then
  for f in "$HERE"/*.md; do render "$(basename "$f" .md)" "$OUT"; done
else
  render "${1:?usage: make-pdf.sh <DOC_NAME|all> [outdir]}" "$OUT"
fi
