#!/usr/bin/env bash
#
# test_check_parallel_logs.sh: a gate run keeps the previous run's logs, hands
# its log dir to the api test run (issue #3750), and runs the argon2 canary
# into it beside the api chain (issue #3748).
#
# The defect. Each run started with `rm -rf` of its log dir, so re-running a
# red gate -- the documented response to a flake -- destroyed the failing run's
# logs before anyone had read them. The gate now moves the last run's logs to
# `check-logs.prev` (one generation) before it starts a fresh `check-logs`.
#
# What is asserted:
#
#   1. Three runs leave the third run's logs in check-logs and the second
#      run's in check-logs.prev, with nothing left of the first run's: the
#      rotation keeps exactly one generation and replaces an older one rather
#      than nesting into it.
#   2. The orchestrator exports MCSD_CHECK_LOGDIR naming that log dir to the
#      targets it runs, and `make api-test` turns it into the pytest option that
#      writes the per-worker run order there. Either half can regress silently
#      -- the gate stays green without the record.
#   3. A rotation step that fails stops the run with a message before any
#      target runs. Otherwise the run would go on in the old check-logs and
#      append to the previous run's chain logs, or move check-logs into the
#      undeletable check-logs.prev as check-logs.prev/check-logs.
#   4. A run that finds check-logs.prev but no check-logs -- the previous run
#      was killed between moving its predecessor's logs aside and creating its
#      own -- keeps check-logs.prev: there is nothing newer to replace it with.
#   5. The argon2 canary runs while the api chain runs, from the api venv's
#      interpreter with -I at nice 19, into argon2-canary.log, without the
#      gate lock's descriptor; and it is stopped, with nothing of it left
#      behind, whether the run passes, the api chain fails, or the run is
#      sent SIGTERM. A canary left running would load the host for the gate
#      after it; one holding the lock would block it.
#
# The runs use the real script with a stub `make` on PATH, as
# test_check_parallel_identity.sh does: the stub writes its run's marker and the
# exported variable into the pre-flight golangci-lint install log -- the first
# thing the script runs -- and then fails, so no chain is ever spawned. The
# canary runs of 5 use a second stub `make` that passes every target, and a stub
# api venv interpreter that stands in for the canary and records what it was
# given; neither runs Python, so this suite still needs no dependency. `GIT_*`
# is unset and the fake worktree has its own repository, so a run never rotates
# the live gate's logs when this test runs inside `make check`; the lock file is
# a temp one for the same reason as in that test.
#
# Exit code: 0 = all pass, non-zero = at least one failure.
set -uo pipefail

unset "${!GIT_@}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

pass=0
fail=0

ok()        { echo "  PASS: $1"; pass=$((pass + 1)); }
fail_test() { echo "  FAIL: $1"; fail=$((fail + 1)); }

echo "=== check_parallel.sh log-dir tests ==="

fake_worktree="$(mktemp -d)"
stub_dir="$(mktemp -d)"
trap 'rm -rf "$fake_worktree" "$stub_dir"' EXIT

git -C "$fake_worktree" init -q
logdir="$fake_worktree/.git/check-logs"

cat > "$stub_dir/make" << 'STUB'
#!/usr/bin/env bash
# Stub `make`: report this run's marker and the exported log dir, then fail so
# the run stops before Phase 1a spawns anything.
echo "marker=$RUN_MARKER"
echo "logdir=${MCSD_CHECK_LOGDIR:-<unset>}"
exit 1
STUB
chmod +x "$stub_dir/make"

run_gate() {
	(
		cd "$fake_worktree" &&
			PATH="$stub_dir:$PATH" \
				MCSD_CHECK_LOCK_FILE="$stub_dir/lock" \
				RUN_MARKER="$1" \
				bash "$ROOT/scripts/check_parallel.sh" "$fake_worktree"
	) > "$stub_dir/out" 2>&1
}

# marker_in <dir>: the marker the run that wrote <dir> recorded, or <none>.
marker_in() {
	local line
	line="$(grep -m 1 '^marker=' "$1/golangci-install.log" 2>/dev/null)" || line="marker=<none>"
	printf '%s' "${line#marker=}"
}

# ---------------------------------------------------------------------------
# 1. One generation of previous logs survives a re-run.
{
	run_gate first
	run_gate second
	run_gate third

	current="$(marker_in "$logdir")"
	previous="$(marker_in "$logdir.prev")"
	if [ "$current" = third ] && [ "$previous" = second ]; then
		ok "a re-run keeps the previous run's logs in check-logs.prev"
	else
		fail_test "after three runs check-logs holds '$current' and check-logs.prev '$previous' (want third, second)"
	fi

	if [ -e "$logdir.prev/check-logs" ] || [ -e "$logdir.prev.prev" ]; then
		fail_test "the rotation nested or kept a second generation: $(ls -a "$logdir.prev")"
	else
		ok "the rotation replaces the older generation rather than nesting into it"
	fi
}

