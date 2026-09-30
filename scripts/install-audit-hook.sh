#!/usr/bin/env bash
# Install the pre-publish audit into .git/hooks (idempotent).
# Any push that would leak personal data or credentials is refused.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
mkdir -p .git/hooks
cat > .git/hooks/pre-push <<'HOOK'
#!/usr/bin/env bash
exec python3 "$(git rev-parse --show-toplevel)/scripts/pre-push-audit.py" "$@"
HOOK
chmod +x .git/hooks/pre-push
echo "installed .git/hooks/pre-push -> scripts/pre-push-audit.py"