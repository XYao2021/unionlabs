#!/usr/bin/env bash
# init-workspace.sh — create the shared experiment layout under /workspace.
#
#   bash init-workspace.sh            # /workspace/experiments
#   ROOT=/some/other/path bash init-workspace.sh
#
# Idempotent and non-destructive: existing files are never overwritten, so running it in
# a fresh session is always safe. Run it from inside a session (the platform mounts
# /workspace there); anything the IMAGE writes to /workspace is hidden by that mount.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${ROOT:-/workspace/experiments}"

[ -d "$(dirname "$ROOT")" ] || { echo "no $(dirname "$ROOT") — is this a session with the workspace mounted?" >&2; exit 1; }

created=0 kept=0
for d in settings searching topologies cross_channel algorithms env; do
  mkdir -p "$ROOT/$d"
  if [ -e "$ROOT/$d/README.md" ]; then
    kept=$((kept + 1))
  elif [ -e "$HERE/$d/README.md" ]; then
    cp "$HERE/$d/README.md" "$ROOT/$d/README.md"
    created=$((created + 1))
  fi
done

# the env manifest — seeded once, never overwritten (it is the user's file)
if [ ! -e "$ROOT/env/requirements.txt" ] && [ -e "$HERE/env/requirements.txt" ]; then
  cp "$HERE/env/requirements.txt" "$ROOT/env/requirements.txt"
  created=$((created + 1))
fi
cp "$HERE/env/sync-env.sh" "$ROOT/env/sync-env.sh" 2>/dev/null || true   # tool, always current

# the repo's algorithms — seeded once per folder, never overwritten, so an
# algorithm the account has edited in the shared workspace stays as edited
ALGOSRC="$HERE/algorithms"
if [ -d "$ALGOSRC" ]; then
  for a in "$ALGOSRC"/*/; do
    n="$(basename "$a")"
    if [ -e "$ROOT/algorithms/$n" ]; then
      kept=$((kept + 1))
    else
      cp -R "$a" "$ROOT/algorithms/$n"
      rm -rf "$ROOT/algorithms/$n/__pycache__"
      created=$((created + 1))
    fi
  done
fi
[ -e "$ROOT/README.md" ] || cp "$HERE/README.md" "$ROOT/README.md"

# the example wirings — these are meant to be copied and edited, so an existing file of
# the same name is somebody's experiment and is never overwritten
for f in "$HERE"/topologies/*.json; do
  [ -e "$f" ] || continue
  dst="$ROOT/topologies/$(basename "$f")"
  if [ -e "$dst" ]; then
    kept=$((kept + 1))
  else
    cp "$f" "$dst"
    created=$((created + 1))
  fi
done

echo "workspace layout at $ROOT"
find "$ROOT" -maxdepth 1 -mindepth 1 -type d | sort | sed 's|^|  |'
echo "  ($created file(s) written, $kept already present — nothing overwritten)"
echo
echo "example wirings now in $ROOT/topologies:"
for f in "$ROOT"/topologies/*.json; do [ -e "$f" ] && echo "  $(basename "$f" .json)"; done
echo "run one with:  ./run.sh --algo fl --topology fl-star-tcp --node srv"
