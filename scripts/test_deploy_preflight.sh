#!/usr/bin/env bash
#
# test_deploy_preflight.sh: unit tests for the Postgres major-version guard in
# scripts/deploy_preflight.sh (issue #2133).
#
# The guard shells out to `sg docker -c "docker ..."`. Both are stubbed on PATH
# so these tests never reach a real Docker daemon and never touch the live
# db-data volume: the stub `sg` just runs its command string, and the stub
# `docker` answers `compose config`, `volume inspect`, and `run` from MOCK_*
# environment variables.
#
# Exit code: 0 = all pass, non-zero = at least one failure.
set -uo pipefail

# Drop GIT_* leaks from any enclosing hook / test runner (pre-push runs this
# via `make check`), so the temp repos below are truly isolated.
unset "${!GIT_@}"

SCRIPT="$(cd "$(dirname "$0")" && pwd)/deploy_preflight.sh"
if [ ! -x "$SCRIPT" ]; then
	echo "FAIL: script not found or not executable: $SCRIPT" >&2
	exit 1
fi

pass=0
fail=0

ok()        { echo "  PASS: $1"; pass=$((pass + 1)); }
fail_test() { echo "  FAIL: $1"; fail=$((fail + 1)); }

write_compose() {
	printf 'services:\n  db:\n    image: %s\n    volumes:\n      - db-data:/var/lib/postgresql\nvolumes:\n  db-data:\n' \
		"$2" > "$1/compose.yaml"
}

# ---------------------------------------------------------------------------
# Fixture: a clean temp repo on 'main' (so the two pre-existing checks pass)
# with an 'origin' it can fetch from, plus stub `sg` and `docker` in a sibling
# bin/ -- outside the repo, or the stubs themselves would make the working tree
# dirty. Prints the base dir.
#
#   make_fixture [working-tree image] [origin/main image]
#
# The two images are set independently because the guard must read its target
# from the revision that is about to be deployed. When they differ the repo is
# left one commit BEHIND origin/main -- the state every deploy path runs the
# preflight in, since all of them are preflight -> git pull -> docker compose up
# (#2303). The remote-tracking ref is dropped afterwards, so origin/main only
# resolves if the guard really fetches.
# ---------------------------------------------------------------------------
make_fixture() {
	local wt_image="${1:-postgres:18}" origin_image="${2:-}" base dir bin
	origin_image="${origin_image:-$wt_image}"
	base="$(mktemp -d)"
	dir="$base/repo"
	bin="$base/bin"
	mkdir -p "$dir" "$bin"
	git -C "$dir" init -b main -q
	git -C "$dir" config user.email "test@example.com"
	git -C "$dir" config user.name "Test"
	write_compose "$dir" "$wt_image"
	git -C "$dir" add compose.yaml
	git -C "$dir" commit -q -m "init"

	git init -q --bare "$base/origin.git"
	git -C "$dir" remote add origin "$base/origin.git"
	git -C "$dir" push -q origin main
	if [ "$origin_image" != "$wt_image" ]; then
		write_compose "$dir" "$origin_image"
		git -C "$dir" commit -q -am "bump the db image"
		git -C "$dir" push -q origin main
		git -C "$dir" reset -q --hard HEAD~1
	fi
	git -C "$dir" update-ref -d refs/remotes/origin/main

	# `sg <group> -c <command>` -- run the command string, ignoring the group.
	cat > "$bin/sg" << 'SGEOF'
#!/bin/sh
exec sh -c "$3"
SGEOF

	# Answers only the three sub-commands the guard issues; the compose.yaml the
	# guard hands it plus MOCK_VOLUME_EXISTS and MOCK_PG_VERSION drive each case.
	# MOCK_RUN_LOG, when set, records the probe invocation so a test can assert
	# which image it used.
	cat > "$bin/docker" << 'DOCKEREOF'
#!/bin/sh
case "$1 $2" in
	"compose config" | "compose -f")
		# Real `docker compose config` reads compose.yaml from the project
		# directory unless `-f -` feeds it one on stdin. The stub models both, so
		# a test can tell WHICH revision the guard resolved its target from.
		if [ "$2" = "-f" ]; then
			yaml="$(cat)"
		else
			yaml="$(cat compose.yaml)"
		fi
		image="$(printf '%s\n' "$yaml" | sed -n 's/^[[:space:]]*image:[[:space:]]*//p' | head -1)"
		printf '{"services": {"db": {"image": "%s"}}, "volumes": {"db-data": {"name": "%s"}}}\n' \
			"$image" "${MOCK_VOLUME_NAME-testproj_db-data}"
		;;
	"volume ls")
		# The guard LISTS volumes rather than inspecting one, so that a daemon
		# which cannot answer is distinguishable from a volume that is genuinely
		# not there -- `volume inspect` fails identically for both (#2301).
		[ "${MOCK_VOLUME_LS_FAILS:-0}" = "1" ] && exit 1
		# A host's other volumes come back too, including one whose name has the
		# target as a prefix: the match has to be exact, not a substring.
		echo "${MOCK_VOLUME_NAME-testproj_db-data}-old"
		[ "${MOCK_VOLUME_EXISTS:-1}" = "1" ] && echo "${MOCK_VOLUME_NAME-testproj_db-data}"
		exit 0
		;;
	"run --rm")
		# One ARGUMENT per line, not "$*": the point of the log is which argv the
		# probe was invoked with, and a value that was split by a shell on its way
		# here is indistinguishable from one that was not once they are joined
		# back together with spaces (#2308).
		if [ -n "${MOCK_RUN_LOG:-}" ]; then
			printf '%s\n' "$@" >> "$MOCK_RUN_LOG"
		fi
		printf '%s\n' "${MOCK_PG_VERSION:-}"
		;;
	*)
		echo "stub docker: unexpected invocation: $*" >&2
		exit 2
		;;
