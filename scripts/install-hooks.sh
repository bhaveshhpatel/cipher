#!/usr/bin/env bash
# =============================================================================
# Cipher — Install Git Pre-Push Hook
# =============================================================================
# Run ONCE after cloning:
#   bash scripts/install-hooks.sh
#
# This symlinks scripts/pre-push-check.sh into .git/hooks/pre-push so that
# `git push` automatically runs the full regression check before the push
# goes through.  To bypass in emergencies: git push --no-verify
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOKS_DIR="$REPO_ROOT/.git/hooks"
PRE_PUSH_SCRIPT="$REPO_ROOT/scripts/pre-push-check.sh"
HOOK_TARGET="$HOOKS_DIR/pre-push"

# Guard: must be run from inside a git repo
if [[ ! -d "$HOOKS_DIR" ]]; then
  echo "[ERROR] .git/hooks not found. Are you inside the cipher repo?"
  exit 1
fi

# Make the script executable
chmod +x "$PRE_PUSH_SCRIPT"

# Symlink (overwrite if already exists)
ln -sf "$PRE_PUSH_SCRIPT" "$HOOK_TARGET"
chmod +x "$HOOK_TARGET"

echo ""
echo "✔ Pre-push hook installed."
echo ""
echo "  Every 'git push' will now run:  bash scripts/pre-push-check.sh"
echo "  To bypass (emergencies only):   git push --no-verify"
echo "  To uninstall:                   rm .git/hooks/pre-push"
echo ""
