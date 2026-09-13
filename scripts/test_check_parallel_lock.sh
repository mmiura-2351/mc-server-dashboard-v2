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
#   5. A waiter whose recorded holder pid is dead is pointed at `fuser`. The
#      lock outlives the pid the holder recorded -- fd 9 is inherited by every
#      process the run spawns -- so an orchestrator killed while its sub-makes
#      are still running holds the lock from processes that
#      `pgrep -f <worktree>` cannot see (they carry a bare argv), and a message
#      naming only the dead pid reads as a stale lock file (#2776).
#   6. A worktree path that mimics the recorded suffix does not fake a dead
#      pid, in either shape. The line is "<worktree> (pid N, since <stamp>)"
#      and the worktree half is arbitrary text: a parse taking the first
#      "(pid ..., since ...)" reads a terminated decoy, and one anchored only
#      at the end of the line is beaten by an unterminated decoy, which has no
#      paren to stop a "not a paren" class before the real suffix. Either way
#      a live holder is reported as gone, sending the reader after holders
#      that do not exist (#2776).
#   7. A run that had to be stopped is reported rather than reaped in silence.
#      The reaps are bounded so a wedged run cannot hang the gate, but a stop
#      that passed quietly would be worse than the hang: stopping the holder
#      releases the lock, so the suite would go on to "pass" assertions about
#      scaffolding it had cut short itself (#2776).
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
# The failure paths are bounded on purpose (#2776). This suite runs inside
# `make check`, so a `wait` on a run still blocked on the lock would hang the
# whole gate instead of failing it, and the holder stub -- which idles until a
# release file appears -- would outlive a killed suite still holding the lock,
# because the EXIT trap has by then removed the directory that file would have
# appeared in.
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

# The same, for a line that has to appear in a run's output rather than a file
# that has to appear on disk.
await_grep() {
	local needle=$1 path=$2 limit=${3:-100} i=0
	while [ "$i" -lt "$limit" ]; do
		grep -qF "$needle" "$path" 2> /dev/null && return 0
		sleep 0.1
		i=$((i + 1))
	done
	return 1
}

# Stop a backgrounded run and reap it. Only the failure paths use this: a `wait`
# on a run still blocked on the lock never returns, which would hang the scripts
# chain rather than report the failure it is standing in for. The run is exec'd
# into its subshell, so the recorded pid is check_parallel.sh itself, and the
# script installs its TERM trap only after the lock section -- a run still
# waiting there dies on the signal. Its `flock` child is swept first because
# signalling only the parent would leave it reparented and running.
#
# Each step is allowed to fail: the run may have no `flock` child left, it may
# have exited between the two signals, and `wait` reports the status of a run
# that exits non-zero by design (its stub fails on purpose). None of that is an
# error here, and leaving the statuses bare aborts the suite under `set -e`.
stop_run() {
	local pid=$1
	[ -n "$pid" ] || return 0
	pkill -TERM -P "$pid" 2> /dev/null || true
	kill -TERM "$pid" 2> /dev/null || true
	wait "$pid" 2> /dev/null || true
	return 0
}

# Reap a run that should be finishing on its own, without waiting forever for
# it. Every run started below can only finish by getting past the lock, so a
# plain `wait` on one that never does turns a red into a hung gate -- this
# suite runs inside `make check`. Bash reaps its own background children as
# they exit, so `kill -0` failing is the signal that the run is done and the
# `wait` returns at once; a run still there at the ceiling is stopped instead.
#
# Returns non-zero when it had to stop the run instead of reaping one that
# finished. Callers must check that: a run stopped part-way has not done what
# the assertion around it claims, and stopping a holder releases the lock as a
# side effect, which would let the assertions after it pass on scaffolding the
# suite itself cut short.
reap_run() {
	local pid=$1 limit=${2:-100} i=0
	[ -n "$pid" ] || return 0
	while [ "$i" -lt "$limit" ]; do
		kill -0 "$pid" 2> /dev/null || { wait "$pid" 2> /dev/null || true; return 0; }
		sleep 0.1
		i=$((i + 1))
	done
	stop_run "$pid"
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
# released, so the lock is demonstrably held while the other runs are made. It
# gives up as well when its work dir disappears: if this suite is killed, the
# EXIT trap removes $work, the release file can then never appear, and a stub
# watching only for that file would sit on the lock forever with nothing left
# alive to release it (#2776).
cat > "$stub_dir/make-holder" << 'STUB'
#!/usr/bin/env bash
touch "$LOCK_ACQUIRED"
while [ ! -e "$LOCK_RELEASE" ]; do
	[ -d "$WORK_DIR" ] || exit 1
	sleep 0.05
done
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
			WORK_DIR="$work" \
			bash "$ROOT/scripts/check_parallel.sh" "$holder_wt"
) > "$work/holder.out" 2>&1 &
holder_pid=$!