esac
DOCKEREOF

	chmod +x "$bin/sg" "$bin/docker"
	echo "$base"
}

# Run the preflight inside a fixture; captures stdout+stderr in `output` and
# the status in `exit_code`.
run_preflight() {
	local base="$1"
	shift
	exit_code=0
	output="$(
		cd "$base/repo" || exit 99
		env PATH="$base/bin:$PATH" "$@" bash "$SCRIPT" 2>&1
	)" || exit_code=$?
}

# Same, but with PATH restricted to the stub dir so the script finds no python3.
# `git` is symlinked in because the two pre-existing checks need it.
run_preflight_without_python3() {
	local base="$1" bash_bin
	bash_bin="$(command -v bash)"
	ln -sf "$(command -v git)" "$base/bin/git"
	exit_code=0
	output="$(
		cd "$base/repo" || exit 99
		env PATH="$base/bin" "$bash_bin" "$SCRIPT" 2>&1
	)" || exit_code=$?
}

# Same again, on a host with no `timeout`: PATH is restricted to the stub dir
# with everything the guard and the stubs need symlinked in EXCEPT timeout, so
# the fallback is exercised for real rather than simulated.
run_preflight_without_timeout() {
	local base="$1" bash_bin tool
	shift
	bash_bin="$(command -v bash)"
	for tool in sh git python3 cat sed head grep; do
		ln -sf "$(command -v "$tool")" "$base/bin/$tool"
	done
	exit_code=0
	output="$(
		cd "$base/repo" || exit 99
		env PATH="$base/bin" "$@" "$bash_bin" "$SCRIPT" 2>&1
	)" || exit_code=$?
}

# A skip must be loud AND must not read as a completed check: the stderr line
# says SKIPPED, and the success line is marked so "ok to build" on its own can
# never be mistaken for "the db-data volume was checked and is compatible".
assert_skipped() {
	local label="$1"
	case "$output" in
		*"SKIPPED the Postgres version check"*) ok "$label: skip is reported" ;;
		*) fail_test "$label: skip is not reported -- $output" ;;
	esac
	case "$output" in
		*"ok to build. (Postgres version check skipped"*)
			ok "$label: success line is marked as unchecked" ;;
		*)
			fail_test "$label: success line reads as a full pass -- $output" ;;
	esac
}

