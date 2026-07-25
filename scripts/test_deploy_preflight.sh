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
	# MOCK_VOLUME_EXISTS and MOCK_PG_VERSION drive each case.
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
	case "$output" in
		*skipping*) ok "docker unavailable: skip is reported" ;;
		*) fail_test "docker unavailable: skip is silent -- $output" ;;
	esac
	rm -rf "$base"
}

# ---------------------------------------------------------------------------
echo
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