if ! await_file "$acquired"; then
	fail_test "the holder run never took the lock (nothing to test against)"
	touch "$release"
	stop_run "$holder_pid"
	echo
	echo "Results: $pass passed, $fail failed"
	exit 1
fi

# ---------------------------------------------------------------------------
# 4. A nested invocation must not block. Asserted first, while the lock is
#    demonstrably held: with the guard set, the run walks straight past it.
#    Backgrounded like every other run here, because the failure this asserts
#    IS a run that blocks on the lock: in the foreground it would hang the gate
#    on the way to its own failure message rather than print it (#2776).
{
	nested_entered="$work/nested-entered"
	(
		cd "$waiter_wt" &&
			PATH="$waiter_bin:$PATH" \
				MCSD_CHECK_LOCK_FILE="$lock_file" \
				MCSD_CHECK_LOCK_HELD=1 \
				ENTERED="$nested_entered" \
				bash "$ROOT/scripts/check_parallel.sh" "$waiter_wt"
	) > /dev/null 2>&1 &
	nested_pid=$!

	if await_file "$nested_entered"; then
		ok "a nested run (MCSD_CHECK_LOCK_HELD) does not wait for the lock it already holds"
		reap_run "$nested_pid" ||
			fail_test "the nested run had to be stopped instead of exiting on its own"
	else
		fail_test "a nested run blocked on the held lock -- scripts-test would deadlock the gate"
		stop_run "$nested_pid"
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

# The holder recorded a pid that is alive, so the dead-pid hint must be absent
# here. Without this half, assertion 5 below is equally satisfied by a script
# that appends the hint unconditionally, which would send every ordinary waiter
# hunting for holders that the recorded pid already accounts for.
if grep -qF 'fuser -v' "$work/waiter.out"; then
	fail_test "the waiting run points at fuser while the recorded holder pid is alive"
else
	ok "a waiter whose recorded holder pid is alive gets no fuser hint"
fi

# ---------------------------------------------------------------------------
# 5. A waiter whose recorded holder pid is dead is pointed at `fuser` (#2776).
#    The holder above still holds the lock; only the line it recorded is
#    doctored to name a pid that is gone, which is the state a killed
#    orchestrator leaves behind -- its sub-makes inherited fd 9 and hold the
#    lock without it. Rewriting the file does not disturb the flock, which
#    lives on the inode, so this stages the message without staging a kill.
#
#    The pid used here is above this host's pid_max, so it cannot be alive. A
#    reaped pid would do the same job only for as long as it stayed unissued:
#    the kernel could hand it to an unrelated process between the check here
#    and the waiter parsing the line, and the assertion would then pass for the
#    wrong reason.
dead_pid=$(( $(cat /proc/sys/kernel/pid_max) + 1 ))

dead_pid_waiter=""
{
	printf '%s (pid %d, since %s)\n' \
		"$holder_wt" "$dead_pid" "$(date '+%Y-%m-%dT%H:%M:%S%z')" > "$lock_file"

	dead_pid_entered="$work/dead-pid-entered"
	(
		cd "$waiter_wt" &&
			PATH="$waiter_bin:$PATH" \
				MCSD_CHECK_LOCK_FILE="$lock_file" \
				ENTERED="$dead_pid_entered" \
				bash "$ROOT/scripts/check_parallel.sh" "$waiter_wt"
	) > "$work/dead-pid-waiter.out" 2>&1 &
	dead_pid_waiter=$!

	if await_grep "fuser -v $lock_file" "$work/dead-pid-waiter.out"; then
		ok "a waiter whose recorded holder pid is dead is pointed at fuser"
	else
		fail_test "a waiter whose recorded holder pid is dead prints no fuser hint (output: $(cat "$work/dead-pid-waiter.out"))"
	fi
}

# ---------------------------------------------------------------------------
# 6. A worktree path that mimics the recorded suffix does not fake a dead pid
#    (#2776). Staged like assertion 5 -- the holder still holds the lock, only
#    the line it recorded is doctored -- except that here the decoy sits in the
#    worktree half and the real suffix names this suite, which is alive.
#
#    Both shapes are asserted because they defeat different parses. A parse
#    that takes the first "(pid ..., since ...)" it sees reads the TERMINATED
#    decoy; a parse anchored only at the end of the line is beaten by the
#    UNTERMINATED one, whose missing paren lets a "not a paren" class run from
#    the decoy straight through the real suffix to the closing paren at the
#    end. Either way a live holder is reported as gone.
decoy_waiters=()
{
	decoy_n=0
	for decoy_tail in "since decoy): 42" "since decoy: 42"; do
		decoy_n=$((decoy_n + 1))
		decoy_wt="$holder_wt (pid $dead_pid, $decoy_tail"
		printf '%s (pid %d, since %s)\n' \
			"$decoy_wt" "$$" "$(date '+%Y-%m-%dT%H:%M:%S%z')" > "$lock_file"

		decoy_out="$work/decoy-waiter-$decoy_n.out"
		(
			cd "$waiter_wt" &&
				PATH="$waiter_bin:$PATH" \
					MCSD_CHECK_LOCK_FILE="$lock_file" \
					ENTERED="$work/decoy-entered-$decoy_n" \
					bash "$ROOT/scripts/check_parallel.sh" "$waiter_wt"
		) > "$decoy_out" 2>&1 &
		decoy_waiters+=($!)

		if await_grep "held by: $decoy_wt" "$decoy_out"; then
			if grep -qF 'fuser -v' "$decoy_out"; then
				fail_test "a decoy pid in the worktree path faked a dead holder [$decoy_tail]"
			else
				ok "a decoy pid in the worktree path does not fake a dead holder [$decoy_tail]"
			fi
		else
			fail_test "the decoy waiter never named the holder [$decoy_tail] (output: $(cat "$decoy_out"))"
		fi
	done
}

# ---------------------------------------------------------------------------
# 3. Releasing the lock lets the waiter through.
touch "$release"
reap_run "$holder_pid" ||
	fail_test "the holder run had to be stopped: it did not exit after the lock was released, so the lock below was freed by this suite rather than by the holder"

if await_file "$waiter_entered"; then
	ok "the waiting run proceeds once the holder releases the lock"
	# Bounded for the same reason as the failure branch below: each of these
	# runs can only finish by getting past the lock.
	reap_run "$waiter_pid" ||
		fail_test "the waiting run had to be stopped instead of exiting on its own"
	reap_run "$dead_pid_waiter" ||
		fail_test "the dead-pid waiter had to be stopped instead of exiting on its own"
	for decoy_waiter in "${decoy_waiters[@]}"; do
		reap_run "$decoy_waiter" ||
			fail_test "a decoy waiter had to be stopped instead of exiting on its own"
	done
else
	fail_test "the waiting run never proceeded after the holder released the lock"
	# Whatever went wrong, the waiters are still blocked on a lock they are now
	# never going to get: reporting this failure must not cost the gate a hang.
	stop_run "$waiter_pid"
	stop_run "$dead_pid_waiter"
	for decoy_waiter in "${decoy_waiters[@]}"; do
		stop_run "$decoy_waiter"
	done
fi

# ---------------------------------------------------------------------------
# 7. A run that had to be stopped is reported, not reaped in silence (#2776).
#    This is the contract every reap above relies on. A silent stop would let
#    the suite cut its own scaffolding short and still report green -- and
#    because stopping the holder releases the lock as a side effect, every
#    assertion after it would pass on a lock this suite freed itself.
{
	sleep 30 &
	stuck_pid=$!

	if reap_run "$stuck_pid" 3; then
		fail_test "reap_run reported success for a run it had to stop"
	else
		ok "a run that had to be stopped is reported rather than reaped in silence"
	fi
}

# ---------------------------------------------------------------------------
echo
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
