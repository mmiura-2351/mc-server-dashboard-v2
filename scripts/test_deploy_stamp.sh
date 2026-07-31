#!/usr/bin/env bash
#
# test_deploy_stamp.sh: unit tests for the deploy stamp written by
# scripts/update.sh and scripts/deploy.sh (issue #2311).
#
# The stamp answers "which revision was the running stack built and started
# from" -- that is what update.sh's change detection diffs against. The
# healthcheck verdict is a different fact and lives in its own file. These tests
# pin both, plus the partial-failure cases where no revision honestly describes
# the stack.
#
# `sg`, `docker`, `curl` and `sleep` are stubbed on PATH, so nothing here
# reaches a real Docker daemon, the live stack, or the repo root's real stamp:
# every case runs against a throwaway git repo under a temp dir.
#
# Exit code: 0 = all pass, non-zero = at least one failure.
set -uo pipefail

# Drop GIT_* leaks from any enclosing hook / test runner (pre-push runs this
# via `make check`), so the temp repos below are truly isolated.
unset "${!GIT_@}"

SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
UPDATE_SCRIPT="$SCRIPTS_DIR/update.sh"
DEPLOY_SCRIPT="$SCRIPTS_DIR/deploy.sh"
for s in "$UPDATE_SCRIPT" "$DEPLOY_SCRIPT"; do
	if [ ! -f "$s" ]; then
		echo "FAIL: script not found: $s" >&2
		exit 1
	fi
done

STAMP=".last-deploy-sha"
HEALTH=".last-deploy-health"

pass=0
fail=0

ok()        { echo "  PASS: $1"; pass=$((pass + 1)); }
fail_test() { echo "  FAIL: $1"; fail=$((fail + 1)); }

# ---------------------------------------------------------------------------
# Fixture: a temp repo on 'main' with an 'origin' to pull from, a stub
# deploy_preflight.sh (the real one guards the live host and is tested
# separately), and stub `sg` / `docker` / `curl` / `sleep` in a sibling bin/.
#
# The stubs are driven by MOCK_* environment variables:
#   MOCK_BUILD_STATUS  exit status of `docker build`      (default 0)
#   MOCK_UP_STATUS     exit status of `docker compose up` (default 0)
#   MOCK_HEALTH_STATUS exit status of the healthcheck curl (default 0)
# and every docker invocation is appended to $MOCK_LOG.
#
# Prints the base dir.
# ---------------------------------------------------------------------------
make_fixture() {
	local base dir bin
	base="$(mktemp -d)"
	dir="$base/repo"
	bin="$base/bin"
	mkdir -p "$dir/scripts" "$dir/api" "$dir/relay" "$dir/worker" "$bin"
	git -C "$dir" init -b main -q
	git -C "$dir" config user.email "test@example.com"
	git -C "$dir" config user.name "Test"
	printf '#!/bin/sh\nexit 0\n' > "$dir/scripts/deploy_preflight.sh"
	chmod +x "$dir/scripts/deploy_preflight.sh"
	printf 'services: {}\n' > "$dir/compose.yaml"
	printf 'API_HTTP_PORT=8000\n' > "$dir/.env"
	echo v1 > "$dir/api/main.py"
	echo v1 > "$dir/relay/main.go"
	echo v1 > "$dir/worker/main.go"
	git -C "$dir" add -A
	git -C "$dir" commit -q -m "init"

	git init -q --bare -b main "$base/origin.git"
	git -C "$dir" remote add origin "$base/origin.git"
	git -C "$dir" push -q origin main

	# `sg <group> -c <command>` -- run the command string, ignoring the group.
	cat > "$bin/sg" << 'SGEOF'
#!/bin/sh
exec sh -c "$3"
SGEOF

	cat > "$bin/docker" << 'DOCKEREOF'
#!/bin/sh
echo "docker $*" >> "$MOCK_LOG"
case "$1" in
	build)   exit "${MOCK_BUILD_STATUS:-0}" ;;
	compose) exit "${MOCK_UP_STATUS:-0}" ;;