# ---------------------------------------------------------------------------
echo "=== deploy_preflight Postgres version guard tests ==="

# --- 1. No db-data volume yet (fresh deployment) -- pass, stay quiet ---
{
	base="$(make_fixture)"
	run_preflight "$base" MOCK_VOLUME_EXISTS=0
	if [ "$exit_code" -eq 0 ]; then
		ok "missing db-data volume: deploy allowed"
	else
		fail_test "missing db-data volume: expected exit 0, got $exit_code -- $output"
	fi
	case "$output" in
		*PostgreSQL*) fail_test "missing db-data volume: unexpected Postgres output -- $output" ;;
		*) ok "missing db-data volume: no Postgres complaint" ;;
	esac
	# A fresh deployment is a completed check ("nothing to be incompatible
	# with"), not a skip -- it must not claim the check was skipped.
	case "$output" in
		*SKIPPED*) fail_test "missing db-data volume: reported as a skip -- $output" ;;
		*) ok "missing db-data volume: reported as checked, not skipped" ;;
	esac
	rm -rf "$base"
}

# --- 2. PG17 data under a postgres:18 target -- refuse ---
{
	base="$(make_fixture postgres:18)"
	run_preflight "$base" MOCK_PG_VERSION=17
	if [ "$exit_code" -ne 0 ]; then
		ok "PG17 data + postgres:18 target: deploy refused"
	else
		fail_test "PG17 data + postgres:18 target: expected refusal, got exit 0 -- $output"
	fi
	case "$output" in
		*"PostgreSQL 17"*"postgres:18"*)
			ok "PG17 data + postgres:18 target: message names both majors" ;;
		*)
			fail_test "PG17 data + postgres:18 target: message lacks the majors -- $output" ;;
	esac
	case "$output" in
		*DEPLOYMENT.md*) ok "PG17 data + postgres:18 target: message points at the runbook" ;;
		*) fail_test "PG17 data + postgres:18 target: message lacks the runbook pointer -- $output" ;;
	esac
	# The refusal has to name the way out, not just the problem: the operator is
	# mid-deploy and the fix is one deliberately-invoked script (#2304).
	case "$output" in
		*"scripts/pg_major_upgrade.sh"*)
			ok "PG17 data + postgres:18 target: message names the upgrade script" ;;
		*)
			fail_test "PG17 data + postgres:18 target: message does not name the upgrade script -- $output" ;;
	esac
	rm -rf "$base"
}

# --- 3. Matching major -- pass ---
{
	base="$(make_fixture postgres:18)"
	run_preflight "$base" MOCK_PG_VERSION=18
	if [ "$exit_code" -eq 0 ]; then
		ok "PG18 data + postgres:18 target: deploy allowed"
	else
		fail_test "PG18 data + postgres:18 target: expected exit 0, got $exit_code -- $output"
	fi
	rm -rf "$base"
}

# --- 4. Volume exists but holds no cluster -- pass (must not be a hard error) ---
{
	base="$(make_fixture postgres:18)"
	run_preflight "$base" MOCK_PG_VERSION=
	if [ "$exit_code" -eq 0 ]; then
		ok "empty PG_VERSION: deploy allowed"
	else
		fail_test "empty PG_VERSION: expected exit 0, got $exit_code -- $output"
	fi
	rm -rf "$base"
}

# --- 5. Docker unusable -- skip the check with a note, do not block ---
{
	base="$(make_fixture)"
	# Simulate a session that cannot reach the daemon (no docker group, no
	# daemon, no `sg`): every wrapped invocation fails.
	printf '#!/bin/sh\nexit 1\n' > "$base/bin/sg"
	run_preflight "$base"
	if [ "$exit_code" -eq 0 ]; then
		ok "docker unavailable: deploy allowed"
	else
		fail_test "docker unavailable: expected exit 0, got $exit_code -- $output"
	fi
	assert_skipped "docker unavailable"
	rm -rf "$base"
}

