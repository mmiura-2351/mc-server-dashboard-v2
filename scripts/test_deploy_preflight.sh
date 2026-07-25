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

# ---------------------------------------------------------------------------
# Fixture: a clean temp repo on 'main' (so the two pre-existing checks pass)
# plus stub `sg` and `docker` in a sibling bin/ -- outside the repo, or the
# stubs themselves would make the working tree dirty. Prints the base dir.
# ---------------------------------------------------------------------------
make_fixture() {
	local base dir bin
	base="$(mktemp -d)"
	dir="$base/repo"
	bin="$base/bin"
	mkdir -p "$dir" "$bin"
	git -C "$dir" init -b main -q
	git -C "$dir" config user.email "test@example.com"
	git -C "$dir" config user.name "Test"
	touch "$dir/file.txt"
	git -C "$dir" add file.txt
	git -C "$dir" commit -q -m "init"

	# `sg <group> -c <command>` -- run the command string, ignoring the group.
	cat > "$bin/sg" << 'SGEOF'
#!/bin/sh
exec sh -c "$3"
SGEOF

	# Answers only the three sub-commands the guard issues; MOCK_DB_IMAGE,
	# MOCK_VOLUME_EXISTS and MOCK_PG_VERSION drive each case. MOCK_RUN_LOG, when
	# set, records the probe invocation so a test can assert which image it used.
	cat > "$bin/docker" << 'DOCKEREOF'
#!/bin/sh
case "$1 $2" in
	"compose config")
		printf '{"services": {"db": {"image": "%s"}}, "volumes": {"db-data": {"name": "testproj_db-data"}}}\n' \
			"${MOCK_DB_IMAGE:-postgres:18}"
		;;
	"volume inspect")
		[ "${MOCK_VOLUME_EXISTS:-1}" = "1" ] || exit 1
		echo "[]"
		;;
	"run --rm")
		if [ -n "${MOCK_RUN_LOG:-}" ]; then
			echo "$*" >> "$MOCK_RUN_LOG"
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
	base="$(make_fixture)"
	run_preflight "$base" MOCK_DB_IMAGE=postgres:18 MOCK_PG_VERSION=17
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
	rm -rf "$base"
}

# --- 3. Matching major -- pass ---
{
	base="$(make_fixture)"
	run_preflight "$base" MOCK_DB_IMAGE=postgres:18 MOCK_PG_VERSION=18
	if [ "$exit_code" -eq 0 ]; then
		ok "PG18 data + postgres:18 target: deploy allowed"
	else
		fail_test "PG18 data + postgres:18 target: expected exit 0, got $exit_code -- $output"
	fi
	rm -rf "$base"
}

# --- 4. Volume exists but holds no cluster -- pass (must not be a hard error) ---
{
	base="$(make_fixture)"
	run_preflight "$base" MOCK_DB_IMAGE=postgres:18 MOCK_PG_VERSION=
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
	base="$(make_fixture)"
	run_preflight "$base" MOCK_DB_IMAGE=registry.example.com:5000/postgres:18 MOCK_PG_VERSION=18
	if [ "$exit_code" -eq 0 ]; then
		ok "registry port in the image ref: deploy allowed"
	else
		fail_test "registry port in the image ref: expected exit 0, got $exit_code -- $output"
	fi
	rm -rf "$base"
}

# --- 7. Digest-pinned image -- no readable major, skip rather than guess ---
{
	base="$(make_fixture)"
	run_preflight "$base" \
		MOCK_DB_IMAGE=postgres@sha256:18f0e6c9c8b1a2d3e4f5061728394a5b6c7d8e9f0a1b2c3d4e5f60718293a4b5c \
		MOCK_PG_VERSION=17
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
	base="$(make_fixture)"
	run_preflight "$base" MOCK_DB_IMAGE=postgres:latest MOCK_PG_VERSION=17
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
	base="$(make_fixture)"
	run_preflight "$base" MOCK_DB_IMAGE=postgres:18.4-bookworm MOCK_PG_VERSION=17
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
	base="$(make_fixture)"
	run_preflight "$base" MOCK_DB_IMAGE=postgres:18 MOCK_PG_VERSION=18 \
		MOCK_RUN_LOG="$base/run.log"
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

# ---------------------------------------------------------------------------
echo
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
