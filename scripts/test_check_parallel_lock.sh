#!/usr/bin/env bash
#
# test_check_parallel_lock.sh: one gate at a time per host (issue #2513).
#
# The defect. `make check` was written on the assumption that it owns the
# machine, and stopped owning it once several agent worktrees ran on one box:
# four concurrent gates each fan pytest out with `-n auto`, which sizes the pool
# to the host's core count, so a 4-core box ran ~16 workers plus four vitest
# pools and four Go suites. The fs-heavy api tests lost first, because an fsync
# under that load waits on everyone else's writeback: a test that takes 3.6 s
# alone was killed at the 120 s per-test cap, ~33x degradation, and the reds
# landed on diffs that touched no Python at all. Re-running was the correct
# response and indistinguishable from the wrong one, so the gate's own signal
# stopped meaning anything and a push was pushed with --no-verify (PR #2517).
#
# The fix under test is mutual exclusion, not a bigger budget: an exclusive
# flock on a host-global lock file, taken before the run does any work, so a
# contended push waits once instead of failing and retrying. `-n auto` is then
# the right size again, because the run really does own the host.
#
# What is asserted, and why in this shape:
#
#   1. A second run does not start work while a first holds the lock. This is
#      the whole behavior; everything else is diagnostics around it.
#   2. The waiting run names the holder's worktree. A wait with no explanation
#      is indistinguishable from a hang, and the holder is frequently the same
#      worktree -- a gate orphaned by a killed `git push` keeps the lock through
#      the inherited descriptor -- which is exactly the case that used to be
#      diagnosed by hand (docs/dev/AGENTS.md Section 3).
#   3. The waiter proceeds once the holder releases. A lock that never lets go
#      would also satisfy assertion 1.
#   4. A nested invocation does not block. scripts-test runs check_parallel.sh
#      itself (test_check_parallel_identity.sh, and this file), so the gate
#      would otherwise wait for its own lock forever.
#
# The runs use a stub `make` on PATH: the stub is reached at the pre-flight
# golangci-lint install -- the first thing the script runs after the lock -- so
# "reached the stub" means "got past the lock", and the stub then fails, which
# stops the run before any chain is spawned. No pytest, no vitest, no cores.
#
# Isolation details that are load-bearing:
#
#   * `GIT_*` is unset first: `git push` exports GIT_DIR pointing at the real
#     repository, and check_parallel.sh derives its log dir from `git rev-parse`
#     (same reason as test_shell_pipefail.sh and the identity test).
#   * `MCSD_CHECK_LOCK_HELD` is unset: this suite runs INSIDE a gate, which
#     exports it, and every assertion below would then be made against runs that
#     skip the lock entirely.
#   * `MCSD_CHECK_LOCK_FILE` points at a temp file, so the suite never touches
#     the host lock -- neither taking it (which would deadlock against the gate
#     running this test) nor waiting on it.
#
# Exit code: 0 = all pass, non-zero = at least one failure.
set -uo pipefail

unset "${!GIT_@}"
unset MCSD_CHECK_LOCK_HELD

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

pass=0
fail=0

ok()        { echo "  PASS: $1"; pass=$((pass + 1)); }
fail_test() { echo "  FAIL: $1"; fail=$((fail + 1)); }

# Poll for a file to appear, up to a generous ceiling. Used instead of a fixed
# sleep wherever the expectation is "this happens", so a slow CI runner costs
# latency rather than a false red. The negative expectation ("this does NOT
# happen yet") has no such formulation and pays a fixed wait below.
await_file() {
	local path=$1 limit=${2:-100} i=0
	while [ "$i" -lt "$limit" ]; do
		[ -e "$path" ] && return 0
		sleep 0.1
		i=$((i + 1))
	done
	return 1
}

echo "=== check_parallel.sh host-lock tests ==="

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

lock_file="$work/mcsd-check.lock"

# The two fake worktrees. check_parallel.sh needs a git checkout to place its
# log dir; nothing below reaches further than the pre-flight make.
holder_wt="$work/holder-worktree"
waiter_wt="$work/waiter-worktree"
mkdir -p "$holder_wt" "$waiter_wt"
git -C "$holder_wt" init -q
git -C "$waiter_wt" init -q

