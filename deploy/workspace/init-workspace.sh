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
for d in settings topologies cross_channel; do
  mkdir -p "$ROOT/$d"
  if [ -e "$ROOT/$d/README.md" ]; then
    kept=$((kept + 1))
  else
    cp "$HERE/$d/README.md" "$ROOT/$d/README.md"
    created=$((created + 1))
  fi
done
[ -e "$ROOT/README.md" ] || cp "$HERE/README.md" "$ROOT/README.md"

echo "workspace layout at $ROOT"
find "$ROOT" -maxdepth 1 -mindepth 1 -type d | sort | sed 's|^|  |'
echo "  ($created README(s) written, $kept already present — nothing overwritten)"