# ---------------------------------------------------------------------------
# 2. The log dir reaches the api test run.
{
	exported="$(grep -m 1 '^logdir=' "$logdir/golangci-install.log" 2>/dev/null)" || exported="<no line>"
	if [ "$exported" = "logdir=$logdir" ]; then
		ok "the orchestrator exports MCSD_CHECK_LOGDIR naming its log dir"
	else
		fail_test "the targets see '$exported' (want logdir=$logdir)"
	fi

	recipe="$(cd "$ROOT" && MCSD_CHECK_LOGDIR=/the/log/dir make -n api-test 2>&1)"
	want='--run-order-log="/the/log/dir/api-run-order.tsv"'
	case "$recipe" in
		*"$want"*) ok "make api-test writes the run order into MCSD_CHECK_LOGDIR" ;;
		*) fail_test "make api-test does not pass $want (recipe: $recipe)" ;;
	esac

	recipe="$(cd "$ROOT" && env -u MCSD_CHECK_LOGDIR make -n api-test 2>&1)"
	case "$recipe" in
		*--run-order-log*) fail_test "make api-test outside the gate writes a run order: $recipe" ;;
		*) ok "make api-test outside the gate writes no run order" ;;
	esac
}

# ---------------------------------------------------------------------------
# 3. A failed rotation step stops the run. check-logs.prev holds a read-only
#    dir with a file in it, so it cannot be removed but would still take a
#    move into it. Needs a non-root user: root can delete from a read-only dir.
if [ "$(id -u)" -eq 0 ]; then
	echo "  SKIP: the failed-rotation case needs a non-root user"
else
	mkdir "$logdir.prev/stuck" && touch "$logdir.prev/stuck/file" && chmod a-w "$logdir.prev/stuck"
	if run_gate fourth; then
		fail_test "a run whose check-logs.prev cannot be removed did not fail"
	else
		ok "a run whose check-logs.prev cannot be removed fails"
	fi
	chmod u+w "$logdir.prev/stuck"

	out="$(cat "$stub_dir/out")"
	case "$out" in
		*"cannot rotate"*) ok "the failure names the rotation" ;;
		*) fail_test "the failure does not name the rotation: $out" ;;
	esac

	current="$(marker_in "$logdir")"
	if [ "$current" = third ] && [ ! -e "$logdir.prev/check-logs" ]; then
		ok "no target runs after a failed rotation, and nothing nests"
	else
		fail_test "after a failed rotation check-logs holds '$current' (want third); nested: $(ls "$logdir.prev")"
	fi
fi

# ---------------------------------------------------------------------------
# 4. A run killed between the move and the mkdir loses nothing on the next run.
{
	rm -rf "$logdir.prev"
	mv "$logdir" "$logdir.prev"
	run_gate fifth

	current="$(marker_in "$logdir")"
	previous="$(marker_in "$logdir.prev")"
	if [ "$current" = fifth ] && [ "$previous" = third ]; then
		ok "a run with no check-logs keeps check-logs.prev"
	else
		fail_test "check-logs holds '$current' and check-logs.prev '$previous' (want fifth, third)"
	fi
}

# ---------------------------------------------------------------------------
# 5. The argon2 canary runs beside the api chain, and stops on every way out.
canary_worktree="$(mktemp -d)"
canary_stub="$(mktemp -d)"
trap 'rm -rf "$fake_worktree" "$stub_dir" "$canary_worktree" "$canary_stub"' EXIT
git -C "$canary_worktree" init -q
canary_logdir="$canary_worktree/.git/check-logs"

# The stand-in canary: it records its pid, niceness, arguments and whether it
# holds fd 9 -- the gate lock's descriptor -- then idles until a SIGTERM, which
# it takes a moment over, as the real one does to write its last line: a run
# that did not wait for it would end with the canary still there. The trap and
# the output come before the record, and the record appears whole, so a run
# that stops the canary as soon as it reads the record finds both in place.
mkdir -p "$canary_worktree/api/.venv/bin"
cat > "$canary_worktree/api/.venv/bin/python" << 'STUB'
#!/usr/bin/env bash
trap 'sleep 0.3; echo TERM > "$CANARY_RECORD.stopped"; exit 0' TERM
echo "stand-in canary output"
fd9=closed
[ -e "/proc/$$/fd/9" ] && fd9=open
echo "pid=$$ nice=$(nice) fd9=$fd9 args=$*" > "$CANARY_RECORD.started.tmp"
mv "$CANARY_RECORD.started.tmp" "$CANARY_RECORD.started"
while :; do sleep 0.1; done
STUB
chmod +x "$canary_worktree/api/.venv/bin/python"

# Every target passes at once, except the api chain's: each waits (bounded) for
# the canary to have started, so the start never races the chain's end;
# api-test notes whether the canary is running, and then waits for a release
# file when the run is to be stopped from outside. CANARY_FAIL_API fails
# api-lint. proto-check, the first target after every chain has ended, notes
# whether the canary is still running.
cat > "$canary_stub/make" << 'STUB'
#!/usr/bin/env bash
canary_running() {
	local pid
	pid="$(sed -n 's/^pid=\([0-9]*\) .*/\1/p' "$CANARY_RECORD.started" 2> /dev/null)"
	[ -n "$pid" ] && kill -0 "$pid" 2> /dev/null
}
case "$1" in
api-lint | api-test)
	for _ in $(seq 1 100); do
		[ -e "$CANARY_RECORD.started" ] && break
		sleep 0.05
	done
	;;
