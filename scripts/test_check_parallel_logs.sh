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
#   3. A rotation step that fails stops the run, non-zero and with a message,
#      before any target runs. Otherwise the run would go on in the old
#      check-logs and append to the previous run's chain logs, or move
#      check-logs into the undeletable check-logs.prev as
#      check-logs.prev/check-logs.
#   4. A run that finds check-logs.prev but no check-logs -- the previous run
#      was killed between moving its predecessor's logs aside and creating its
#      own -- keeps check-logs.prev: there is nothing newer to replace it with.
#   5. The argon2 canary runs while the api chain runs, from the api venv's
#      interpreter with -I at nice 19, told the gate's pid, into
#      argon2-canary.log, without the gate lock's descriptor; and it is
#      stopped, with nothing of it left behind, whether the run passes, the api
#      chain fails, the run is sent SIGTERM, or the canary ignores its SIGTERM.
#      A canary left running would load the host for the gate after it; one
#      holding the lock would block it. A canary that logged a MISMATCH or an
#      ERROR is announced on stderr and its log is kept where neither the
#      rotation nor a worktree removal reaches -- the git common dir, which
#      only a linked worktree tells apart from its own git dir -- even when the
#      run passes, and a clean one is neither. A canary that never got a check
#      done is announced too: a clean log from it would read as a clean run.
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
# the run stops before Phase 1a spawns anything. STUB_STATUS=0 passes every
# target instead, so that only the script itself can fail the run.
echo "marker=$RUN_MARKER"
echo "logdir=${MCSD_CHECK_LOGDIR:-<unset>}"
exit "${STUB_STATUS:-1}"
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
#    The stub passes every target here, so a run that went on past the failed
#    rotation would pass: only the rotation can fail it (#3774).
if [ "$(id -u)" -eq 0 ]; then
	echo "  SKIP: the failed-rotation case needs a non-root user"
else
	mkdir "$logdir.prev/stuck" && touch "$logdir.prev/stuck/file" && chmod a-w "$logdir.prev/stuck"
	if STUB_STATUS=0 run_gate fourth; then
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
linked_primary="$(mktemp -d)"
trap 'rm -rf "$fake_worktree" "$stub_dir" "$canary_worktree" "$canary_stub" "$linked_primary"' EXIT
git -C "$canary_worktree" init -q
canary_logdir="$canary_worktree/.git/check-logs"
hits_dir="$canary_worktree/.git/argon2-canary-hits"

# The ceiling of every wait in these runs for something a correct run does --
# its canary starting, the run reaching api-test, the run ending -- in polls of
# 0.1 s: 120 s, as in test_check_parallel_lock.sh (#3788), the bound the Worker
# tests give a wait on filesystem work (#3637). It only keeps a run that never
# does it from hanging the gate this suite runs in, so it must never be what a
# slow run loses to (#3833): every step of a run execs from the disk and writes
# to the suite's temp dir, and under IO load each can take seconds. No wait here
# asserts how soon. A passing run pays none of it, and a wait stops once its
# run has ended, so a red pays it only for a run that is still there and never
# does what is awaited. The stub's waits are part of a run, so they get half
# the ceiling (stub_ceiling): one that expires does so while the suite still
# waits for its run, which then ends and reports it. Nothing ends the stub's
# wait for the canary to start early when the canary never starts, since its
# run is waiting on it. That red would pay the wait in every run, so it pays it
# once: the stub records the expiry in $canary_expired and skips the wait from
# then on, and the record fails the suite.
ceiling=1200
stub_ceiling=$((ceiling / 2))
canary_expired="$canary_stub/canary-start-expired"