esac
exit 0
DOCKEREOF

	printf '#!/bin/sh\nexit "${MOCK_HEALTH_STATUS:-0}"\n' > "$bin/curl"
	# The healthcheck polls 30 times with `sleep 2`; a failing case would
	# otherwise cost a real minute per test.
	printf '#!/bin/sh\nexit 0\n' > "$bin/sleep"

	chmod +x "$bin/sg" "$bin/docker" "$bin/curl" "$bin/sleep"
	echo "$base"
}

# Push a new commit to origin so the scripts' `git pull` has something to fetch.
#   advance <base> <path> <content>
advance() {
	local base="$1" path="$2" content="$3" work="$base/work"
	rm -rf "$work"
	git clone -q -b main "$base/origin.git" "$work"
	git -C "$work" config user.email "test@example.com"
	git -C "$work" config user.name "Test"
	echo "$content" > "$work/$path"
	git -C "$work" add -A
	git -C "$work" commit -q -m "change $path"
	git -C "$work" push -q origin main
}

# Record a previously deployed-and-verified revision, the way a successful run
# leaves the repo.
seed_stamp() {
	local base="$1" sha="$2"
	echo "$sha" > "$base/repo/$STAMP"
	echo "ok" > "$base/repo/$HEALTH"
}

# Run one of the deploy scripts in a fixture. Captures stdout+stderr in
# `output`, the status in `exit_code`, and this run's docker calls in
# `docker_log` (the log is truncated first, so each call sees only its own).
#   run_script <base> <script> [VAR=value ...]
run_script() {
	local base="$1" script="$2"
	shift 2
	: > "$base/docker.log"
	exit_code=0
	output="$(
		cd "$base/repo" || exit 99
		env PATH="$base/bin:$PATH" MOCK_LOG="$base/docker.log" "$@" bash "$script" 2>&1
	)" || exit_code=$?
	docker_log="$(cat "$base/docker.log")"
}

run_update() {
	local base="$1"
	shift
	run_script "$base" "$UPDATE_SCRIPT" "$@"
}

head_sha()  { git -C "$1/repo" rev-parse HEAD; }
stamp_of()  { cat "$1/repo/$STAMP" 2>/dev/null; }
health_of() { cat "$1/repo/$HEALTH" 2>/dev/null; }

assert_stamp() {
	local base="$1" want="$2" label="$3" got
	got="$(stamp_of "$base")"
	if [ "$got" = "$want" ]; then
		ok "$label"
	else
		fail_test "$label: stamp is '${got:-<absent>}', expected '${want}'"
	fi
}

assert_output_contains() {
	local label="$1" needle="$2"
	case "$output" in
		*"$needle"*) ok "$label" ;;
		*) fail_test "$label: output lacks '$needle' -- $output" ;;
	esac
}

assert_output_lacks() {
	local label="$1" needle="$2"
	case "$output" in
		*"$needle"*) fail_test "$label: output contains '$needle' -- $output" ;;
		*) ok "$label" ;;
	esac
}

assert_built() {
	local label="$1" component="$2"
	case "$docker_log" in
		*"mcsd-${component}:dev"*) ok "$label" ;;
		*) fail_test "$label: ${component} was not built -- $docker_log" ;;
	esac
}

assert_not_built() {
	local label="$1" component="$2"
	case "$docker_log" in
		*"mcsd-${component}:dev"*) fail_test "$label: ${component} was built -- $docker_log" ;;
		*) ok "$label" ;;
	esac
}

# ---------------------------------------------------------------------------
echo "=== deploy stamp semantics (update.sh / deploy.sh) ==="

