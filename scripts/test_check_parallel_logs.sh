#!/usr/bin/env bash
#
# test_check_parallel_logs.sh: a gate run keeps the previous run's logs, and
# hands its log dir to the api test run (issue #3750).
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
#
# The runs use the real script with a stub `make` on PATH, as
# test_check_parallel_identity.sh does: the stub writes its run's marker and the
# exported variable into the pre-flight golangci-lint install log -- the first
# thing the script runs -- and then fails, so no chain is ever spawned. `GIT_*`
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
# 3. A failed rotation step stops the run. Needs a non-root user: root can
#    delete from a read-only dir.
if [ "$(id -u)" -eq 0 ]; then
	echo "  SKIP: the failed-rotation case needs a non-root user"
else
	chmod a-w "$logdir.prev"
	if run_gate fourth; then
		fail_test "a run whose check-logs.prev cannot be removed did not fail"
	else
		ok "a run whose check-logs.prev cannot be removed fails"
	fi
	chmod u+w "$logdir.prev"

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
echo
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
