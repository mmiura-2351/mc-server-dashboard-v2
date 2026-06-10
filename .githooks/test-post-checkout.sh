#!/usr/bin/env bash
#
# test-post-checkout.sh: self-contained unit tests for .githooks/post-checkout.
#
# Creates an isolated temporary git repo to simulate the primary checkout
# (toplevel path does NOT contain .claude/worktrees/) and exercises the four
# behaviours: worktree exemption, clean-tree auto-restore, dirty-tree refusal,
# MCSD_ALLOW_PRIMARY_BRANCH override, in-operation guard (rebase/bisect), and
# staged-only / dirty-detached-HEAD edge cases.
#
# Exit code: 0 = all pass, non-zero = first failure (set -e).
set -euo pipefail

HOOK="$(cd "$(dirname "$0")" && pwd)/post-checkout"
if [ ! -x "$HOOK" ]; then
	echo "FAIL: hook not found or not executable: $HOOK" >&2
	exit 1
fi

pass=0
fail=0

ok()  { echo "  PASS: $1"; pass=$((pass + 1)); }
fail_test() { echo "  FAIL: $1"; fail=$((fail + 1)); }

# ---------------------------------------------------------------------------
# Helper: create a throwaway git repo with a 'main' branch and one commit.
# Prints the repo path.
make_repo() {
	local dir
	dir="$(mktemp -d)"
	git -C "$dir" init -b main -q
	git -C "$dir" config user.email "test@example.com"
	git -C "$dir" config user.name "Test"
	touch "$dir/file.txt"
	git -C "$dir" add file.txt
	git -C "$dir" commit -q -m "init"
	echo "$dir"
}

# Run the hook inside a repo; returns the exit code.
# Usage: run_hook <repo> <prev_sha> <new_sha> <flag> [env_overrides...]
run_hook() {
	local repo="$1" prev="$2" new="$3" flag="$4"
	shift 4
	# Execute with the repo as GIT_DIR context so git commands inside the hook
	# operate on the test repo, not the real one.
	(
		cd "$repo"
		# shellcheck disable=SC2294
		env "$@" bash "$HOOK" "$prev" "$new" "$flag" 2>/dev/null
	)
}

run_hook_stderr() {
	local repo="$1" prev="$2" new="$3" flag="$4"
	shift 4
	(
		cd "$repo"
		env "$@" bash "$HOOK" "$prev" "$new" "$flag" 2>&1 >/dev/null
	)
}

# ---------------------------------------------------------------------------
echo "=== post-checkout tests ==="

# --- 1. File checkout ($3 == 0) is always a no-op ---
{
	repo="$(make_repo)"
	run_hook "$repo" "abc" "def" "0"
	ok "file checkout (flag=0) exits silently"
	rm -rf "$repo"
}

# --- 2. Worktree path (.claude/worktrees/) is exempt ---
{
	# Simulate a worktree by placing the repo inside a path containing
	# .claude/worktrees/ and then having git report that as the toplevel.
	# Easiest: create the repo at a path that matches the pattern.
	base="$(mktemp -d)"
	repo="$base/.claude/worktrees/agent-xyz"
	mkdir -p "$repo"
	git -C "$repo" init -b main -q
	git -C "$repo" config user.email "test@example.com"
	git -C "$repo" config user.name "Test"
	touch "$repo/f"
	git -C "$repo" add f
	git -C "$repo" commit -q -m "init"
	# Create a branch (so HEAD is not on main).
	git -C "$repo" checkout -q -b some-branch
	# Hook should exit silently (worktree exempt).
	out="$(run_hook_stderr "$repo" "abc" "def" "1")"
	if [ -z "$out" ]; then
		ok "worktree path is exempt (silent)"
	else
		fail_test "worktree path produced output: $out"
	fi
	rm -rf "$base"
}

# --- 3. Primary checkout on main -- no action ---
{
	repo="$(make_repo)"
	# HEAD is already on main; hook should be silent.
	out="$(run_hook_stderr "$repo" "abc" "def" "1")"
	if [ -z "$out" ]; then
		ok "primary checkout on main exits silently"
	else
		fail_test "primary on main produced output: $out"
	fi
	rm -rf "$repo"
}

# --- 4. Clean-tree auto-restore ---
{
	repo="$(make_repo)"
	main_sha="$(git -C "$repo" rev-parse HEAD)"
	git -C "$repo" checkout -q -b feature-x
	feature_sha="$(git -C "$repo" rev-parse HEAD)"
	# Now HEAD is on feature-x; simulate the hook firing.
	(
		cd "$repo"
		bash "$HOOK" "$main_sha" "$feature_sha" "1" 2>/dev/null
	)
	restored="$(git -C "$repo" symbolic-ref --short HEAD 2>/dev/null)"
	if [ "$restored" = "main" ]; then
		ok "clean tree: auto-restored to main"
	else
		fail_test "clean tree: expected HEAD=main, got HEAD=$restored"
	fi
	rm -rf "$repo"
}