# The stand-in canary: it records its pid, its parent, niceness, arguments and
# whether it holds fd 9 -- the gate lock's descriptor -- then idles until a
# SIGTERM, or until its gate is gone: both are described below.
# Its log has the real one's start and stopped lines. The trap and the output
# come before the record, and the record appears whole, so a run that stops the
# canary as soon as it reads the record finds both in place. CANARY_HIT adds a
# MISMATCH line; CANARY_IGNORE_TERM makes it ignore SIGTERM; CANARY_CHECKS sets
# the checks its stopped line counts (1); CANARY_USAGE_ERROR makes it fail at
# once, as on a bad command line, and with no TERM trap. The real one blocks
# TERM before it parses its arguments, so on a usage error it exits 2 with the
# TERM pending and the error last in its log. The stand-in cannot block it, so a
# TERM between its record and its exit kills it instead (143, a status the gate
# ignores), with the error still last.
#
# Its TERM trap writes the stopped line and .stopped, then exits, and starts no
# process on the way (#3858). The gate kills a canary 5 s after its SIGTERM, and
# at nice 19 on a loaded host each process the stand-in starts waits for the
# CPU, so a trap that started one could lose its writes to that bound on a
# correct gate. Before the writes there is now only the rest of one idle
# `sleep 0.1`, which bash finishes before it runs the trap.
#
# Like the real one, it stops once the pid it was given as --parent is no
# longer its parent, so a gate that dies without stopping it -- one killed at
# the ceiling, say -- leaves nothing idling. A stand-in that finds its gate gone,
# idle or in its trap, has outlived its run: it records that in .orphaned, which
# fails the run's check that nothing of the canary is left. That record, not a
# `kill -0` racing the stand-in's own exit, is what catches a gate that ends
# without stopping its canary. The trap used to pause 0.3 s before its writes,
# which also left a canary running past a gate that sent the SIGTERM but did not
# wait for it. Without the pause such a gate is caught only when it reads the
# log, or ends, before the stand-in's trap has run: a race, decided by the rest
# of that idle `sleep 0.1`.
mkdir -p "$canary_worktree/api/.venv/bin"
cat > "$canary_worktree/api/.venv/bin/python" << 'STUB'
#!/usr/bin/env bash
# gate_gone: the real canary's parent check -- the pid it was given as --parent
# is no longer its parent -- read from /proc without forking. Its comm is bash,
# so the fourth field of its stat is its parent's pid.
parent=$4
gate_gone() {
	local ppid
	read -r _ _ _ ppid _ < "/proc/$$/stat"
	[ "$ppid" != "$parent" ]
}
if [ -n "${CANARY_IGNORE_TERM:-}" ]; then
	trap '' TERM
elif [ -z "${CANARY_USAGE_ERROR:-}" ]; then
	trap 'echo "2026-11-17T00:00:01.000+00:00 stopped by SIGTERM: checks=${CANARY_CHECKS:-1} mismatches=0 errors=0 cpu_s=0.1 wall_s=1.0"
		echo TERM > "$CANARY_RECORD.stopped"
		gate_gone && echo TERM > "$CANARY_RECORD.orphaned"
		exit 0' TERM
fi
if [ -n "${CANARY_USAGE_ERROR:-}" ]; then
	echo "argon2_canary.py: error: the following arguments are required: --parent"
else
	echo "2026-11-17T00:00:00.000+00:00 start pid=$$ interval_s=2.0 summary_every=30"
fi
[ -z "${CANARY_HIT:-}" ] ||
	echo "2026-11-17T00:00:00.500+00:00 MISMATCH check=1 kind=p4 tag=00 expected=3a"
fd9=closed
[ -e "/proc/$$/fd/9" ] && fd9=open
echo "pid=$$ ppid=$PPID nice=$(nice) fd9=$fd9 args=$*" > "$CANARY_RECORD.started.tmp"
mv "$CANARY_RECORD.started.tmp" "$CANARY_RECORD.started"
[ -z "${CANARY_USAGE_ERROR:-}" ] || exit 2
while ! gate_gone; do sleep 0.1; done
echo idle > "$CANARY_RECORD.orphaned"
STUB
chmod +x "$canary_worktree/api/.venv/bin/python"

