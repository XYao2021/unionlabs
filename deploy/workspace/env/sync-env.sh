#!/usr/bin/env bash
# sync-env.sh — make the account's persistent environment match env/requirements.txt.
#
#   bash sync-env.sh              # normally run by the session hook
#   ROOT=/elsewhere bash sync-env.sh
#
# Idempotent and cheap: when the requirements have not changed since the last
# apply, this is a hash comparison and nothing more. When they have, pip
# installs only what is missing. The venv sees the image's own site-packages
# (numpy, the UHD bindings) through --system-site-packages, so requirements
# only need to list what the image lacks.
set -euo pipefail
ROOT="${ROOT:-/workspace/experiments}"
ENVDIR="$ROOT/env"
REQ="$ENVDIR/requirements.txt"
VENV="$ENVDIR/venv"
APPLIED="$ENVDIR/.applied"

[ -d "$ROOT" ] || { echo "[sync-env] no $ROOT — is the workspace mounted?" >&2; exit 1; }
mkdir -p "$ENVDIR"
[ -f "$REQ" ] || { echo "# add one library per line" > "$REQ"; }

if [ ! -x "$VENV/bin/pip" ]; then
  echo "[sync-env] creating the account environment at $VENV"
  rm -rf "$VENV"                      # a half-made venv (no pip) is worse than none
  python3 -m venv --system-site-packages "$VENV" || true
  if [ ! -x "$VENV/bin/pip" ]; then
    # Ubuntu strips ensurepip from the base python; venv then comes up with no
    # pip inside and every install fails two steps later with a confusing path
    # error. Say the real cause.
    echo "[sync-env] FATAL: venv has no pip — the image lacks python3-venv" >&2
    echo "[sync-env]        (apt-get install python3-venv, then re-run)" >&2
    exit 1
  fi
fi

# strip comments/blanks so a comment edit does not trigger an install. grep
# exits 1 when nothing matches (an all-comments file), which under pipefail
# would poison the pipeline — hence the || true inside the group.
if command -v sha256sum >/dev/null 2>&1; then HASHER="sha256sum"; else HASHER="shasum -a 256"; fi
WANT_HASH=$({ grep -vE '^[[:space:]]*(#|$)' "$REQ" || true; } | sort | $HASHER | cut -d' ' -f1)
HAVE_HASH=$(cat "$APPLIED" 2>/dev/null || true)

if [ "$WANT_HASH" = "$HAVE_HASH" ]; then
  echo "[sync-env] up to date"
  exit 0
fi

if grep -qvE '^\s*(#|$)' "$REQ"; then
  echo "[sync-env] requirements changed — installing the delta"
  "$VENV/bin/pip" install --no-input -r "$REQ"
else
  echo "[sync-env] requirements list is empty — nothing to install"
fi
echo "$WANT_HASH" > "$APPLIED"
# the reproducibility snapshot: what the environment actually holds, dated
"$VENV/bin/pip" freeze --local > "$ENVDIR/.frozen-$(date -u +%Y%m%d)" 2>/dev/null || true
echo "[sync-env] applied"