# --- 6. Registry host with a port -- the port is not a major version ---
{
	base="$(make_fixture registry.example.com:5000/postgres:18)"
	run_preflight "$base" MOCK_PG_VERSION=18
	if [ "$exit_code" -eq 0 ]; then
		ok "registry port in the image ref: deploy allowed"
	else
		fail_test "registry port in the image ref: expected exit 0, got $exit_code -- $output"
	fi
	rm -rf "$base"
}

# --- 7. Digest-pinned image -- no readable major, skip rather than guess ---
{
	base="$(make_fixture postgres@sha256:18f0e6c9c8b1a2d3e4f5061728394a5b6c7d8e9f0a1b2c3d4e5f60718293a4b5c)"
	run_preflight "$base" MOCK_PG_VERSION=17
	if [ "$exit_code" -eq 0 ]; then
		ok "digest-pinned image: deploy allowed"
	else
		fail_test "digest-pinned image: expected exit 0, got $exit_code -- $output"
	fi
	assert_skipped "digest-pinned image"
	rm -rf "$base"
}

# --- 8. Non-numeric tag -- no readable major, skip rather than guess ---
{
	base="$(make_fixture postgres:latest)"
	run_preflight "$base" MOCK_PG_VERSION=17
	if [ "$exit_code" -eq 0 ]; then
		ok "'latest' tag: deploy allowed"
	else
		fail_test "'latest' tag: expected exit 0, got $exit_code -- $output"
	fi
	assert_skipped "'latest' tag"
	rm -rf "$base"
}

# --- 9. Tag with a leading integer -- the major is read from it ---
{
	base="$(make_fixture postgres:18.4-bookworm)"
	run_preflight "$base" MOCK_PG_VERSION=17
	if [ "$exit_code" -ne 0 ]; then
		ok "'18.4-bookworm' tag: PG17 data refused"
	else
		fail_test "'18.4-bookworm' tag: expected refusal, got exit 0 -- $output"
	fi
	rm -rf "$base"
}

# --- 10. python3 missing -- skip loudly, never silently no-op ---
{
	base="$(make_fixture)"
	run_preflight_without_python3 "$base"
	if [ "$exit_code" -eq 0 ]; then
		ok "python3 missing: deploy allowed"
	else
		fail_test "python3 missing: expected exit 0, got $exit_code -- $output"
	fi
	assert_skipped "python3 missing"
	case "$output" in
		*python3*) ok "python3 missing: the message names python3" ;;
		*) fail_test "python3 missing: the message does not name python3 -- $output" ;;
	esac
	rm -rf "$base"
}

# --- 11. The probe reuses the db image, not a separate helper image ---
{
	base="$(make_fixture postgres:18)"
	run_preflight "$base" MOCK_PG_VERSION=18 MOCK_RUN_LOG="$base/run.log"
	probe="$(cat "$base/run.log" 2>/dev/null || true)"
	case "$probe" in
		*postgres:18*) ok "probe runs the db image" ;;
		*) fail_test "probe does not run the db image -- $probe" ;;
	esac
	case "$probe" in
		*alpine*) fail_test "probe still pulls a separate helper image -- $probe" ;;
		*) ok "probe pulls no separate helper image" ;;
	esac
	rm -rf "$base"
}

# --- 12. Pre-pull checkout: the target comes from origin/main, not the tree ---
# Every deploy path is preflight -> git pull -> docker compose up, so the tree
# still pins the OLD image when the guard runs. Reading it compares 17 against
# 17, passes, and the stack goes down on the `up` right after the pull (#2303).
{
	base="$(make_fixture postgres:17 postgres:18)"
	run_preflight "$base" MOCK_PG_VERSION=17
	if [ "$exit_code" -ne 0 ]; then
		ok "PG17 data + incoming postgres:18: deploy refused before the pull"
	else
		fail_test "PG17 data + incoming postgres:18: expected refusal, got exit 0 -- $output"
	fi
	case "$output" in
		*"PostgreSQL 17"*"postgres:18"*)
			ok "pre-pull checkout: message names both majors" ;;
		*)
			fail_test "pre-pull checkout: message lacks the majors -- $output" ;;
	esac
	rm -rf "$base"
}