# Every target passes at once, except the api chain's: each waits for the
# canary to have started, so the start never races the chain's end; api-test
# notes whether the canary is running, and if it is, holds until a release file
# appears when the run is to be stopped from outside. CANARY_FAIL_API fails
# api-lint. proto-check, the first target after every chain has ended, notes
# whether the canary is still running.
#
# Both waits stand in for a real api chain, which outlasts the canary's start
# and is still running when the run is stopped. A correct gate never lets either
# reach its bound, so both take stub_ceiling (CANARY_CEILING), and both stop
# once the gate (CANARY_GATE) is gone: the stub then has no run to hold, and it
# has the gate lock's fd 9 open, so a stub that outlived its gate would hold the
# lock.
cat > "$canary_stub/make" << 'STUB'
#!/usr/bin/env bash
canary_running() {
	local pid
	pid="$(sed -n 's/^pid=\([0-9]*\) .*/\1/p' "$CANARY_RECORD.started" 2> /dev/null)"
	[ -n "$pid" ] && kill -0 "$pid" 2> /dev/null
}
# hold <command...>: poll until the command succeeds; fail once the gate is gone
# or at the ceiling.
hold() {
	local i=0
	while [ "$i" -lt "$CANARY_CEILING" ]; do
		"$@" && return 0
		kill -0 "$CANARY_GATE" 2> /dev/null || return 1
		sleep 0.1
		i=$((i + 1))
	done
	return 1
}
case "$1" in
api-lint | api-test)
	if [ ! -e "$CANARY_EXPIRED" ] && ! hold test -e "$CANARY_RECORD.started" &&
		kill -0 "$CANARY_GATE" 2> /dev/null; then
		echo "${CANARY_RECORD##*/} $1" >> "$CANARY_EXPIRED"
	fi
	;;
esac
case "$1" in
api-lint)
	[ -z "${CANARY_FAIL_API:-}" ] || exit 1
	;;
api-test)
	if canary_running; then
		echo running > "$CANARY_RECORD.during-api-test"
		[ -z "${CANARY_HOLD_API:-}" ] || hold test -e "$CANARY_RECORD.release"
	fi
	;;
proto-check)
	canary_running && echo running > "$CANARY_RECORD.after-the-chains"
	;;
esac
exit 0
STUB
chmod +x "$canary_stub/make"

# canary_run <name> [VAR=value ...]: one gate run in $canary_dir (the canary
# worktree unless a run says otherwise), in the background, with its records
# under $canary_stub/<name>; sets gate_pid. The subshell execs into the gate,
# so its $BASHPID is the gate's pid, which the stub is given as CANARY_GATE.
canary_dir="$canary_worktree"
canary_run() {
	local name=$1
	shift
	(
		cd "$canary_dir" &&
			exec env PATH="$canary_stub:$PATH" \
				MCSD_CHECK_LOCK_FILE="$canary_stub/lock" \
				CANARY_GATE="$BASHPID" CANARY_CEILING="$stub_ceiling" \
				CANARY_EXPIRED="$canary_expired" \
				CANARY_RECORD="$canary_stub/$name" "$@" \
				bash "$ROOT/scripts/check_parallel.sh" "$canary_dir"
	) > "$canary_stub/$name.out" 2>&1 &
	gate_pid=$!
}

# await_gate: reap the run into gate_rc, or fail and stop it at the ceiling: a
# `wait` on a run that never ends would hang the gate this suite runs in.
await_gate() {
	local i
	for i in $(seq 1 "$ceiling"); do
		kill -0 "$gate_pid" 2> /dev/null || break
		sleep 0.1
	done
	if kill -0 "$gate_pid" 2> /dev/null; then
		kill -KILL "$gate_pid" 2> /dev/null
		fail_test "the $1 run did not end within $((ceiling / 10)) s: $(cat "$canary_stub/$1.out")"
	fi
	wait "$gate_pid" 2> /dev/null
	gate_rc=$?
}

