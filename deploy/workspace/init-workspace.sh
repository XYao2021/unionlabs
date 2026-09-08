#!/usr/bin/env bash
# init-workspace.sh — create (and keep current) the shared experiment layout under
# /workspace.
#
#   bash init-workspace.sh            # seed / refresh /workspace/experiments
#   FORCE=1 bash init-workspace.sh    # refresh now even if the image version is unchanged
#   ROOT=/some/other/path bash init-workspace.sh
#
# /workspace is a PERSISTENT mount: it outlives the image and every session, and it
# WINS over the repo checkout at run time (run_algo and topology.py both search it
# first). That is deliberate — an experiment you edit in the workspace persists — but
# it also means a `git pull` or a newer image does NOT, by itself, update the shipped
# examples that already sit in /workspace. That is how a stale DQPSK topology outlived
# a QPSK image.
#
# So the shipped examples (topologies/ and the repo's algorithms/) now track the image:
# each is stamped with the image commit, and when the image is newer they are REFRESHED
# — but never at the cost of an edit:
#   * unchanged since we seeded it   -> updated silently to the image's version
#   * you edited it in the workspace -> LEFT as yours; the image's version is dropped
#                                       beside it as <name>.repo, with a notice
#   * a pre-stamp seed we can't judge -> refreshed, previous saved as <name>.local
# Your own new-named files, settings/, searching/ profiles and reservation.json are
# never touched.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${ROOT:-/workspace/experiments}"
FORCE="${FORCE:-0}"

[ -d "$(dirname "$ROOT")" ] || { echo "no $(dirname "$ROOT") — is this a session with the workspace mounted?" >&2; exit 1; }

# The image this workspace should track. BUILD_INFO.txt is written into the repo
# folder at image build; fall back to git for a plain checkout.
IMG_COMMIT=""
if [ -f "$HERE/../../BUILD_INFO.txt" ]; then
  IMG_COMMIT="$(sed -n 's/^commit //p' "$HERE/../../BUILD_INFO.txt" | head -1 | cut -c1-12)"
fi
if [ -z "$IMG_COMMIT" ]; then
  IMG_COMMIT="$(git -C "$HERE/../.." rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
fi

STAMP="$ROOT/.seed-commit"
MANIFEST="$ROOT/.seed-manifest"          # "<sha> <relpath>" — what we last seeded
PREV="$(cat "$STAMP" 2>/dev/null || echo none)"
RESEED=0
if [ "$FORCE" = 1 ] || [ "$PREV" != "$IMG_COMMIT" ]; then RESEED=1; fi

_sha()   { { command -v sha256sum >/dev/null 2>&1 && sha256sum "$1" || shasum -a 256 "$1"; } | cut -d' ' -f1; }
_dirsha(){ ( cd "$1" && find . -type f ! -path '*/__pycache__/*' ! -name '*.pyc' | LC_ALL=C sort \
             | while IFS= read -r f; do printf '%s\n' "$f"; cat "$f"; done \
             | { command -v sha256sum >/dev/null 2>&1 && sha256sum || shasum -a 256; } ) | cut -d' ' -f1; }
manifest_get() { [ -e "$MANIFEST" ] && awk -v r="$1" '$2==r{print $1}' "$MANIFEST" | tail -1 || true; }
manifest_set() { # rel sha
  local rel="$1" sha="$2" tmp="$MANIFEST.tmp.$$"
  { [ -e "$MANIFEST" ] && awk -v r="$rel" '$2!=r' "$MANIFEST"; echo "$sha $rel"; } > "$tmp"
  mv "$tmp" "$MANIFEST"
}

created=0 kept=0 refreshed=0 edited=0

