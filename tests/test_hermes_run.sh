#!/bin/bash
# test_hermes_run.sh — exercise the gateway's own run script WITHOUT bouncing
# the live gateway: s6, curl and the health endpoint are stubbed, everything else
# (the decision tree) is the real script. The path comes from the same place the
# plugin gets it, because a literal host path here fails the pre-push audit.
#
#   bash tests/test_hermes_run.sh
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
REAL="$REPO/../../scripts/hermes_run.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fail=0
ck() { if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1 (got '$2', want '$3')"; fail=1; fi; }

[ -f "$REAL" ] || { echo "  FAIL missing $REAL"; exit 1; }

# --- stubs ------------------------------------------------------------------
cat >"$TMP/s6-svc" <<'EOF'
#!/bin/bash
echo "$@" >>"$TESTDIR/calls.log"
EOF
cat >"$TMP/s6-svstat" <<'EOF'
#!/bin/bash
echo "up (pid 344023 pgid 344023) 10 seconds"
EOF
cat >"$TMP/curl" <<'EOF'
#!/bin/bash
exit 0
EOF
cat >"$TMP/pgrep" <<'EOF'
#!/bin/bash
echo 344023
EOF
chmod +x "$TMP"/*
# curl/pgrep are resolved through PATH — without this the real ones would be
# used and the real health endpoint (and real gateway pid) would leak into the
# test, turning a dry run into a live bounce.
export TESTDIR="$TMP"
export PATH="$TMP:$PATH"

# --- run the real script with only its paths swapped ------------------------
sed -e "s|^SVC=.*|SVC=\"$TMP/svc\"|" \
    -e "s|^SVCCTL=.*|SVCCTL=\"$TMP/s6-svc\"|" \
    -e "s|^SVCSTAT=.*|SVCSTAT=\"$TMP/s6-svstat\"|" \
    -e "s|^HEALTH=.*|HEALTH=\"http://127.0.0.1:1/health\"|" \
    -e "s|^GWLOG=.*|GWLOG=\"$TMP/gw.log\"|" \
    -e "s|^EXITLOG=.*|EXITLOG=\"$TMP/exit.log\"|" \
    -e "s|^RUNLOG=.*|RUNLOG=\"$TMP/run.log\"|" \
    "$REAL" >"$TMP/hermes_run.sh"

# auto mode against a healthy gateway must bounce: stop, then start.
: >"$TMP/calls.log"
timeout 45 bash "$TMP/hermes_run.sh" auto >"$TMP/auto.out" 2>&1; rc=$?
ck "auto exits 0" "$rc" "0"
ck "auto stops the service" "$(grep -c -- '-d ' "$TMP/calls.log")" "1"
ck "auto starts it again"  "$(grep -c -- '-u ' "$TMP/calls.log")" "1"
ck "auto order is stop-then-start" "$(head -1 "$TMP/calls.log")" "-d $TMP/svc"

# start mode against a healthy gateway must NOT touch the service.
: >"$TMP/calls.log"
CALLER=test timeout 45 bash "$TMP/hermes_run.sh" start >"$TMP/start.out" 2>&1; rc=$?
ck "start exits 0" "$rc" "0"
ck "start does not touch s6" "$(wc -c <"$TMP/calls.log" | tr -d ' ')" "0"
ck "start says already up" "$(grep -c 'already up' "$TMP/start.out")" "1"

# a usage error for anything that is not a known mode.
timeout 15 bash "$TMP/hermes_run.sh" bogus >/dev/null 2>&1; rc=$?
ck "unknown mode exits 2" "$rc" "2"

echo
if [ "$fail" -eq 0 ]; then echo "hermes_run: all checks passed"; else echo "hermes_run: FAILURES"; fi
exit "$fail"
