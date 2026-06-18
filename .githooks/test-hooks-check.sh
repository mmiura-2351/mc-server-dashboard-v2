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
	# _FORCE_EMAIL, _FORCE_GITDIR.  All default to values that pass.
	local ci="${CI:-false}"
	local toplevel="${_FORCE_TOPLEVEL:-/some/normal/repo}"
	local hookspath="${_FORCE_HOOKSPATH-.githooks}"
	local name="${_FORCE_NAME:-Real Name}"
	local email="${_FORCE_EMAIL:-real@example.com}"
	local gitdir="${_FORCE_GITDIR:-}"

	[ "$ci" = "true" ] && return 0
	case "$toplevel" in */.claude/worktrees/*) return 0 ;; esac
	local _fail=0
	if [ "$hookspath" != ".githooks" ]; then
		if [ -n "$gitdir" ] && \
		   [ -x "$gitdir/hooks/post-checkout" ] && \
		   [ -x "$gitdir/hooks/pre-commit" ] && \
		   [ -x "$gitdir/hooks/pre-push" ]; then
			echo "WARN: core.hooksPath is not '.githooks' but symlinks exist in .git/hooks/ -- hooks will fire."
			echo "  Run 'make hooks-install' to also fix the config value."
		else
			echo "FAIL: git core.hooksPath is not '.githooks'"
			echo "  current: ${hookspath:-<unset>}"
			_fail=1
		fi
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

# --- 8. Wrong hooksPath + all symlinks present: exits 0 (WARN) ---
{
	tmpdir="$(mktemp -d)"
	mkdir -p "$tmpdir/hooks"
	# Create executable symlink targets (real files, simulating .githooks/).
	for h in post-checkout pre-commit pre-push; do
		printf '#!/bin/sh\n' > "$tmpdir/$h"
		chmod +x "$tmpdir/$h"
		ln -s "../$h" "$tmpdir/hooks/$h"
	done
	exit_code=0
	out="$(
		CI=false \
		_FORCE_TOPLEVEL="/home/user/repo" \
		_FORCE_HOOKSPATH="/home/user/repo/.git/hooks" \
		_FORCE_GITDIR="$tmpdir" \
		run_hooks_check 2>/dev/null
	)" || exit_code=$?
	if [ "$exit_code" -eq 0 ] && echo "$out" | grep -q "WARN"; then
		ok "wrong hooksPath + symlinks present: exits 0 (WARN)"
	else
		fail_test "wrong hooksPath + symlinks present: expected exit 0 + WARN, got exit=$exit_code output=$out"
	fi
	rm -rf "$tmpdir"
}

# --- 9. Wrong hooksPath + no symlinks: exits non-zero (FAIL) ---
{
	tmpdir="$(mktemp -d)"
	mkdir -p "$tmpdir/hooks"
	exit_code=0
	(
		CI=false \
		_FORCE_TOPLEVEL="/home/user/repo" \
		_FORCE_HOOKSPATH="/home/user/repo/.git/hooks" \
		_FORCE_GITDIR="$tmpdir" \
		run_hooks_check 2>/dev/null
	) || exit_code=$?
	if [ "$exit_code" -ne 0 ]; then
		ok "wrong hooksPath + no symlinks: exits non-zero (FAIL)"
	else
		fail_test "wrong hooksPath + no symlinks: expected non-zero, got 0"
	fi
	rm -rf "$tmpdir"
}

# --- 10. Wrong hooksPath + partial symlinks (2 of 3): exits non-zero (FAIL) ---
{
	tmpdir="$(mktemp -d)"
	mkdir -p "$tmpdir/hooks"
	for h in post-checkout pre-commit; do
		printf '#!/bin/sh\n' > "$tmpdir/$h"
		chmod +x "$tmpdir/$h"
		ln -s "../$h" "$tmpdir/hooks/$h"
	done
	exit_code=0
	(
		CI=false \
		_FORCE_TOPLEVEL="/home/user/repo" \
		_FORCE_HOOKSPATH="/home/user/repo/.git/hooks" \
		_FORCE_GITDIR="$tmpdir" \
		run_hooks_check 2>/dev/null
	) || exit_code=$?
	if [ "$exit_code" -ne 0 ]; then
		ok "wrong hooksPath + partial symlinks (2/3): exits non-zero (FAIL)"
	else
		fail_test "wrong hooksPath + partial symlinks: expected non-zero, got 0"
	fi
	rm -rf "$tmpdir"
}

# --- 11. Both wrong hooksPath and test identity: exits non-zero ---
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