# reseed KIND SRC DST REL  — KIND is 'file' or 'dir'
reseed() {
  local kind="$1" src="$2" dst="$3" rel="$4" new cur old
  if [ "$kind" = file ]; then new="$(_sha "$src")"; else new="$(_dirsha "$src")"; fi

  if [ ! -e "$dst" ]; then                              # first time: seed it
    _install "$kind" "$src" "$dst"; manifest_set "$rel" "$new"; created=$((created + 1)); return
  fi
  if [ "$RESEED" != 1 ]; then kept=$((kept + 1)); return; fi

  if [ "$kind" = file ]; then cur="$(_sha "$dst")"; else cur="$(_dirsha "$dst")"; fi
  if [ "$cur" = "$new" ]; then manifest_set "$rel" "$new"; kept=$((kept + 1)); return; fi  # already current

  old="$(manifest_get "$rel")"
  if [ -n "$old" ] && [ "$cur" != "$old" ]; then       # you edited it -> protect, drop .repo
    rm -rf "$dst.repo"; _install "$kind" "$src" "$dst.repo"
    echo "  [keep]   $rel edited in workspace — image's version at $(basename "$dst").repo"
    edited=$((edited + 1)); return
  fi
  if [ -n "$old" ]; then                                # unchanged since our seed -> update
    _install "$kind" "$src" "$dst"; manifest_set "$rel" "$new"
    echo "  [update] $rel refreshed from image"; refreshed=$((refreshed + 1)); return
  fi
  # pre-stamp seed, no manifest -> can't judge; refresh but keep the old as .local
  rm -rf "$dst.local"; cp -R "$dst" "$dst.local" 2>/dev/null || cp "$dst" "$dst.local"
  _install "$kind" "$src" "$dst"; manifest_set "$rel" "$new"
  echo "  [update] $rel refreshed from image (previous seed saved as $(basename "$dst").local)"
  refreshed=$((refreshed + 1))
}
_install() { # kind src dst
  if [ "$1" = file ]; then cp "$2" "$3"
  else rm -rf "$3"; cp -R "$2" "$3"; rm -rf "$3/__pycache__"; fi
}

# ── the per-folder READMEs (seed once; they are just labels) ──
for d in settings searching topologies cross_channel algorithms env; do
  mkdir -p "$ROOT/$d"
  if [ -e "$ROOT/$d/README.md" ]; then kept=$((kept + 1))
  elif [ -e "$HERE/$d/README.md" ]; then cp "$HERE/$d/README.md" "$ROOT/$d/README.md"; created=$((created + 1)); fi
done

# ── the env manifest (the user's file, seed once); sync-env is a tool, always current ──
if [ ! -e "$ROOT/env/requirements.txt" ] && [ -e "$HERE/env/requirements.txt" ]; then
  cp "$HERE/env/requirements.txt" "$ROOT/env/requirements.txt"; created=$((created + 1))
fi
cp "$HERE/env/sync-env.sh" "$ROOT/env/sync-env.sh" 2>/dev/null || true

# ── the reservation TEMPLATE tracks the image; the filled reservation.json never does ──
if [ -e "$HERE/settings/reservation.template.json" ]; then
  reseed file "$HERE/settings/reservation.template.json" \
              "$ROOT/settings/reservation.template.json" "settings/reservation.template.json"
fi

# ── the repo's algorithms: track the image, protect edits ──
if [ -d "$HERE/algorithms" ]; then
  for a in "$HERE"/algorithms/*/; do
    n="$(basename "$a")"
    reseed dir "${a%/}" "$ROOT/algorithms/$n" "algorithms/$n"
  done
fi

# ── the example wirings: track the image, protect edits ──
for f in "$HERE"/topologies/*.json; do
  [ -e "$f" ] || continue
  reseed file "$f" "$ROOT/topologies/$(basename "$f")" "topologies/$(basename "$f")"
done

[ -e "$ROOT/README.md" ] || cp "$HERE/README.md" "$ROOT/README.md"
echo "$IMG_COMMIT" > "$STAMP"

echo "workspace layout at $ROOT   (tracking image $IMG_COMMIT)"
find "$ROOT" -maxdepth 1 -mindepth 1 -type d | sort | sed 's|^|  |'
echo "  ($created written, $refreshed refreshed, $edited kept-as-edited, $kept unchanged)"
if [ "$RESEED" != 1 ]; then
  echo "  (image unchanged since last seed — run with FORCE=1, or ./run.sh refresh-workspace, to re-seed)"
fi