# check_canary <name>: the canary of run <name> (the last one started) started
# as the gate starts it -- the gate's own child, told the gate's pid -- was
# stopped with a SIGTERM, and is gone without having outlived its gate. The pid
# is tested before the record: a stand-in already gone has written any record
# it was going to.
check_canary() {
	local name=$1 started pid
	started="$(cat "$canary_stub/$name.started" 2> /dev/null)" || started="<never started>"
	case "$started" in
		*" ppid=$gate_pid nice=19 fd9=closed args=-I scripts/argon2_canary.py --parent $gate_pid") ok "$name: the canary ran -I at nice 19 as the gate's child, told its pid, without the lock's fd" ;;
		*) fail_test "$name: the canary started as '$started' (gate pid $gate_pid)" ;;
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
	elif [ -e "$canary_stub/$name.orphaned" ]; then
		fail_test "$name: the canary outlived the run: it found its gate gone ($(cat "$canary_stub/$name.orphaned"))"
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
	if grep -q ' start pid=' "$canary_logdir/argon2-canary.log" 2> /dev/null; then
		ok "the canary writes into argon2-canary.log in the run's log dir"
	else
		fail_test "argon2-canary.log does not hold the canary's output"
	fi
	if grep -q 'argon2 canary' "$canary_stub/passing.out" || [ -e "$hits_dir" ]; then
		fail_test "a clean canary was announced or kept: $(cat "$canary_stub/passing.out")"
	else
		ok "a clean canary is neither announced nor kept"
	fi

	canary_run failing CANARY_FAIL_API=1
	await_gate failing
	check_canary failing
	if [ "$gate_rc" -ne 0 ]; then
		ok "a run whose api chain fails still fails"
	else
		fail_test "a run whose api chain failed exited 0"
	fi

	# The SIGTERM goes to a run in api-test with its canary running. A run that
	# ends, or is still there at the ceiling, without getting there is reported,
	# and is still sent the SIGTERM, which stops one that is still there.
	canary_run stopped CANARY_HOLD_API=1
	for _ in $(seq 1 "$ceiling"); do
		[ -e "$canary_stub/stopped.during-api-test" ] && break
		kill -0 "$gate_pid" 2> /dev/null || break
		sleep 0.1
	done
	[ -e "$canary_stub/stopped.during-api-test" ] ||
		fail_test "stopped: the run never reached api-test with the canary running: $(cat "$canary_stub/stopped.out")"
	kill -TERM "$gate_pid" 2> /dev/null
	await_gate stopped
	# The gate's TERM trap exits 1. A run that exits 0 was not ended by the
	# SIGTERM: the stub's release hold ran out and the run passed on its own.
	if [ "$gate_rc" -ne 0 ]; then
		ok "stopped: the SIGTERM ends the run, and the run fails"
	else
		fail_test "stopped: the run exited 0 after its SIGTERM, so the SIGTERM did not end it: $(cat "$canary_stub/stopped.out")"
	fi
	check_canary stopped
	touch "$canary_stub/stopped.release"

	# A hit in a passing run: the run still passes, says so, and keeps the log.
	canary_run hit CANARY_HIT=1
	await_gate hit
	check_canary hit
	if [ "$gate_rc" -eq 0 ]; then
		ok "a canary hit does not fail the run"
	else
		fail_test "a run whose canary hit exited $gate_rc"
	fi
	if grep -q '^!!! argon2 canary: a MISMATCH or ERROR' "$canary_stub/hit.out" &&
		grep -q ' MISMATCH check=1 kind=p4 ' "$canary_stub/hit.out"; then
		ok "a canary hit is announced with its lines"
	else
		fail_test "a canary hit was not announced: $(cat "$canary_stub/hit.out")"
	fi
	kept=("$hits_dir"/*-"$(basename "$canary_worktree")".log)
	if [ "${#kept[@]}" -eq 1 ] && [ -f "${kept[0]}" ] &&
		grep -q ' MISMATCH check=1 kind=p4 ' "${kept[0]}" &&
		grep -qF "${kept[0]}" "$canary_stub/hit.out"; then
		ok "a canary hit's log is kept outside the rotation, and the run names it"
	else
		fail_test "a canary hit's log was not kept: $(ls -a "$hits_dir" 2>&1)"
	fi

	# A canary that ignores its SIGTERM costs the run a bounded wait, no more.
	canary_run deaf CANARY_IGNORE_TERM=1
	await_gate deaf
	started="$(cat "$canary_stub/deaf.started" 2> /dev/null)" || started=""
	pid="${started#pid=}"
	pid="${pid%% *}"
	if [ -n "$pid" ] && ! kill -0 "$pid" 2> /dev/null &&
		[ ! -e "$canary_stub/deaf.orphaned" ] && [ "$gate_rc" -eq 0 ]; then
		ok "a canary that ignores SIGTERM is killed, and the run still passes"
	else
		[ -z "$pid" ] || kill -KILL "$pid" 2> /dev/null
		fail_test "a canary that ignores SIGTERM: pid '$pid', run exit $gate_rc, found its gate gone: $(cat "$canary_stub/deaf.orphaned" 2> /dev/null || echo no)"
	fi

	# A canary that got no check done says so, and the run's verdict stands:
	# one killed at the bound, one stopped at checks=0, one that never started.
	if grep -q '^argon2 canary did not run: .* start pid=' "$canary_stub/deaf.out"; then
		ok "a canary killed at the bound is announced as not run"
	else
		fail_test "a canary killed at the bound was not announced: $(cat "$canary_stub/deaf.out")"
	fi
	canary_run zero CANARY_CHECKS=0
	await_gate zero
	if [ "$gate_rc" -eq 0 ] &&
		grep -q '^argon2 canary did not run: .* stopped by SIGTERM: checks=0 ' "$canary_stub/zero.out"; then
		ok "a canary stopped at checks=0 is announced as not run, and the run passes"
	else
		fail_test "a canary stopped at checks=0 (run exit $gate_rc): $(cat "$canary_stub/zero.out")"
	fi
	canary_run usage CANARY_USAGE_ERROR=1
	await_gate usage
	if [ "$gate_rc" -eq 0 ] &&
		grep -q '^argon2 canary did not run: argon2_canary.py: error: ' "$canary_stub/usage.out"; then
		ok "a canary that never started is announced as not run, and the run passes"
	else
		fail_test "a canary that never started (run exit $gate_rc): $(cat "$canary_stub/usage.out")"
	fi

	# From a linked worktree, a hit's log goes to the primary checkout's git
	# dir, which the worktree's removal leaves alone; its own git dir does not.
	git -C "$linked_primary" init -q
	git -C "$linked_primary" -c user.name=test -c user.email=test@example.com \
		commit -q --allow-empty -m init
	canary_dir="$canary_stub/linked"
	git -C "$linked_primary" worktree add -q --detach "$canary_dir"
	mkdir -p "$canary_dir/api/.venv/bin"
	cp "$canary_worktree/api/.venv/bin/python" "$canary_dir/api/.venv/bin/python"
	canary_run linked CANARY_HIT=1
	await_gate linked
	git -C "$linked_primary" worktree remove --force "$canary_dir"
	canary_dir="$canary_worktree"
	kept=("$linked_primary"/.git/argon2-canary-hits/*-linked.log)
	if [ -f "${kept[0]}" ] && grep -q ' MISMATCH check=1 kind=p4 ' "${kept[0]}"; then
		ok "a hit from a linked worktree is kept in the primary's git dir, past its removal"
	else
		fail_test "a hit from a linked worktree was not kept: $(cat "$canary_stub/linked.out")"
	fi

	# A stub that gave up waiting for a canary to start fails the suite, so
	# skipping that wait from then on is never silent (see the ceiling above).
	[ ! -e "$canary_expired" ] ||
		fail_test "a canary did not start within $((stub_ceiling / 10)) s (run, target): $(cat "$canary_expired")"
}

# ---------------------------------------------------------------------------
echo
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
