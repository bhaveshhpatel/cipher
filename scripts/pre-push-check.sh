#!/usr/bin/env bash
# =============================================================================
# Cipher — Local Pre-Push Regression Check
# =============================================================================
# Mirrors exactly what regression-gate.yml does on CI.
# Run manually:  bash scripts/pre-push-check.sh
# Run via hook:  bash scripts/install-hooks.sh  (one-time setup)
#
# Exit codes:
#   0  — all checks passed, safe to push
#   1  — one or more checks failed, push blocked
# =============================================================================
set -euo pipefail

# ───────────────────────────────────────────────────────────────────────────
# Colour helpers — degrade gracefully if terminal has no colour support
# ───────────────────────────────────────────────────────────────────────────
if command -v tput &>/dev/null && tput setaf 1 &>/dev/null; then
  RED=$(tput setaf 1); GREEN=$(tput setaf 2); YELLOW=$(tput setaf 3)
  CYAN=$(tput setaf 6); BOLD=$(tput bold); RESET=$(tput sgr0)
else
  RED=''; GREEN=''; YELLOW=''; CYAN=''; BOLD=''; RESET=''
fi

DIVIDER="${CYAN}$(printf '=%.0s' {1..72})${RESET}"
PASS="${GREEN}${BOLD}[PASS]${RESET}"
FAIL="${RED}${BOLD}[FAIL]${RESET}"
INFO="${YELLOW}[INFO]${RESET}"

# Repo root — always resolve relative to this script so it works from any cwd
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo ""
echo "$DIVIDER"
echo "${BOLD}  Cipher — Pre-Push Regression Check${RESET}"
echo "$DIVIDER"

BACKEND_OK=0
FRONTEND_OK=0

# ===========================================================================
# BACKEND
# ===========================================================================
echo ""
echo "${CYAN}${BOLD}► BACKEND${RESET}"
echo ""

cd "$REPO_ROOT/backend"

# 1a. Syntax check (fast — no heavy deps)
echo "${INFO} Running pyflakes syntax check..."
if python -m pyflakes . 2>&1; then
  echo "$PASS pyflakes clean"
else
  echo "$FAIL pyflakes found syntax errors"
  BACKEND_OK=1
fi

# 1b. Pytest regression suite with coverage
echo ""
echo "${INFO} Running pytest regression suite (--cov-fail-under=92, --cov-branch)..."
if pytest \
    -x \
    --tb=short \
    -q \
    --cov=. \
    --cov-branch \
    --cov-report=term-missing \
    --cov-report=xml:coverage.xml \
    --cov-report=json:coverage.json \
    --cov-fail-under=92 \
    --cov-config=.coveragerc; then
  echo ""
  echo "$PASS Backend regression suite passed (coverage ≥ 92%)"
else
  echo ""
  echo "$FAIL Backend regression suite FAILED"
  BACKEND_OK=1
fi

# ===========================================================================
# FRONTEND
# ===========================================================================
echo ""
echo "${CYAN}${BOLD}► FRONTEND${RESET}"
echo ""

cd "$REPO_ROOT/frontend"

# 2a. TypeScript type check
echo "${INFO} Running TypeScript type check..."
if npm run typecheck 2>&1; then
  echo "$PASS Type check passed"
else
  echo "$FAIL TypeScript errors found"
  FRONTEND_OK=1
fi

# 2b. ESLint
echo ""
echo "${INFO} Running ESLint..."
if npm run lint 2>&1; then
  echo "$PASS ESLint passed"
else
  echo "$FAIL ESLint errors found"
  FRONTEND_OK=1
fi

# 2c. Jest regression suite with coverage thresholds
echo ""
echo "${INFO} Running Jest regression suite (coverageThreshold enforced)..."
if npx jest --ci --coverage 2>&1; then
  echo ""
  echo "$PASS Frontend regression suite passed"
else
  echo ""
  echo "$FAIL Frontend regression suite FAILED"
  FRONTEND_OK=1
fi

# ===========================================================================
# SUMMARY
# ===========================================================================
cd "$REPO_ROOT"
echo ""
echo "$DIVIDER"
echo "${BOLD}  Summary${RESET}"
echo "$DIVIDER"

if [[ $BACKEND_OK -eq 0 ]]; then
  echo "  Backend   $PASS"
else
  echo "  Backend   $FAIL"
fi

if [[ $FRONTEND_OK -eq 0 ]]; then
  echo "  Frontend  $PASS"
else
  echo "  Frontend  $FAIL"
fi

echo ""

if [[ $BACKEND_OK -ne 0 || $FRONTEND_OK -ne 0 ]]; then
  echo "${RED}${BOLD}  ✖ Pre-push check FAILED — push blocked.${RESET}"
  echo "  Fix the issues above, then re-run:  bash scripts/pre-push-check.sh"
  echo ""
  exit 1
fi

echo "${GREEN}${BOLD}  ✔ All checks passed — safe to push.${RESET}"
echo ""
exit 0
