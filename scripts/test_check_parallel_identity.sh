#!/usr/bin/env bash
#
# test_check_parallel_identity.sh: the gate names its worktree in its own
# command line (issue #2605).
#
# The defect. Killing a backgrounded `git push` leaves the pre-push gate it
# spawned running, and nothing in the process listing tied that survivor to the
# worktree it belongs to: `make check` and `scripts/check_parallel.sh` both ran
# with a bare argv, so `pgrep -f <worktree>` found nothing and the only way to
# identify a survivor was `readlink /proc/<pid>/cwd`, one pid at a time. The
# resulting red -- the next gate run in that worktree dying mid-suite -- reads
# exactly like the #2228 / #2513 timeout flakes, so the diagnostic has to be
# cheap or nobody performs it (docs/dev/AGENTS.md Section 3).
#
# What is asserted, and why in this shape:
#
#   1. `make check` passes the worktree path to the orchestrator. This is the
#      half that can regress silently -- the argument looks decorative at the
#      call site, and dropping it breaks the diagnostic without breaking the
#      gate.
#   2. A *running* orchestrator carries that path in /proc/<pid>/cmdline and is
#      found by `pgrep -f <worktree>`. Asserting the recorded argv alone would
#      pass on a script that rejected or swallowed the argument, and pgrep is
#      the command the manual actually tells an agent to run.
#
# The second assertion runs the real script with a stub `make` on PATH: the
# stub records its parent's command line at the pre-flight golangci-lint
# install -- the first thing the script runs -- and then fails, so the run stops
# there and no chain is ever spawned. That makes the observation synchronous
# (no backgrounding, no sleeps, no races) and keeps the test off the host's
# cores.
#
# Two isolation details that are load-bearing:
#
#   * `GIT_*` is unset first. `git push` exports GIT_DIR pointing at the real
#     repository, and the script derives its log dir from `git rev-parse`; with
#     the variable inherited, the stub run would `rm -rf` the *live* gate's
#     check-logs, since this test itself runs inside `make check` (same reason
#     as test_shell_pipefail.sh).
#   * The stub lives in a different temp dir from the fake worktree used as the
#     pgrep pattern. Sharing one dir would put the pattern in the stub's own
#     command line and the match would prove nothing.
#   * `MCSD_CHECK_LOCK_FILE` points at a temp path. The script now takes a
#     host-global lock before it does anything (#2513); run inside a gate the
#     nested-invocation guard would skip it, but run standalone while another
#     gate is live this test would block on the real lock for the length of that
#     gate. Its own lock file makes it hermetic either way.
#
# Linux-only, by construction: /proc/<pid>/cmdline is what `pgrep -f` reads, and
# the diagnostic being pinned is itself a /proc one.
#
# Exit code: 0 = all pass, non-zero = at least one failure.
set -uo pipefail

unset "${!GIT_@}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

pass=0
fail=0

ok()        { echo "  PASS: $1"; pass=$((pass + 1)); }
fail_test() { echo "  FAIL: $1"; fail=$((fail + 1)); }

echo "=== check_parallel.sh worktree-identity tests ==="

# ---------------------------------------------------------------------------
# 1. `make check` hands the worktree path to the orchestrator.
{
	recipe="$(cd "$ROOT" && make -n check 2>&1 | grep -F 'check_parallel.sh')"
	case "$recipe" in
		*"$ROOT"*) ok "make check passes the worktree path to check_parallel.sh" ;;
		*) fail_test "make check does not pass the worktree path (recipe: ${recipe:-<none>})" ;;
	esac
}

# ---------------------------------------------------------------------------
# 2. A running orchestrator is findable by that path.
{
	fake_worktree="$(mktemp -d)"
	stub_dir="$(mktemp -d)"
	trap 'rm -rf "$fake_worktree" "$stub_dir"' EXIT

	git -C "$fake_worktree" init -q

	cat > "$stub_dir/make" << 'STUB'
#!/usr/bin/env bash
# Stub `make`: record how the orchestrator that invoked us is identified, then
# fail so the run stops before Phase 1a spawns anything.
tr '\0' ' ' < "/proc/$PPID/cmdline" > "$IDENTITY_CMDLINE"
echo "$PPID" > "$IDENTITY_PID"
pgrep -f "$IDENTITY_MARKER" > "$IDENTITY_PGREP" 2>/dev/null
exit 1
STUB
	chmod +x "$stub_dir/make"

	cmdline_file="$stub_dir/cmdline"
	pid_file="$stub_dir/pid"
	pgrep_file="$stub_dir/pgrep"

	(
		cd "$fake_worktree" &&
			PATH="$stub_dir:$PATH" \
				MCSD_CHECK_LOCK_FILE="$stub_dir/lock" \
				IDENTITY_CMDLINE="$cmdline_file" \
				IDENTITY_PID="$pid_file" \
				IDENTITY_PGREP="$pgrep_file" \
				IDENTITY_MARKER="$fake_worktree" \
				bash "$ROOT/scripts/check_parallel.sh" "$fake_worktree"
	) > /dev/null 2>&1

	if [ ! -s "$cmdline_file" ]; then
		fail_test "the stub run never reached the pre-flight make (nothing recorded)"
	else
		recorded="$(cat "$cmdline_file")"
		case "$recorded" in
			*"$fake_worktree"*) ok "a running check_parallel.sh carries its worktree in /proc/<pid>/cmdline" ;;
			*) fail_test "the running command line does not name the worktree: $recorded" ;;
		esac

		gate_pid="$(cat "$pid_file")"
		if grep -qx "$gate_pid" "$pgrep_file"; then
			ok "pgrep -f <worktree> finds the running gate (pid $gate_pid)"
		else
			fail_test "pgrep -f <worktree> did not find the gate (pid $gate_pid, matches: $(tr '\n' ' ' < "$pgrep_file"))"
		fi
	fi
}

# ---------------------------------------------------------------------------
echo
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