# --- 5. Clean-tree auto-restore prints AUTO-RESTORE message ---
{
	repo="$(make_repo)"
	main_sha="$(git -C "$repo" rev-parse HEAD)"
	git -C "$repo" checkout -q -b feature-y
	feature_sha="$(git -C "$repo" rev-parse HEAD)"
	out="$(
		cd "$repo"
		bash "$HOOK" "$main_sha" "$feature_sha" "1" 2>&1 >/dev/null
	)"
	if echo "$out" | grep -q "AUTO-RESTORE"; then
		ok "clean tree: auto-restore message printed"
	else
		fail_test "clean tree: missing AUTO-RESTORE in output: $out"
	fi
	rm -rf "$repo"
}

# --- 6. Dirty working tree (unstaged): auto-restore refused, exits non-zero ---
{
	repo="$(make_repo)"
	main_sha="$(git -C "$repo" rev-parse HEAD)"
	git -C "$repo" checkout -q -b feature-dirty
	feature_sha="$(git -C "$repo" rev-parse HEAD)"
	# Make the working tree dirty (unstaged change).
	echo "dirty" >> "$repo/file.txt"
	exit_code=0
	(
		cd "$repo"
		bash "$HOOK" "$main_sha" "$feature_sha" "1" 2>/dev/null
	) || exit_code=$?
	if [ "$exit_code" -ne 0 ]; then
		ok "dirty tree: hook exits non-zero (refused)"
	else
		fail_test "dirty tree: expected non-zero exit, got 0"
	fi
	# Verify HEAD was NOT restored (still on feature-dirty).
	current="$(git -C "$repo" symbolic-ref --short HEAD 2>/dev/null)"
	if [ "$current" = "feature-dirty" ]; then
		ok "dirty tree: HEAD not changed"
	else
		fail_test "dirty tree: HEAD changed unexpectedly to $current"
	fi
	rm -rf "$repo"
}

# --- 7. Dirty tree: ERROR message printed ---
{
	repo="$(make_repo)"
	main_sha="$(git -C "$repo" rev-parse HEAD)"
	git -C "$repo" checkout -q -b feature-dirty2
	feature_sha="$(git -C "$repo" rev-parse HEAD)"
	echo "dirty" >> "$repo/file.txt"
	out="$(
		cd "$repo"
		bash "$HOOK" "$main_sha" "$feature_sha" "1" 2>&1 >/dev/null || true
	)"
	if echo "$out" | grep -q "ERROR"; then
		ok "dirty tree: ERROR message printed"
	else
		fail_test "dirty tree: missing ERROR in output: $out"
	fi
	rm -rf "$repo"
}

# --- 8. MCSD_ALLOW_PRIMARY_BRANCH=1: skip restore, stay on branch ---
{
	repo="$(make_repo)"
	main_sha="$(git -C "$repo" rev-parse HEAD)"
	git -C "$repo" checkout -q -b feature-override
	feature_sha="$(git -C "$repo" rev-parse HEAD)"
	(
		cd "$repo"
		MCSD_ALLOW_PRIMARY_BRANCH=1 bash "$HOOK" "$main_sha" "$feature_sha" "1" 2>/dev/null
	)
	current="$(git -C "$repo" symbolic-ref --short HEAD 2>/dev/null)"
	if [ "$current" = "feature-override" ]; then
		ok "MCSD_ALLOW_PRIMARY_BRANCH=1: branch left in place"
	else
		fail_test "MCSD_ALLOW_PRIMARY_BRANCH=1: expected feature-override, got $current"
	fi
	rm -rf "$repo"
}

# --- 9. MCSD_ALLOW_PRIMARY_BRANCH=1: NOTICE message printed ---
{
	repo="$(make_repo)"
	main_sha="$(git -C "$repo" rev-parse HEAD)"
	git -C "$repo" checkout -q -b feature-override2
	feature_sha="$(git -C "$repo" rev-parse HEAD)"
	out="$(
		cd "$repo"
		MCSD_ALLOW_PRIMARY_BRANCH=1 bash "$HOOK" "$main_sha" "$feature_sha" "1" 2>&1 >/dev/null
	)"
	if echo "$out" | grep -q "NOTICE"; then
		ok "MCSD_ALLOW_PRIMARY_BRANCH=1: NOTICE message printed"
	else
		fail_test "MCSD_ALLOW_PRIMARY_BRANCH=1: missing NOTICE in output: $out"
	fi
	rm -rf "$repo"
}

# --- 10. Detached HEAD: auto-restored to main ---
{
	repo="$(make_repo)"
	main_sha="$(git -C "$repo" rev-parse HEAD)"
	# Detach HEAD.
	git -C "$repo" checkout -q --detach HEAD
	detached_sha="$(git -C "$repo" rev-parse HEAD)"
	(
		cd "$repo"
		bash "$HOOK" "$main_sha" "$detached_sha" "1" 2>/dev/null
	)
	current="$(git -C "$repo" symbolic-ref --short HEAD 2>/dev/null || echo "detached")"
	if [ "$current" = "main" ]; then
		ok "detached HEAD: auto-restored to main"
	else
		fail_test "detached HEAD: expected main, got $current"
	fi
	rm -rf "$repo"
}