# --- 1. A failed healthcheck still stamps the revision that was STARTED ------
# The containers were recreated from the new images before /api/healthz was
# ever polled, so the running stack IS the new revision whether or not it
# answers. Leaving the pre-deploy sha behind is what made change detection
# diff from a revision that stopped running (#2311).
{
	base="$(make_fixture)"
	seed_stamp "$base" "$(head_sha "$base")"
	advance "$base" api/main.py v2
	run_update "$base" MOCK_HEALTH_STATUS=7

	if [ "$exit_code" -ne 0 ]; then
		ok "failed healthcheck: update still exits non-zero"
	else
		fail_test "failed healthcheck: expected a non-zero exit, got 0 -- $output"
	fi
	assert_stamp "$base" "$(head_sha "$base")" "failed healthcheck: stamp names the started revision"
	if [ "$(health_of "$base")" = "failed" ]; then
		ok "failed healthcheck: the verdict is recorded separately as 'failed'"
	else
		fail_test "failed healthcheck: health record is '$(health_of "$base")', expected 'failed'"
	fi
	rm -rf "$base"
}

# --- 2. Re-running after a failed healthcheck re-verifies, never "nothing to do"
# Moving the stamp forward must not turn the retry into a no-op: HEAD now
# equals the stamp, but the stack was never confirmed healthy. Nothing changed
# in the tree, so nothing may be rebuilt -- least of all the worker, whose
# restart bounces running MC servers (DEPLOYMENT.md Section 9).
{
	base="$(make_fixture)"
	seed_stamp "$base" "$(head_sha "$base")"
	advance "$base" api/main.py v2
	run_update "$base" MOCK_HEALTH_STATUS=7

	run_update "$base" MOCK_HEALTH_STATUS=0
	if [ "$exit_code" -eq 0 ]; then
		ok "retry after a failed healthcheck: succeeds once the API answers"
	else
		fail_test "retry after a failed healthcheck: expected exit 0, got $exit_code -- $output"
	fi
	assert_output_lacks "retry after a failed healthcheck: does not report nothing to do" "nothing to do"
	assert_not_built "retry after a failed healthcheck: api is not rebuilt" "api"
	assert_not_built "retry after a failed healthcheck: worker is not rebuilt" "worker"
	case "$docker_log" in
		*"compose up -d"*) ok "retry after a failed healthcheck: the stack is re-started and re-checked" ;;
		*) fail_test "retry after a failed healthcheck: no compose up -- $docker_log" ;;
	esac
	if [ "$(health_of "$base")" = "ok" ]; then
		ok "retry after a failed healthcheck: the verdict flips to 'ok'"
	else
		fail_test "retry after a failed healthcheck: health record is '$(health_of "$base")', expected 'ok'"
	fi
	rm -rf "$base"
}

# --- 3. The happy path stamps and records the pass ---------------------------
{
	base="$(make_fixture)"
	seed_stamp "$base" "$(head_sha "$base")"
	advance "$base" api/main.py v2
	run_update "$base"

	if [ "$exit_code" -eq 0 ]; then
		ok "successful deploy: exit 0"
	else
		fail_test "successful deploy: expected exit 0, got $exit_code -- $output"
	fi
	assert_stamp "$base" "$(head_sha "$base")" "successful deploy: stamp names the deployed revision"
	if [ "$(health_of "$base")" = "ok" ]; then
		ok "successful deploy: the verdict is recorded as 'ok'"
	else
		fail_test "successful deploy: health record is '$(health_of "$base")', expected 'ok'"
	fi
	rm -rf "$base"
}

# --- 4. A verified deploy at the same HEAD stays a no-op ---------------------
{
	base="$(make_fixture)"
	seed_stamp "$base" "$(head_sha "$base")"
	run_update "$base"

	if [ "$exit_code" -eq 0 ]; then
		ok "verified stamp at HEAD: exit 0"
	else
		fail_test "verified stamp at HEAD: expected exit 0, got $exit_code -- $output"
	fi
	assert_output_contains "verified stamp at HEAD: reports nothing to do" "nothing to do"
	if [ -z "$docker_log" ]; then
		ok "verified stamp at HEAD: touches no containers"
	else
		fail_test "verified stamp at HEAD: ran docker -- $docker_log"
	fi
	rm -rf "$base"
}