# --- 13. origin/main unreachable -- skip the check, never refuse ---
# The fetch is the one part of this guard that needs the network. An offline or
# flaky host must fall back to the same loud skip as every other "could not
# determine" case, not block a deploy.
{
	base="$(make_fixture postgres:17 postgres:18)"
	git -C "$base/repo" remote set-url origin "$base/no-such-remote.git"
	run_preflight "$base" MOCK_PG_VERSION=17
	if [ "$exit_code" -eq 0 ]; then
		ok "origin/main unreachable: deploy allowed"
	else
		fail_test "origin/main unreachable: expected exit 0, got $exit_code -- $output"
	fi
	assert_skipped "origin/main unreachable"
	case "$output" in
		*origin/main*) ok "origin/main unreachable: the message names origin/main" ;;
		*) fail_test "origin/main unreachable: the message does not name origin/main -- $output" ;;
	esac
	rm -rf "$base"
}

# --- 14. The daemon cannot answer whether the volume exists -- skip, loudly ---
# "Volume absent" and "could not ask" used to share an outcome, because
# `docker volume inspect` exits non-zero for both. Absence is a real answer a
# fresh host gives and stays silent (test 1); an unanswerable question is a
# "could not determine" case like every other, and the one thing the guard must
# never do is disappear without saying so (#2301).
{
	base="$(make_fixture postgres:18)"
	run_preflight "$base" MOCK_VOLUME_LS_FAILS=1 MOCK_PG_VERSION=17
	if [ "$exit_code" -eq 0 ]; then
		ok "docker cannot answer about the volume: deploy allowed"
	else
		fail_test "docker cannot answer about the volume: expected exit 0, got $exit_code -- $output"
	fi
	assert_skipped "docker cannot answer about the volume"
	case "$output" in
		*testproj_db-data*)
			ok "docker cannot answer about the volume: the message names the volume" ;;
		*)
			fail_test "docker cannot answer about the volume: the message does not name the volume -- $output" ;;
	esac
	rm -rf "$base"
}

# --- 15. A volume whose name merely CONTAINS the target's is not the target ---
# The absent/unanswerable split reads the daemon's list of volumes; matching that
# list loosely would invent a cluster to compare against on a fresh host.
{
	base="$(make_fixture postgres:18)"
	run_preflight "$base" MOCK_VOLUME_EXISTS=0 MOCK_PG_VERSION=17
	if [ "$exit_code" -eq 0 ]; then
		ok "near-miss volume name: deploy allowed"
	else
		fail_test "near-miss volume name: expected exit 0, got $exit_code -- $output"
	fi
	case "$output" in
		*SKIPPED*) fail_test "near-miss volume name: reported as a skip -- $output" ;;
		*) ok "near-miss volume name: still a silent, completed check" ;;
	esac
	rm -rf "$base"
}