esac
case "$1" in
api-lint)
	[ -z "${CANARY_FAIL_API:-}" ] || exit 1
	;;
api-test)
	canary_running && echo running > "$CANARY_RECORD.during-api-test"
	if [ -n "${CANARY_HOLD_API:-}" ]; then
		for _ in $(seq 1 200); do
			[ -e "$CANARY_RECORD.release" ] && break
			sleep 0.05
		done
	fi
	;;
proto-check)
	canary_running && echo running > "$CANARY_RECORD.after-the-chains"
	;;
esac
exit 0
STUB
chmod +x "$canary_stub/make"

# canary_run <name> [VAR=value ...]: one gate run in the canary worktree, in the
# background, with its records under $canary_stub/<name>; sets gate_pid.
canary_run() {
	local name=$1
	shift
	(
		cd "$canary_worktree" &&
			exec env PATH="$canary_stub:$PATH" \
				MCSD_CHECK_LOCK_FILE="$canary_stub/lock" \
				CANARY_RECORD="$canary_stub/$name" "$@" \
				bash "$ROOT/scripts/check_parallel.sh" "$canary_worktree"
	) > "$canary_stub/$name.out" 2>&1 &
	gate_pid=$!
}

# await_gate: reap the run into gate_rc, or fail and stop it after ~20 s: a
# `wait` on a run that never ends would hang the gate this suite runs in.
await_gate() {
	local i
	for i in $(seq 1 200); do
		kill -0 "$gate_pid" 2> /dev/null || break
		sleep 0.1
	done
	if kill -0 "$gate_pid" 2> /dev/null; then
		kill -KILL "$gate_pid" 2> /dev/null
		fail_test "the $1 run did not end within 20 s: $(cat "$canary_stub/$1.out")"
	fi
	wait "$gate_pid" 2> /dev/null
	gate_rc=$?
}

# check_canary <name>: the canary of run <name> started as the gate starts it,
# was stopped with a SIGTERM, and is gone.
check_canary() {
	local name=$1 started pid
	started="$(cat "$canary_stub/$name.started" 2> /dev/null)" || started="<never started>"
	case "$started" in
		*" nice=19 fd9=closed args=-I scripts/argon2_canary.py") ok "$name: the canary ran -I at nice 19 without the lock's fd" ;;
		*) fail_test "$name: the canary started as '$started'" ;;
	esac
	if [ -e "$canary_stub/$name.stopped" ]; then
		ok "$name: the canary was stopped with SIGTERM"
	else
		fail_test "$name: the canary was never sent SIGTERM"
	fi
	pid="${started#pid=}"
	pid="${pid%% *}"
	if [ -n "$pid" ] && kill -0 "$pid" 2> /dev/null; then
		kill -KILL "$pid" 2> /dev/null
		fail_test "$name: the canary (pid $pid) outlived the run"
	else
		ok "$name: nothing of the canary is left"
	fi
}

{
	canary_run passing
	await_gate passing
	check_canary passing
	# Stopping the canary on the way out leaves the run's verdict alone.
	if [ "$gate_rc" -eq 0 ]; then
		ok "a passing run still exits 0"
	else
		fail_test "a passing run exited $gate_rc: $(cat "$canary_stub/passing.out")"
	fi
	if [ -e "$canary_stub/passing.during-api-test" ]; then
		ok "the canary runs while the api tests do"
	else
		fail_test "the canary was not running during api-test: $(cat "$canary_stub/passing.out")"
	fi
	if [ -e "$canary_stub/passing.after-the-chains" ]; then
		fail_test "the canary was still running after the api chain had ended"
	else
		ok "the canary stops as the api chain ends"
	fi
	if grep -qxF 'stand-in canary output' "$canary_logdir/argon2-canary.log" 2> /dev/null; then
		ok "the canary writes into argon2-canary.log in the run's log dir"
	else
		fail_test "argon2-canary.log does not hold the canary's output"
	fi

	canary_run failing CANARY_FAIL_API=1
	await_gate failing
	check_canary failing
	if [ "$gate_rc" -ne 0 ]; then
		ok "a run whose api chain fails still fails"
	else
		fail_test "a run whose api chain failed exited 0"
	fi

	canary_run stopped CANARY_HOLD_API=1
	for _ in $(seq 1 200); do
		[ -e "$canary_stub/stopped.during-api-test" ] && break
		sleep 0.05
	done
	kill -TERM "$gate_pid" 2> /dev/null
	await_gate stopped
	check_canary stopped
	touch "$canary_stub/stopped.release"
}

# ---------------------------------------------------------------------------
echo
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
