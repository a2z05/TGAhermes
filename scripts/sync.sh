#!/usr/bin/env bash
# Single source of truth for the guest-mode plugin.
#
# The repo checkout is where edits are made; the deployed copy is what runs.
# Editing them separately is how they drifted before, which shipped a stale
# default to the live plugin while the repo looked correct.
#
# Usage:
#   ./sync.sh check    report drift + test results, change nothing
#   ./sync.sh deploy   copy repo -> deploy, run tests, hot-reload
#   ./sync.sh reload   hot-reload only
set -uo pipefail

# Paths are derived, never written literally: the pre-push audit blocks host
# paths in tracked files, and a literal host path here would both fail it and
# not survive a move. Override any of these from the environment.
REPO=${TGM_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
DEPLOY=${TGM_DEPLOY:-$(cd "$REPO/../.." && pwd)/plugins/$(basename "$REPO")}
# The plugin's tests import the gateway's own libs (PTB/telethon), so they need
# the interpreter that runs Hermes, not a bare python3. Look for one that can
# actually import them; TGM_PYTHON overrides the search entirely.
find_python() {
  local c d p
  for c in "${TGM_PYTHON:-}" "${HERMES_PYTHON:-}" \
           "$(command -v python3 2>/dev/null)"; do
    [ -n "$c" ] && [ -x "$c" ] || continue
    "$c" -c "import telegram" >/dev/null 2>&1 && { echo "$c"; return 0; }
  done
  # Walk up from the repo looking for the Hermes venv. A checkout keeps it at
  # <root>/hermes/.venv or <root>/.venv, and the repo itself usually sits at
  # <root>/projects/<name> — so climbing covers both, plus the container's
  # /hermes/.venv, without ever naming a host path (the pre-push audit blocks
  # those, and a literal one would not survive a move anyway).
  d=$REPO
  while :; do
    for p in "$d/hermes/.venv/bin/python" "$d/.venv/bin/python"; do
      [ -x "$p" ] && "$p" -c "import telegram" >/dev/null 2>&1 && { echo "$p"; return 0; }
    done
    [ "$d" = "/" ] && break
    d=$(dirname "$d")
  done
  # Invoked from inside a venv already? Reuse it rather than guessing.
  if [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
    "$VIRTUAL_ENV/bin/python" -c "import telegram" >/dev/null 2>&1 && { echo "$VIRTUAL_ENV/bin/python"; return 0; }
  fi
  echo "${TGM_PYTHON:-python3}"
}
# Default to the real control-socket reloader: a deploy that does not
# reload leaves the gateway serving the OLD code, silently.
RELOAD=${TGM_RELOAD:-/opt/data/scripts/tgm_reload.py}
PY=$(find_python)

# Only these files are copied. settings.json, state.json and anything learned
# at runtime are never touched: an update must not reset the owner's config.
FILES=(__init__.py bizauto.py userbridge.py selfupdate.py plugin.yaml README.md AGENT.md settings.example.json)
DIRS=(tests scripts assets)

# Never copy the identity file that holds the owner's real ids: it stays
# local-only and gitignored, and the deployed copy must not carry it.
EXCLUDE=(.audit-identities.json)

# Hot-reload through the control socket. Never SIGUSR1: a reload in the middle
# of a turn is how a live session gets corrupted.
reload() {
  if [ -z "$RELOAD" ] || [ ! -f "$RELOAD" ]; then
    echo "--- hot reload: skipped (no reloader at '$RELOAD') ---"
    return 0
  fi
  echo "--- hot reload ---"
  # Run ONCE: the script is not idempotent-cheap and a second reload would
  # re-wire handlers mid-flight for no reason.
  local out
  if ! out="$("$PY" "$RELOAD" 2>&1)"; then
    printf '%s\n' "$out" | tail -2
    echo "--- hot reload: FAILED (deployed, but the gateway still runs the old code) ---"
    return 0
  fi
  printf '%s\n' "$out" | grep -o "'reloaded': [A-Za-z]*" | head -1
  printf '%s\n' "$out" | grep -o "'adapters_rewired': [0-9]*" | head -1
}

drift() {
  local bad=0 f
  for f in "${FILES[@]}"; do
    [ -f "$REPO/$f" ] || continue
    if ! diff -q "$REPO/$f" "$DEPLOY/$f" >/dev/null 2>&1; then
      echo "  DRIFT: $f"; bad=1
    fi
  done
  for d in "${DIRS[@]}"; do
    [ -d "$REPO/$d" ] || continue
    while IFS= read -r f; do
      rel="${f#$REPO/}"
      skip=0
      for x in "${EXCLUDE[@]}"; do [ "$rel" = "$x" ] && skip=1; done
      [ "$skip" = 1 ] && continue
      if ! diff -q "$f" "$DEPLOY/$rel" >/dev/null 2>&1; then
        echo "  DRIFT: $rel"; bad=1
      fi
    done < <(find "$REPO/$d" -type f ! -path '*/__pycache__/*' ! -name '*.pyc')
  done
  return $bad
}

tests() {
  echo "--- tests ---"
  # Run against the repo copy: that is what is about to be copied, so a broken
  # edit is caught before it ever reaches the live plugin.
  ( cd "$REPO" && "$PY" "$REPO/tests/test_plugin.py" 2>&1 | tail -1 )
  ( cd "$REPO" && TGM_PLUGIN_DIR="$REPO" "$PY" scripts/test-audit.py 2>&1 | tail -1 )
  ( cd "$REPO" && TGM_PLUGIN_DIR="$REPO" "$PY" scripts/test-selfupdate.py 2>&1 | tail -1 )
}

case "${1:-check}" in
  check)
    echo "--- drift (repo -> deploy) ---"
    if drift; then echo "  in sync"; else echo "  OUT OF SYNC: run sync.sh deploy"; fi
    echo "--- version ---"
    echo "  repo:   $(grep -m1 '^version:' "$REPO/plugin.yaml" 2>/dev/null | tr -d ' ')"
    echo "  deploy: $(grep -m1 '^version:' "$DEPLOY/plugin.yaml" 2>/dev/null | tr -d ' ')"
    tests
    ;;
  deploy)
    echo "--- copying repo -> deploy ---"
    for f in "${FILES[@]}"; do
      [ -f "$REPO/$f" ] && cp "$REPO/$f" "$DEPLOY/$f" && echo "  $f"
    done
    for d in "${DIRS[@]}"; do
      [ -d "$REPO/$d" ] || continue
      while IFS= read -r f; do
        rel="${f#$REPO/}"
        mkdir -p "$(dirname "$DEPLOY/$rel")"
        cp "$f" "$DEPLOY/$rel"
        echo "  $rel"
      done < <(find "$REPO/$d" -type f ! -path '*/__cache__/*' ! -name '*.pyc')
    done
    # Full unlock bridge: vendored telethon lives only in the deploy dir —
    # the repo never carries it and the Hermes venv is left alone.
    if [ ! -d "$DEPLOY/deps/telethon" ]; then
      if command -v uv >/dev/null 2>&1; then
        echo "--- bridge deps: installing telethon into deps/ ---"
        uv pip install --target "$DEPLOY/deps" telethon >/dev/null 2>&1 \
          || echo "  WARN: bridge deps failed — Full unlock stays inert"
      else
        echo "  WARN: uv not found — bridge deps missing (Full unlock inert)"
      fi
    fi
    echo "--- drift after copy ---"
    drift && echo "  in sync"
    tests
    reload
    ;;
  reload)
    reload
    ;;
  *)
    echo "usage: $0 {check|deploy|reload}" >&2; exit 2
    ;;
esac