# --- 5. A failed `compose up` leaves no stamp at all -------------------------
# Compose recreates services one at a time, so a failure part-way can leave
# some on the newly built images and some on the old ones. Neither revision
# describes that stack, so the honest record is none -- which forces the next
# run down the rebuild-everything path rather than diffing from a fiction.
{
	base="$(make_fixture)"
	seed_stamp "$base" "$(head_sha "$base")"
	advance "$base" api/main.py v2
	run_update "$base" MOCK_UP_STATUS=1

	if [ "$exit_code" -ne 0 ]; then
		ok "failed compose up: update exits non-zero"
	else
		fail_test "failed compose up: expected a non-zero exit, got 0 -- $output"
	fi
	if [ ! -f "$base/repo/$STAMP" ]; then
		ok "failed compose up: the stamp is cleared, not left claiming a revision"
	else
		fail_test "failed compose up: stamp survived as '$(stamp_of "$base")'"
	fi
	if [ ! -f "$base/repo/$HEALTH" ]; then
		ok "failed compose up: the stale health verdict is cleared too"
	else
		fail_test "failed compose up: health record survived as '$(health_of "$base")'"
	fi
	assert_output_contains "failed compose up: says what it cleared" "$STAMP"

	# ... and the next run rebuilds everything off that cleared stamp.
	run_update "$base"
	assert_built "after a failed compose up: api is rebuilt" "api"
	assert_built "after a failed compose up: relay is rebuilt" "relay"
	assert_built "after a failed compose up: worker is rebuilt" "worker"
	rm -rf "$base"
}

# --- 6. A build failure leaves the previous stamp untouched ------------------
# `compose up` never ran, so the containers are still the ones the old stamp
# names. That record is accurate and must not be disturbed.
{
	base="$(make_fixture)"
	seed_stamp "$base" "$(head_sha "$base")"
	seed="$(head_sha "$base")"
	advance "$base" api/main.py v2
	run_update "$base" MOCK_BUILD_STATUS=1

	if [ "$exit_code" -ne 0 ]; then
		ok "failed build: update exits non-zero"
	else
		fail_test "failed build: expected a non-zero exit, got 0 -- $output"
	fi
	assert_stamp "$base" "$seed" "failed build: the still-running revision stays stamped"
	rm -rf "$base"
}

# --- 7. An absent stamp is loud, and rebuilds everything ---------------------
# A deployment brought up with the documented plain `docker compose up -d
# --build` (DEPLOYMENT.md Section 4) has no stamp. There is no base to diff
# from, so the only honest answer is "rebuild all" -- said out loud, because it
# bounces running MC servers.
{
	base="$(make_fixture)"
	advance "$base" api/main.py v2
	run_update "$base"

	if [ "$exit_code" -eq 0 ]; then
		ok "absent stamp: deploy proceeds"
	else
		fail_test "absent stamp: expected exit 0, got $exit_code -- $output"
	fi
	assert_output_contains "absent stamp: the reason is stated" "unknown"
	assert_output_contains "absent stamp: names the stamp file" "$STAMP"
	assert_built "absent stamp: api is rebuilt" "api"
	assert_built "absent stamp: relay is rebuilt" "relay"
	assert_built "absent stamp: worker is rebuilt" "worker"
	rm -rf "$base"
}