# --- 16. A HANGING fetch is bounded -- skip rather than stall forever ---
# A hard-down network fails fast and reaches the skip in test 13. A black-holed
# route or a stalled proxy does not: the fetch blocks and the preflight blocks
# with it, indefinitely, with no output explaining why (#2306). The remote is an
# ssh:// URL and `ssh` is stubbed to sleep, which is a real hang rather than a
# simulated one; `timeout` is stubbed to keep its semantics on a shorter
# deadline, so the test does not have to wait out the production bound.
{
	base="$(make_fixture postgres:17 postgres:18)"
	printf '#!/bin/sh\nsleep 30\n' > "$base/bin/ssh"
	printf '#!/bin/sh\nshift\nexec %s 2 "$@"\n' "$(command -v timeout)" > "$base/bin/timeout"
	chmod +x "$base/bin/ssh" "$base/bin/timeout"
	git -C "$base/repo" remote set-url origin "ssh://example.invalid/repo.git"

	started="$(date +%s)"
	run_preflight "$base" MOCK_PG_VERSION=17
	elapsed=$(($(date +%s) - started))

	if [ "$elapsed" -lt 15 ]; then
		ok "hanging fetch: the preflight returns instead of blocking (${elapsed}s)"
	else
		fail_test "hanging fetch: the preflight waited ${elapsed}s on a hung fetch -- $output"
	fi
	if [ "$exit_code" -eq 0 ]; then
		ok "hanging fetch: deploy allowed"
	else
		fail_test "hanging fetch: expected exit 0, got $exit_code -- $output"
	fi
	assert_skipped "hanging fetch"
	case "$output" in
		*"did not finish within"*)
			ok "hanging fetch: the skip says the fetch ran out of time" ;;
		*)
			fail_test "hanging fetch: the skip does not name the deadline -- $output" ;;
	esac
	rm -rf "$base"
}

# --- 17. No `timeout` on the host -- run the fetch unbounded, never fail ---
# The bound is an improvement on a hang, not a new prerequisite. A host without
# coreutils' `timeout` must get the guard it had before, not a skip on every
# deploy -- which would make the guard useless in exactly the way #2295 spent
# three rounds avoiding.
{
	base="$(make_fixture postgres:17 postgres:18)"
	run_preflight_without_timeout "$base" MOCK_PG_VERSION=17
	if [ "$exit_code" -ne 0 ]; then
		ok "no timeout binary: the guard still refuses PG17 data under postgres:18"
	else
		fail_test "no timeout binary: expected a refusal, got exit 0 -- $output"
	fi
	case "$output" in
		*SKIPPED*) fail_test "no timeout binary: degraded to a skip -- $output" ;;
		*) ok "no timeout binary: the check still ran" ;;
	esac
	rm -rf "$base"
}

# --- 18. A volume name the shell would rewrite crosses the `sg` boundary ---
# Every value the guard puts in a docker invocation crosses one shell re-parse:
# `sg` has only a `-c <string>` interface, so the command is a string exactly
# once. The volume name is the value that reaches it from furthest away -- the
# operator's own .env, via `docker compose config` -- and a space, an apostrophe
# or a `$` in it is a typo, not an attack. Pasted into the string unquoted
# (#2308) this name split into three words and its unbalanced quote made the
# whole command a syntax error: the probe failed, the guard skipped, and the
# deploy it had to refuse went ahead. Both halves are asserted -- the outcome,
# and that docker was handed the name as ONE argument.
#
# All three characters are load-bearing. A wrapper that merely put DOUBLE quotes
# around each argument keeps the name in one word and satisfies everything else
# here, while still leaving the `$` for the shell to expand -- without it this
# pins only the splitting half of what the boundary has to do.
{
	odd_volume="odd 'na\$me"
	base="$(make_fixture postgres:18)"
	run_preflight "$base" MOCK_PG_VERSION=17 MOCK_VOLUME_NAME="$odd_volume" MOCK_RUN_LOG="$base/run.log"
	if [ "$exit_code" -ne 0 ]; then
		ok "quoted volume name: PG17 data under postgres:18 is still refused"
	else
		fail_test "quoted volume name: expected a refusal, got exit 0 -- $output"
	fi
	case "$output" in
		*SKIPPED*) fail_test "quoted volume name: the check degraded to a skip -- $output" ;;
		*) ok "quoted volume name: the check ran rather than skipping" ;;
	esac
	if grep -qxF -- "${odd_volume}:/probedata:ro" "$base/run.log" 2> /dev/null; then
		ok "quoted volume name: docker received it as a single argument, unchanged"
	else
		fail_test "quoted volume name: the probe's -v argument was mangled -- $(cat "$base/run.log" 2> /dev/null)"
	fi
	rm -rf "$base"
}

# ---------------------------------------------------------------------------
echo
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
