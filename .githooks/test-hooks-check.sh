#!/usr/bin/env bash
#
# test-hooks-check.sh: unit tests for the hooks-check Make target logic.
#
# Exercises the four behaviours: CI skip, worktree-path skip, wrong-hooksPath
# failure, test-identity failure.  Uses a temp git repo + a self-contained
# copy of the hooks-check shell fragment so the tests do not depend on the
# Makefile being present in the temp dir.
#
# Exit code: 0 = all pass, non-zero = first failure (set -e).
set -euo pipefail

# Drop GIT_* leaks from any enclosing hook / test runner.
unset "${!GIT_@}"

pass=0
fail=0

ok()        { echo "  PASS: $1"; pass=$((pass + 1)); }
fail_test() { echo "  FAIL: $1"; fail=$((fail + 1)); }

# ---------------------------------------------------------------------------
# The hooks-check logic extracted as a testable shell function.
# Mirrors the Makefile hooks-check target exactly: CI skip, worktree skip,
# hooksPath assertion, identity assertion.
# ---------------------------------------------------------------------------
run_hooks_check() {
	# Caller may set: CI, _FORCE_TOPLEVEL, _FORCE_HOOKSPATH, _FORCE_NAME,
	# _FORCE_EMAIL.  All default to values that pass.
	local ci="${CI:-false}"
	local toplevel="${_FORCE_TOPLEVEL:-/some/normal/repo}"
	local hookspath="${_FORCE_HOOKSPATH-.githooks}"
	local name="${_FORCE_NAME:-Real Name}"
	local email="${_FORCE_EMAIL:-real@example.com}"

	[ "$ci" = "true" ] && return 0
	case "$toplevel" in */.claude/worktrees/*) return 0 ;; esac
	local _fail=0
	if [ "$hookspath" != ".githooks" ]; then
		echo "FAIL: git core.hooksPath is not '.githooks'"
		echo "  current: ${hookspath:-<unset>}"
		_fail=1
	fi
	if [ "$name" = "Test" ] || [ "$email" = "test@example.com" ]; then
		echo "FAIL: git identity is the test identity (Test/test@example.com)."
		echo "  user.name=$name  user.email=$email"
		_fail=1
	fi
	return "$_fail"
}

# ---------------------------------------------------------------------------
echo "=== hooks-check tests ==="

# --- 1. CI=true: always exits 0, regardless of hooksPath ---
{
	exit_code=0
	(
		CI=true \
		_FORCE_HOOKSPATH="/wrong/absolute/.git/hooks" \
		run_hooks_check 2>/dev/null
	) || exit_code=$?
	if [ "$exit_code" -eq 0 ]; then
		ok "CI=true: skips all checks (exits 0)"
	else
		fail_test "CI=true: expected exit 0, got $exit_code"
	fi
}

# --- 2. Worktree path: exits 0 regardless of hooksPath ---
{
	exit_code=0
	(
		CI=false \
		_FORCE_TOPLEVEL="/home/user/repo/.claude/worktrees/agent-abc" \
		_FORCE_HOOKSPATH="/wrong/absolute/.git/hooks" \
		run_hooks_check 2>/dev/null
	) || exit_code=$?
	if [ "$exit_code" -eq 0 ]; then
		ok "worktree path: skips checks (exits 0)"
	else
		fail_test "worktree path: expected exit 0, got $exit_code"
	fi
}

# --- 3. Wrong hooksPath (absolute): exits non-zero ---
{
	exit_code=0
	(
		CI=false \
		_FORCE_TOPLEVEL="/home/user/repo" \
		_FORCE_HOOKSPATH="/home/user/repo/.git/hooks" \
		run_hooks_check 2>/dev/null
	) || exit_code=$?
	if [ "$exit_code" -ne 0 ]; then
		ok "wrong hooksPath (absolute): exits non-zero"
	else
		fail_test "wrong hooksPath: expected non-zero, got 0"
	fi
}

# --- 4. Unset hooksPath: exits non-zero ---
{
	exit_code=0
	(
		CI=false \
		_FORCE_TOPLEVEL="/home/user/repo" \
		_FORCE_HOOKSPATH="" \
		run_hooks_check 2>/dev/null
	) || exit_code=$?
	if [ "$exit_code" -ne 0 ]; then
		ok "unset hooksPath: exits non-zero"
	else
		fail_test "unset hooksPath: expected non-zero, got 0"
	fi
}

# --- 5. Test identity (name): exits non-zero ---
{
	exit_code=0
	(
		CI=false \
		_FORCE_TOPLEVEL="/home/user/repo" \
		_FORCE_HOOKSPATH=".githooks" \
		_FORCE_NAME="Test" \
		_FORCE_EMAIL="real@example.com" \
		run_hooks_check 2>/dev/null
	) || exit_code=$?
	if [ "$exit_code" -ne 0 ]; then
		ok "test name 'Test': exits non-zero"
	else
		fail_test "test name: expected non-zero, got 0"
	fi
}

# --- 6. Test identity (email): exits non-zero ---
{
	exit_code=0
	(
		CI=false \
		_FORCE_TOPLEVEL="/home/user/repo" \
		_FORCE_HOOKSPATH=".githooks" \
		_FORCE_NAME="Real Name" \
		_FORCE_EMAIL="test@example.com" \
		run_hooks_check 2>/dev/null
	) || exit_code=$?
	if [ "$exit_code" -ne 0 ]; then
		ok "test email 'test@example.com': exits non-zero"
	else
		fail_test "test email: expected non-zero, got 0"
	fi
}

# --- 7. Correct config: exits 0 ---
{
	exit_code=0
	(
		CI=false \
		_FORCE_TOPLEVEL="/home/user/repo" \
		_FORCE_HOOKSPATH=".githooks" \
		_FORCE_NAME="Real Name" \
		_FORCE_EMAIL="real@example.com" \
		run_hooks_check 2>/dev/null
	) || exit_code=$?
	if [ "$exit_code" -eq 0 ]; then
		ok "correct config: exits 0"
	else
		fail_test "correct config: expected exit 0, got $exit_code"
	fi
}

# --- 8. Both wrong hooksPath and test identity: exits non-zero ---
{
	exit_code=0
	(
		CI=false \
		_FORCE_TOPLEVEL="/home/user/repo" \
		_FORCE_HOOKSPATH="/wrong/.git/hooks" \
		_FORCE_NAME="Test" \
		_FORCE_EMAIL="test@example.com" \
		run_hooks_check 2>/dev/null
	) || exit_code=$?
	if [ "$exit_code" -ne 0 ]; then
		ok "both failures: exits non-zero"
	else
		fail_test "both failures: expected non-zero, got 0"
	fi
}

# ---------------------------------------------------------------------------
echo
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