# --- 8. An empty stamp file is "unknown", not a diff against an empty string --
# `git diff --name-only "" HEAD` aborts the run with a raw git error part-way
# through a deploy. An unusable stamp is the same fact as a missing one.
{
	base="$(make_fixture)"
	: > "$base/repo/$STAMP"
	advance "$base" api/main.py v2
	run_update "$base"

	if [ "$exit_code" -eq 0 ]; then
		ok "empty stamp: deploy proceeds"
	else
		fail_test "empty stamp: expected exit 0, got $exit_code -- $output"
	fi
	assert_output_lacks "empty stamp: no raw git error" "ambiguous argument"
	assert_built "empty stamp: api is rebuilt" "api"
	assert_built "empty stamp: worker is rebuilt" "worker"
	rm -rf "$base"
}

# --- 9. A stamp naming a commit this checkout does not have is "unknown" -----
{
	base="$(make_fixture)"
	echo "0123456789012345678901234567890123456789" > "$base/repo/$STAMP"
	advance "$base" api/main.py v2
	run_update "$base"

	if [ "$exit_code" -eq 0 ]; then
		ok "unresolvable stamp: deploy proceeds"
	else
		fail_test "unresolvable stamp: expected exit 0, got $exit_code -- $output"
	fi
	assert_output_contains "unresolvable stamp: the reason is stated" "unknown"
	assert_built "unresolvable stamp: worker is rebuilt" "worker"
	rm -rf "$base"
}

# --- 10. Selective rebuild still selects -------------------------------------
{
	base="$(make_fixture)"
	seed_stamp "$base" "$(head_sha "$base")"
	advance "$base" relay/main.go v2
	run_update "$base"

	assert_built "selective rebuild: relay is rebuilt" "relay"
	assert_not_built "selective rebuild: api is not rebuilt" "api"
	assert_not_built "selective rebuild: worker is not rebuilt" "worker"
	rm -rf "$base"
}

# --- 11. A stale stamp can no longer hide a component that really changed ----
# The regression the issue measured: stamp at c1, a deploy of c2 (api change)
# fails its healthcheck, then c3 reverts api/ back to c1's content and c4
# touches worker/. Diffing c1..c4 shows no api change, so api is skipped while
# the running api image is c2's. Stamping c2 at start time makes the diff
# c2..c4, which sees the revert.
{
	base="$(make_fixture)"
	seed_stamp "$base" "$(head_sha "$base")"
	advance "$base" api/main.py v2
	run_update "$base" MOCK_HEALTH_STATUS=7     # c2 starts, never goes healthy

	advance "$base" api/main.py v1              # c3 reverts api/ to c1's content
	advance "$base" worker/main.go v2           # c4 touches worker/
	run_update "$base"

	assert_built "reverted component: api is rebuilt off the running revision" "api"
	assert_built "reverted component: worker is rebuilt" "worker"
	rm -rf "$base"
}

# --- 12. deploy.sh writes the same stamp with the same meaning ---------------
# It is the other writer of the file update.sh reads; a stamp that means
# "started" from one script and "verified" from the other is the original bug
# with an extra step.
{
	base="$(make_fixture)"
	run_script "$base" "$DEPLOY_SCRIPT" MOCK_HEALTH_STATUS=7

	if [ "$exit_code" -ne 0 ]; then
		ok "deploy.sh failed healthcheck: exits non-zero"
	else
		fail_test "deploy.sh failed healthcheck: expected a non-zero exit, got 0 -- $output"
	fi
	assert_stamp "$base" "$(head_sha "$base")" "deploy.sh failed healthcheck: stamp names the started revision"
	if [ "$(health_of "$base")" = "failed" ]; then
		ok "deploy.sh failed healthcheck: the verdict is recorded as 'failed'"
	else
		fail_test "deploy.sh failed healthcheck: health record is '$(health_of "$base")', expected 'failed'"
	fi

	# And a subsequent `make update` at that HEAD re-verifies rather than
	# reporting the unhealthy stack as up to date.
	run_update "$base"
	assert_output_lacks "after deploy.sh: update does not report nothing to do" "nothing to do"
	rm -rf "$base"
}

# ---------------------------------------------------------------------------
echo
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