# --- 11. In-operation guard: rebase-merge dir present → hook skips ---
{
	repo="$(make_repo)"
	main_sha="$(git -C "$repo" rev-parse HEAD)"
	git -C "$repo" checkout -q -b feature-rebase
	feature_sha="$(git -C "$repo" rev-parse HEAD)"
	# Simulate a rebase in progress by creating the rebase-merge marker dir.
	mkdir -p "$repo/.git/rebase-merge"
	out="$(run_hook_stderr "$repo" "$main_sha" "$feature_sha" "1")"
	hook_exit=0
	(
		cd "$repo"
		bash "$HOOK" "$main_sha" "$feature_sha" "1" 2>/dev/null
	) || hook_exit=$?
	current="$(git -C "$repo" symbolic-ref --short HEAD 2>/dev/null)"
	if [ "$hook_exit" -eq 0 ] && [ "$current" = "feature-rebase" ]; then
		ok "rebase-merge in progress: hook skips (branch unchanged)"
	else
		fail_test "rebase-merge guard: expected skip, got exit=$hook_exit branch=$current"
	fi
	rm -rf "$repo"
}

# --- 12. In-operation guard: BISECT_LOG present → hook skips ---
{
	repo="$(make_repo)"
	main_sha="$(git -C "$repo" rev-parse HEAD)"
	git -C "$repo" checkout -q -b feature-bisect
	feature_sha="$(git -C "$repo" rev-parse HEAD)"
	# Simulate a bisect session by creating the BISECT_LOG marker file.
	touch "$repo/.git/BISECT_LOG"
	hook_exit=0
	(
		cd "$repo"
		bash "$HOOK" "$main_sha" "$feature_sha" "1" 2>/dev/null
	) || hook_exit=$?
	current="$(git -C "$repo" symbolic-ref --short HEAD 2>/dev/null)"
	if [ "$hook_exit" -eq 0 ] && [ "$current" = "feature-bisect" ]; then
		ok "bisect in progress: hook skips (branch unchanged)"
	else
		fail_test "bisect guard: expected skip, got exit=$hook_exit branch=$current"
	fi
	rm -rf "$repo"
}

# --- 13. Staged-only dirty tree: auto-restore refused ---
{
	repo="$(make_repo)"
	main_sha="$(git -C "$repo" rev-parse HEAD)"
	git -C "$repo" checkout -q -b feature-staged
	feature_sha="$(git -C "$repo" rev-parse HEAD)"
	# Stage a change without committing (staged-only dirty).
	echo "staged change" >> "$repo/file.txt"
	git -C "$repo" add file.txt
	exit_code=0
	(
		cd "$repo"
		bash "$HOOK" "$main_sha" "$feature_sha" "1" 2>/dev/null
	) || exit_code=$?
	if [ "$exit_code" -ne 0 ]; then
		ok "staged-only dirty tree: hook exits non-zero (refused)"
	else
		fail_test "staged-only dirty tree: expected non-zero exit, got 0"
	fi
	current="$(git -C "$repo" symbolic-ref --short HEAD 2>/dev/null)"
	if [ "$current" = "feature-staged" ]; then
		ok "staged-only dirty tree: HEAD not changed"
	else
		fail_test "staged-only dirty tree: HEAD changed unexpectedly to $current"
	fi
	rm -rf "$repo"
}

# --- 14. Dirty detached HEAD: auto-restore refused ---
{
	repo="$(make_repo)"
	main_sha="$(git -C "$repo" rev-parse HEAD)"
	# Detach HEAD.
	git -C "$repo" checkout -q --detach HEAD
	detached_sha="$(git -C "$repo" rev-parse HEAD)"
	# Make the working tree dirty (unstaged change).
	echo "dirty" >> "$repo/file.txt"
	exit_code=0
	(
		cd "$repo"
		bash "$HOOK" "$main_sha" "$detached_sha" "1" 2>/dev/null
	) || exit_code=$?
	if [ "$exit_code" -ne 0 ]; then
		ok "dirty detached HEAD: hook exits non-zero (refused)"
	else
		fail_test "dirty detached HEAD: expected non-zero exit, got 0"
	fi
	current="$(git -C "$repo" symbolic-ref --short HEAD 2>/dev/null || echo "detached")"
	if [ "$current" = "detached" ]; then
		ok "dirty detached HEAD: stays detached (not restored)"
	else
		fail_test "dirty detached HEAD: HEAD changed unexpectedly to $current"
	fi
	rm -rf "$repo"
}

# ---------------------------------------------------------------------------
echo
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