# The stubs live outside the worktrees so a worktree path can only reach a
# waiter's output by way of the lock file, never by way of a shared directory.
stub_dir="$work/stubs"
mkdir -p "$stub_dir"

# Holder stub: announce that the run is past the lock, then hold there until
# released, so the lock is demonstrably held while the other runs are made.
cat > "$stub_dir/make-holder" << 'STUB'
#!/usr/bin/env bash
touch "$LOCK_ACQUIRED"
while [ ! -e "$LOCK_RELEASE" ]; do sleep 0.05; done
exit 1
STUB

# Waiter stub: record that the run got past the lock, then fail immediately.
cat > "$stub_dir/make-waiter" << 'STUB'
#!/usr/bin/env bash
touch "$ENTERED"
exit 1
STUB

chmod +x "$stub_dir/make-holder" "$stub_dir/make-waiter"

# Each run needs its stub named `make` on PATH, and the two stubs differ, so
# give each its own PATH entry holding one file called `make`.
holder_bin="$work/holder-bin"
waiter_bin="$work/waiter-bin"
mkdir -p "$holder_bin" "$waiter_bin"
cp "$stub_dir/make-holder" "$holder_bin/make"
cp "$stub_dir/make-waiter" "$waiter_bin/make"

acquired="$work/holder-acquired"
release="$work/holder-release"

(
	cd "$holder_wt" &&
		PATH="$holder_bin:$PATH" \
			MCSD_CHECK_LOCK_FILE="$lock_file" \
			LOCK_ACQUIRED="$acquired" \
			LOCK_RELEASE="$release" \
			bash "$ROOT/scripts/check_parallel.sh" "$holder_wt"
) > "$work/holder.out" 2>&1 &
holder_pid=$!

if ! await_file "$acquired"; then
	fail_test "the holder run never took the lock (nothing to test against)"
	touch "$release"
	wait "$holder_pid" 2> /dev/null
	echo
	echo "Results: $pass passed, $fail failed"
	exit 1
fi

# ---------------------------------------------------------------------------
# 4. A nested invocation must not block. Asserted first, while the lock is
#    demonstrably held: with the guard set, the run walks straight past it.
{
	nested_entered="$work/nested-entered"
	(
		cd "$waiter_wt" &&
			PATH="$waiter_bin:$PATH" \
				MCSD_CHECK_LOCK_FILE="$lock_file" \
				MCSD_CHECK_LOCK_HELD=1 \
				ENTERED="$nested_entered" \
				bash "$ROOT/scripts/check_parallel.sh" "$waiter_wt"
	) > /dev/null 2>&1

	if [ -e "$nested_entered" ]; then
		ok "a nested run (MCSD_CHECK_LOCK_HELD) does not wait for the lock it already holds"
	else
		fail_test "a nested run blocked on the held lock -- scripts-test would deadlock the gate"
	fi
}

# ---------------------------------------------------------------------------
# 1 + 2. A second run blocks, and says whose worktree is holding the lock.
waiter_entered="$work/waiter-entered"
(
	cd "$waiter_wt" &&
		PATH="$waiter_bin:$PATH" \
			MCSD_CHECK_LOCK_FILE="$lock_file" \
			ENTERED="$waiter_entered" \
			bash "$ROOT/scripts/check_parallel.sh" "$waiter_wt"
) > "$work/waiter.out" 2>&1 &
waiter_pid=$!

# The one place a fixed wait is unavoidable: "has not proceeded" is only
# observable by looking after enough time that it would have.
sleep 2

if [ -e "$waiter_entered" ]; then
	fail_test "a second run started work while the first held the lock"
else
	ok "a second run does not start work while another holds the lock"
fi

if grep -qF "$holder_wt" "$work/waiter.out"; then
	ok "the waiting run names the holder's worktree"
else
	fail_test "the waiting run does not name the holder's worktree (output: $(cat "$work/waiter.out"))"
fi

# ---------------------------------------------------------------------------
# 3. Releasing the lock lets the waiter through.
touch "$release"
wait "$holder_pid" 2> /dev/null

if await_file "$waiter_entered"; then
	ok "the waiting run proceeds once the holder releases the lock"
else
	fail_test "the waiting run never proceeded after the holder released the lock"
fi

wait "$waiter_pid" 2> /dev/null

# ---------------------------------------------------------------------------
echo
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
