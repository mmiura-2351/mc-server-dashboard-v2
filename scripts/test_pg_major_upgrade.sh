#!/usr/bin/env bash
#
# test_pg_major_upgrade.sh: unit tests for scripts/pg_major_upgrade.sh, the
# deliberately-invoked Postgres major upgrade (issue #2304).
#
# Same shape as test_deploy_preflight.sh: `sg` and `docker` are stubbed on PATH,
# so these tests never reach a real Docker daemon and never touch the live
# db-data volume. The stub answers `compose config/stop/down/up/exec`,
# `volume inspect/rm` and `run --rm` from MOCK_* environment variables and
# records every invocation in MOCK_LOG, which is what the ordering assertions
# below read.
#
# What this suite exists to pin -- the migration is destructive and irreversible,
# so the interesting cases are all about what must NOT have happened yet:
#   * a failed or truncated dump aborts with the volume still there;
#   * the archive exists before the original volume is released;
#   * the dump is taken by the major that wrote the cluster, never a newer one.
#
# Exit code: 0 = all pass, non-zero = at least one failure.
set -uo pipefail

# Drop GIT_* leaks from any enclosing hook / test runner (pre-push runs this via
# `make check`), so the temp repos below are truly isolated.
unset "${!GIT_@}"

SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$SCRIPTS_DIR/pg_major_upgrade.sh"
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
# Fixture: a clean temp repo on 'main' with an 'origin' to pull from, plus stub
# `sg` and `docker` in a sibling bin/ (outside the repo, or the stubs would make
# the working tree dirty). Prints the base dir.
#
#   make_fixture [working-tree image] [origin/main image]
#
# The default models the state the operator is actually in when the preflight
# refuses: the tree still pins the OLD image, origin/main pins the new one, and
# nothing has been pulled yet.
# ---------------------------------------------------------------------------
make_fixture() {
	local wt_image="${1:-postgres:17}" origin_image="${2:-postgres:18}" base dir bin
	base="$(mktemp -d)"
	dir="$base/repo"
	bin="$base/bin"
	mkdir -p "$dir" "$bin" "$base/out"
	git -C "$dir" init -b main -q
	git -C "$dir" config user.email "test@example.com"
	git -C "$dir" config user.name "Test"
	write_compose "$dir" "$wt_image"
	# Mirrors the real repo: the incomplete-upgrade sentinel is gitignored, so
	# writing it must not make the tree dirty for the next run's precondition.
	printf '.pg-upgrade-incomplete\n' > "$dir/.gitignore"
	git -C "$dir" add compose.yaml .gitignore
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
	# `exec` so stdin, stdout and the exit status all pass straight through: the
	# script pipes a dump out of one invocation and back into another.
	cat > "$bin/sg" << 'SGEOF'
#!/bin/sh
exec sh -c "$3"
SGEOF

	cat > "$bin/docker" << 'DOCKEREOF'
#!/bin/sh
log() { [ -n "${MOCK_LOG:-}" ] && echo "$*" >> "$MOCK_LOG"; return 0; }

case "$1 $2" in
	"compose config" | "compose -f")
		if [ "$2" = "-f" ]; then
			yaml="$(cat)"
		else
			yaml="$(cat compose.yaml)"
		fi
		image="$(printf '%s\n' "$yaml" | sed -n 's/^[[:space:]]*image:[[:space:]]*//p' | head -1)"
		log "compose config $image"
		printf '{"services": {"db": {"image": "%s", "environment": {"POSTGRES_USER": "%s", "POSTGRES_DB": "%s"}}}, "volumes": {"db-data": {"name": "testproj_db-data"}}}\n' \
			"$image" "${MOCK_PG_USER-mcsd}" "${MOCK_PG_DB-mcsd}"
		;;
	"volume inspect")
		[ "${MOCK_VOLUME_EXISTS:-1}" = "1" ] || exit 1
		echo "[]"
		;;
	"volume rm")
		# The whole point of the archive: record whether one existed at the
		# instant the original volume was released.
		if ls "${MCSD_PG_UPGRADE_DIR:-/nonexistent}"/*.tar.gz > /dev/null 2>&1; then
			log "volume rm $3 archive_present=yes"
		else
			log "volume rm $3 archive_present=no"
		fi
		;;
	"run --rm")
		case "$*" in
			*probedata*)
				printf '%s\n' "${MOCK_PG_VERSION-17}"
				;;
			*/out/*)
				log "archive $*"
				name="$(printf '%s' "$*" | sed -n 's|.*/out/\([^ ]*\).*|\1|p')"
				stage="$(mktemp -d)"
				echo "17" > "$stage/PG_VERSION"
				case "${MOCK_TAR_MODE:-ok}" in
					fail)
						exit 1
						;;
					corrupt)
						# tar exits 0 and a file appears, but the bytes are not a
						# readable archive -- what a truncated write looks like.
						printf 'not a tarball' > "${MCSD_PG_UPGRADE_DIR}/${name}"
						;;
					empty)
						# A perfectly readable archive of the wrong thing.
						rm -f "$stage/PG_VERSION"
						echo "x" > "$stage/unrelated"
						tar czf "${MCSD_PG_UPGRADE_DIR}/${name}" -C "$stage" . || exit 1
						;;
					*)
						# A real archive: the script verifies it by listing it back.
						tar czf "${MCSD_PG_UPGRADE_DIR}/${name}" -C "$stage" . || exit 1
						;;
				esac
				rm -rf "$stage"
				;;
			*)
				echo "stub docker: unexpected run: $*" >&2
				exit 2
				;;
		esac
		;;
	"compose stop" | "compose down" | "compose up")
		log "$*"
		;;
	"compose exec")
		case "$*" in
			*pg_isready*)
				# The official image's entrypoint runs a temporary bootstrap
				# server on the UNIX SOCKET (listen_addresses='') before the real
				# one, so `up --wait`'s healthcheck can pass while TCP is still
				# closed. MOCK_TCP_READY_AFTER models that window: the first N
				# TCP probes are refused, as they are against the real image.
				n=$(cat "${MCSD_PG_UPGRADE_DIR}/.tcp-probes" 2>/dev/null || echo 0)
				n=$((n + 1))
				echo "$n" > "${MCSD_PG_UPGRADE_DIR}/.tcp-probes"
				if [ "$n" -le "${MOCK_TCP_READY_AFTER:-0}" ]; then
					log "tcp-probe-refused $*"
					exit 1
				fi
				log "tcp-ready $*"
				;;
			*"pg_dumpall --version"*)
				log "version-probe $*"
				printf 'pg_dumpall (PostgreSQL) %s.6\n' "${MOCK_RUNNING_MAJOR:-17}"
				;;
			*pg_dumpall*)
				log "dump $*"
				case "${MOCK_DUMP_MODE:-ok}" in
					fail)
						# Deliberately a COMPLETE-looking dump, marker and all,
						# from a command that exits non-zero: only checking
						# pg_dumpall's own status can catch this one.
						printf -- '--\n-- PostgreSQL database cluster dump\n--\nCREATE ROLE mcsd;\n'
						printf -- '--\n-- PostgreSQL database cluster dump complete\n--\n\n'
						echo "pg_dumpall: error: query failed" >&2
						exit 1
						;;
					truncated)
						# Non-empty, plausible, and silently missing its tail --
						# exactly the shape a broken pipe or a full disk leaves.
						printf -- '--\n-- PostgreSQL database cluster dump\n--\nCREATE ROLE mcsd;\nCOPY public.servers (id) FROM std\n'
						;;
					*)
						# Shaped like a real pg_dumpall (verified against
						# postgres:17): the bootstrap role's CREATE is a bare
						# line of its own followed by the ALTER that carries its
						# attributes, and the app database is recreated with the
						# old cluster's encoding and locale.
						printf -- '--\n-- PostgreSQL database cluster dump\n--\n'
						printf 'CREATE ROLE mcsd;\n'
						[ "${MOCK_DUMP_MODE:-ok}" = "double_role" ] && printf 'CREATE ROLE mcsd;\n'
						printf "ALTER ROLE mcsd WITH SUPERUSER LOGIN PASSWORD 'SCRAM-SHA-256\$4096:x';\n"
						printf 'CREATE ROLE reporter;\n'
						if [ "${MOCK_DUMP_MODE:-ok}" != "no_createdb" ]; then
							printf "CREATE DATABASE mcsd WITH TEMPLATE = template0 ENCODING = 'UTF8' LOCALE = 'en_US.utf8';\n"
						fi
						[ "${MOCK_DUMP_MODE:-ok}" = "double_createdb" ] &&
							printf "CREATE DATABASE mcsd WITH TEMPLATE = template0 ENCODING = 'UTF8' LOCALE = 'en_US.utf8';\n"
						printf 'COPY public.servers (id, name) FROM stdin;\n1\\talpha\n\\.\n'
						printf -- '--\n-- PostgreSQL database cluster dump complete\n--\n\n'
						;;
				esac
				;;
			*-tAc*)
				log "table-count $*"
				printf '%s\n' "${MOCK_TABLE_COUNT:-42}"
				;;
			*"DROP DATABASE"*)
				log "dropdb $*"
				[ "${MOCK_DROPDB_FAILS:-0}" = "1" ] && exit 1
				;;
			*psql*)
				# Keep the restore's stdin so a test can assert what the script
				# actually fed psql (and so the writer never sees SIGPIPE).
				cat > "${MCSD_PG_UPGRADE_DIR}/restore-stdin.sql"
				log "restore $*"
				[ "${MOCK_RESTORE_FAILS:-0}" = "1" ] && exit 1
				;;
			*)
				echo "stub docker: unexpected exec: $*" >&2
				exit 2
				;;
		esac
		;;
	*)
		echo "stub docker: unexpected invocation: $*" >&2
		exit 2
		;;
esac
exit 0
DOCKEREOF

	chmod +x "$bin/sg" "$bin/docker"
	echo "$base"
}

# Run the upgrade inside a fixture; captures stdout+stderr in `output`, the
# status in `exit_code`, and the stub's invocation trace in `log`.
run_upgrade() {
	local base="$1"
	shift
	exit_code=0
	output="$(
		cd "$base/repo" || exit 99
		env PATH="$base/bin:$PATH" \
			MCSD_PG_UPGRADE_DIR="$base/out" \
			MOCK_LOG="$base/docker.log" \
			"$@" bash "$SCRIPT" 2>&1
	)" || exit_code=$?
	log="$(cat "$base/docker.log" 2>/dev/null || true)"
}

# Nothing irreversible has happened: the stack was not taken down, the volume
# was not removed, and no archive was written.
assert_nothing_destructive() {
	local label="$1" base="$2"
	case "$log" in
		*"compose down"*) fail_test "$label: took the stack down -- $log" ;;
		*"volume rm"*)    fail_test "$label: removed the db-data volume -- $log" ;;
		*)                ok "$label: the original volume is untouched" ;;
	esac
}

# ---------------------------------------------------------------------------
echo "=== pg_major_upgrade tests ==="

# --- 1. Nothing pending: the volume already holds the target major ---
# Re-running after a successful upgrade must detect there is nothing to do
# rather than start over.
{
	base="$(make_fixture postgres:18 postgres:18)"
	run_upgrade "$base" MOCK_PG_VERSION=18
	if [ "$exit_code" -eq 0 ]; then
		ok "no upgrade pending: exits 0"
	else
		fail_test "no upgrade pending: expected exit 0, got $exit_code -- $output"
	fi
	case "$output" in
		*"nothing to do"*) ok "no upgrade pending: says so" ;;
		*) fail_test "no upgrade pending: no explanation -- $output" ;;
	esac
	assert_nothing_destructive "no upgrade pending" "$base"
	rm -rf "$base"
}

# --- 2. No db-data volume at all -- nothing to migrate ---
{
	base="$(make_fixture)"
	run_upgrade "$base" MOCK_VOLUME_EXISTS=0
	if [ "$exit_code" -eq 0 ]; then
		ok "no db-data volume: exits 0"
	else
		fail_test "no db-data volume: expected exit 0, got $exit_code -- $output"
	fi
	assert_nothing_destructive "no db-data volume" "$base"
	rm -rf "$base"
}

# --- 3. The volume exists but holds no cluster -- nothing to migrate ---
{
	base="$(make_fixture)"
	run_upgrade "$base" MOCK_PG_VERSION=
	if [ "$exit_code" -eq 0 ]; then
		ok "volume without a cluster: exits 0"
	else
		fail_test "volume without a cluster: expected exit 0, got $exit_code -- $output"
	fi
	assert_nothing_destructive "volume without a cluster" "$base"
	rm -rf "$base"
}

# --- 4. The cluster is NEWER than the target -- not a case this handles ---
{
	base="$(make_fixture postgres:18 postgres:17)"
	run_upgrade "$base" MOCK_PG_VERSION=18 MOCK_RUNNING_MAJOR=18
	if [ "$exit_code" -ne 0 ]; then
		ok "cluster newer than the target: refused"
	else
		fail_test "cluster newer than the target: expected refusal, got exit 0 -- $output"
	fi
	assert_nothing_destructive "cluster newer than the target" "$base"
	rm -rf "$base"
}

# --- 5. The running db is ALREADY the new major -- refuse before dumping ---
# The ordering constraint the whole script hangs on: postgres:18 ships no
# PostgreSQL 17 binary, so a dump taken after the image swap is not a dump of
# this cluster. Refuse rather than produce one.
{
	base="$(make_fixture)"
	run_upgrade "$base" MOCK_PG_VERSION=17 MOCK_RUNNING_MAJOR=18
	if [ "$exit_code" -ne 0 ]; then
		ok "running db is the new major: refused"
	else
		fail_test "running db is the new major: expected refusal, got exit 0 -- $output"
	fi
	case "$log" in
		*"dump "*) fail_test "running db is the new major: dumped anyway -- $log" ;;
		*) ok "running db is the new major: no dump was taken" ;;
	esac
	assert_nothing_destructive "running db is the new major" "$base"
	rm -rf "$base"
}

# --- 6. pg_dumpall exits non-zero -- abort with the volume intact ---
{
	base="$(make_fixture)"
	run_upgrade "$base" MOCK_PG_VERSION=17 MOCK_DUMP_MODE=fail
	if [ "$exit_code" -ne 0 ]; then
		ok "pg_dumpall fails: refused"
	else
		fail_test "pg_dumpall fails: expected refusal, got exit 0 -- $output"
	fi
	assert_nothing_destructive "pg_dumpall fails" "$base"
	rm -rf "$base"
}

# --- 7. A truncated dump -- non-empty, plausible, no completion marker ---
# The failure mode #2304 was opened for: pg_dumpall exits 0 into a file that
# stops mid-COPY. Checking that a file appeared is not verification.
{
	base="$(make_fixture)"
	run_upgrade "$base" MOCK_PG_VERSION=17 MOCK_DUMP_MODE=truncated
	if [ "$exit_code" -ne 0 ]; then
		ok "truncated dump: refused"
	else
		fail_test "truncated dump: expected refusal, got exit 0 -- $output"
	fi
	assert_nothing_destructive "truncated dump" "$base"
	rm -rf "$base"
}

# --- 8. The archive is no good -- abort before the volume is released ---
# Three shapes, because "an archive file exists" is as weak a claim as "a dump
# file exists": tar can fail outright, exit 0 over unreadable bytes, or produce
# a perfectly readable archive of the wrong thing.
for tar_mode in fail corrupt empty; do
	base="$(make_fixture)"
	run_upgrade "$base" MOCK_PG_VERSION=17 "MOCK_TAR_MODE=$tar_mode"
	if [ "$exit_code" -ne 0 ]; then
		ok "archive ($tar_mode): refused"
	else
		fail_test "archive ($tar_mode): expected refusal, got exit 0 -- $output"
	fi
	case "$log" in
		*"volume rm"*) fail_test "archive ($tar_mode): removed the volume anyway -- $log" ;;
		*) ok "archive ($tar_mode): the original volume is untouched" ;;
	esac
	rm -rf "$base"
done

# --- 9. The happy path, end to end ---
{
	base="$(make_fixture)"
	run_upgrade "$base" MOCK_PG_VERSION=17
	if [ "$exit_code" -eq 0 ]; then
		ok "happy path: exits 0"
	else
		fail_test "happy path: expected exit 0, got $exit_code -- $output"
	fi

	# The invariant: a readable copy of the data that is not the file just
	# written exists at every instant. The archive is what holds it once the
	# volume goes.
	case "$log" in
		*"volume rm testproj_db-data archive_present=yes"*)
			ok "happy path: the archive exists before the volume is released" ;;
		*)
			fail_test "happy path: no archive when the volume was released -- $log" ;;
	esac

	# Order: dump -> down -> archive -> volume rm -> up --wait db -> drop the
	# freshly initdb'd database -> restore.
	order="$(printf '%s\n' "$log" | sed -n \
		-e 's/^dump .*/dump/p' \
		-e 's/^compose down.*/down/p' \
		-e 's/^archive .*/archive/p' \
		-e 's/^volume rm .*/rm/p' \
		-e 's/^compose up.*/up/p' \
		-e 's/^dropdb .*/dropdb/p' \
		-e 's/^restore .*/restore/p' | tr '\n' ' ')"
	case "$order" in
		"dump down archive rm up dropdb restore "*)
			ok "happy path: dump, archive, release, restore -- in that order" ;;
		*)
			fail_test "happy path: wrong order -- '$order' from $log" ;;
	esac

	# Without -T Compose allocates a TTY and mangles the redirected SQL.
	case "$log" in
		*"exec -T db pg_dumpall -U"*) ok "happy path: the dump uses -T" ;;
		*) fail_test "happy path: the dump has no -T -- $log" ;;
	esac
	case "$log" in
		*"restore compose exec -T db psql"*) ok "happy path: the restore uses -T" ;;
		*) fail_test "happy path: the restore has no -T -- $log" ;;
	esac

	# --wait, so the restore cannot race initdb on the fresh volume.
	case "$log" in
		*"compose up -d --wait db"*) ok "happy path: waits for the new db to be healthy" ;;
		*) fail_test "happy path: did not wait for the new db -- $log" ;;
	esac

	# The restore has to run against the NEW major, which means the checkout
	# must be on the incoming revision by then.
	if grep -q 'postgres:18' "$base/repo/compose.yaml"; then
		ok "happy path: the checkout is on the incoming revision"
	else
		fail_test "happy path: the checkout was never advanced to origin/main"
	fi

	# Both artifacts survive the run, and the operator is told where they are
	# and that removing them is their call.
	if ls "$base"/out/*.sql > /dev/null 2>&1; then
		ok "happy path: the dump is kept"
	else
		fail_test "happy path: no dump left behind"
	fi
	if ls "$base"/out/*.tar.gz > /dev/null 2>&1; then
		ok "happy path: the volume archive is kept"
	else
		fail_test "happy path: no volume archive left behind"
	fi
	case "$output" in
		*"$base/out"*) ok "happy path: the report names the artifact directory" ;;
		*) fail_test "happy path: the report hides the artifacts -- $output" ;;
	esac

	# psql exits 0 after a statement that errored unless this is set, so without
	# it a failed COPY leaves a half-restored database reported as a success.
	case "$log" in
		*"restore compose exec -T db psql -v ON_ERROR_STOP=1"*)
			ok "happy path: the restore runs with ON_ERROR_STOP=1" ;;
		*)
			fail_test "happy path: the restore tolerates statement errors -- $log" ;;
	esac

	# The two collisions with what the new container's initdb already did are
	# removed rather than tolerated -- that is what lets ON_ERROR_STOP stay on.
	fed="$(cat "$base/out/restore-stdin.sql" 2>/dev/null || true)"
	if printf '%s\n' "$fed" | grep -qxF 'CREATE ROLE mcsd;'; then
		fail_test "happy path: the bootstrap role's CREATE was fed to psql -- it errors"
	else
		ok "happy path: the bootstrap role's CREATE is filtered out"
	fi
	# ...and only that one line: the ALTER carries the attributes and password.
	if printf '%s\n' "$fed" | grep -q '^ALTER ROLE mcsd WITH'; then
		ok "happy path: the bootstrap role's ALTER still runs"
	else
		fail_test "happy path: the role's attributes were filtered away too -- $fed"
	fi
	if printf '%s\n' "$fed" | grep -q '^CREATE ROLE reporter;'; then
		ok "happy path: other roles are untouched"
	else
		fail_test "happy path: a non-bootstrap role was filtered out -- $fed"
	fi
	if printf '%s\n' "$fed" | grep -q '^CREATE DATABASE mcsd WITH'; then
		ok "happy path: the dump's own CREATE DATABASE runs (original encoding/locale)"
	else
		fail_test "happy path: CREATE DATABASE was filtered out -- $fed"
	fi
	rm -rf "$base"
}

# --- 9b. The restore itself fails -- non-zero exit, and say it is partial ---
# psql erroring out mid-restore is the case ON_ERROR_STOP=1 exists to produce;
# reporting success here is the worst outcome this script can have.
{
	base="$(make_fixture)"
	run_upgrade "$base" MOCK_PG_VERSION=17 MOCK_RESTORE_FAILS=1
	if [ "$exit_code" -ne 0 ]; then
		ok "restore fails: exits non-zero"
	else
		fail_test "restore fails: expected a non-zero exit, got 0 -- $output"
	fi
	case "$output" in
		*PARTIALLY*) ok "restore fails: warns the database is partially restored" ;;
		*) fail_test "restore fails: no partial-restore warning -- $output" ;;
	esac
	# The two copies of the data must still be named as recoverable.
	case "$output" in
		*.tar.gz*) ok "restore fails: points at the surviving archive" ;;
		*) fail_test "restore fails: does not point at the archive -- $output" ;;
	esac
	rm -rf "$base"
}

# --- 9c. Dropping the freshly initdb'd database fails -- abort ---
{
	base="$(make_fixture)"
	run_upgrade "$base" MOCK_PG_VERSION=17 MOCK_DROPDB_FAILS=1
	if [ "$exit_code" -ne 0 ]; then
		ok "drop of the fresh database fails: exits non-zero"
	else
		fail_test "drop of the fresh database fails: expected a non-zero exit, got 0 -- $output"
	fi
	case "$log" in
		*"restore "*) fail_test "drop failed but the restore ran anyway -- $log" ;;
		*) ok "drop of the fresh database fails: no restore was attempted" ;;
	esac
	rm -rf "$base"
}

# --- 9d. A dump whose bootstrap role this script cannot pin down -- refuse ---
# The filter is exact and counted. A dump that does not have exactly one
# `CREATE ROLE <user>;` line is one this script has misread, and guessing would
# either feed psql a statement that errors under ON_ERROR_STOP or silently drop
# more than intended.
for dump_mode in double_role; do
	base="$(make_fixture)"
	run_upgrade "$base" MOCK_PG_VERSION=17 "MOCK_DUMP_MODE=$dump_mode"
	if [ "$exit_code" -ne 0 ]; then
		ok "dump with an unreadable bootstrap role ($dump_mode): refused"
	else
		fail_test "dump with an unreadable bootstrap role ($dump_mode): expected refusal, got 0 -- $output"
	fi
	case "$log" in
		*"restore "*) fail_test "$dump_mode: restored anyway -- $log" ;;
		*) ok "$dump_mode: no restore was attempted" ;;
	esac
	rm -rf "$base"
done

# --- 9e. A dump with no CREATE DATABASE for POSTGRES_DB -- do not drop it ---
# The drop exists solely to make room for the dump's own CREATE DATABASE. With
# no such statement there is nothing to make room for, and dropping would throw
# away the only database there is.
{
	base="$(make_fixture)"
	run_upgrade "$base" MOCK_PG_VERSION=17 MOCK_DUMP_MODE=no_createdb
	if [ "$exit_code" -eq 0 ]; then
		ok "dump without CREATE DATABASE: still completes"
	else
		fail_test "dump without CREATE DATABASE: expected exit 0, got $exit_code -- $output"
	fi
	case "$log" in
		*dropdb*) fail_test "dump without CREATE DATABASE: dropped it anyway -- $log" ;;
		*) ok "dump without CREATE DATABASE: the database is left alone" ;;
	esac
	rm -rf "$base"
}

# --- 10. A dirty working tree -- refuse (the run pulls origin/main) ---
{
	base="$(make_fixture)"
	echo "stray" > "$base/repo/stray.txt"
	run_upgrade "$base" MOCK_PG_VERSION=17
	if [ "$exit_code" -ne 0 ]; then
		ok "dirty working tree: refused"
	else
		fail_test "dirty working tree: expected refusal, got exit 0 -- $output"
	fi
	assert_nothing_destructive "dirty working tree" "$base"
	rm -rf "$base"
}

# --- 10b. The restore waits for the REAL server, not the bootstrap one -------
# `up --wait` blocks on a healthcheck that the image's temporary bootstrap
# server answers over the unix socket; that server is then shut down. Anything
# the script sends in between dies with "the database system is shutting down".
# The TCP probe is the discriminator -- the bootstrap server has
# listen_addresses='' and can never answer it.
{
	base="$(make_fixture)"
	run_upgrade "$base" MOCK_PG_VERSION=17 MOCK_TCP_READY_AFTER=3
	if [ "$exit_code" -eq 0 ]; then
		ok "bootstrap window: completes once the real server is up"
	else
		fail_test "bootstrap window: expected exit 0, got $exit_code -- $output"
	fi
	refused="$(printf '%s\n' "$log" | grep -c '^tcp-probe-refused' || true)"
	if [ "$refused" -eq 3 ]; then
		ok "bootstrap window: kept probing while TCP was refused ($refused times)"
	else
		fail_test "bootstrap window: expected 3 refused probes, saw $refused -- $log"
	fi
	# The one that matters: nothing was sent to the database during the window.
	order="$(printf '%s\n' "$log" | sed -n \
		-e 's/^compose up.*/up/p' \
		-e 's/^tcp-probe-refused.*/refused/p' \
		-e 's/^tcp-ready.*/ready/p' \
		-e 's/^dropdb .*/dropdb/p' \
		-e 's/^restore .*/restore/p' | tr '\n' ' ')"
	case "$order" in
		"up refused refused refused ready dropdb restore "*)
			ok "bootstrap window: no statement is sent before TCP is ready" ;;
		*)
			fail_test "bootstrap window: wrong order -- '$order'" ;;
	esac
	rm -rf "$base"
}

# --- 10c. An aborted run is not mistaken for a finished one on re-run --------
# The dangerous chain: the restore aborts, the operator re-runs, and the volume
# now holds the TARGET major -- indistinguishable from a completed upgrade
# unless the script left something behind saying otherwise.
{
	base="$(make_fixture)"
	run_upgrade "$base" MOCK_PG_VERSION=17 MOCK_RESTORE_FAILS=1
	if [ "$exit_code" -ne 0 ]; then
		ok "aborted run: first run exits non-zero"
	else
		fail_test "aborted run: first run unexpectedly succeeded -- $output"
	fi
	if [ -f "$base/repo/.pg-upgrade-incomplete" ]; then
		ok "aborted run: leaves the incomplete-upgrade sentinel"
	else
		fail_test "aborted run: no sentinel left behind"
	fi

	# Re-run: the volume now holds 18, exactly as a completed upgrade would.
	run_upgrade "$base" MOCK_PG_VERSION=18
	if [ "$exit_code" -ne 0 ]; then
		ok "aborted run: the re-run refuses instead of reporting success"
	else
		fail_test "aborted run: the re-run reported success -- $output"
	fi
	case "$output" in
		*"PARTIALLY RESTORED"*) ok "aborted run: the re-run says the cluster is partial" ;;
		*) fail_test "aborted run: the re-run does not warn -- $output" ;;
	esac
	case "$output" in
		*"nothing to do"*) fail_test "aborted run: the re-run still says 'nothing to do' -- $output" ;;
		*) ok "aborted run: the re-run does not say 'nothing to do'" ;;
	esac
	# It must name the unfinished run's artifacts, or the operator cannot recover.
	case "$output" in
		*.tar.gz*) ok "aborted run: the re-run names the archive to recover from" ;;
		*) fail_test "aborted run: the re-run hides the archive -- $output" ;;
	esac
	rm -rf "$base"
}

# --- 10d. A completed run clears the sentinel -------------------------------
{
	base="$(make_fixture)"
	run_upgrade "$base" MOCK_PG_VERSION=17
	if [ ! -f "$base/repo/.pg-upgrade-incomplete" ]; then
		ok "completed run: the sentinel is cleared"
	else
		fail_test "completed run: the sentinel was left behind"
	fi
	run_upgrade "$base" MOCK_PG_VERSION=18
	if [ "$exit_code" -eq 0 ]; then
		ok "completed run: the re-run is a no-op"
	else
		fail_test "completed run: the re-run failed -- $output"
	fi
	case "$output" in
		*"nothing to do"*) ok "completed run: the re-run says nothing to do" ;;
		*) fail_test "completed run: no 'nothing to do' -- $output" ;;
	esac
	rm -rf "$base"
}

# --- 10e. Failure after the volume is released prints the way back ----------
# The recovery how-to used to appear only in the SUCCESS report, which an
# operator whose run failed never reaches.
{
	base="$(make_fixture)"
	run_upgrade "$base" MOCK_PG_VERSION=17 MOCK_RESTORE_FAILS=1
	for needle in "docker volume create" "tar xzf" "git checkout" "docker compose up -d --wait db" ".pg-upgrade-incomplete"; do
		case "$output" in
			*"$needle"*) ok "failure path: recovery text includes '$needle'" ;;
			*) fail_test "failure path: recovery text lacks '$needle' -- $output" ;;
		esac
	done
	rm -rf "$base"
}

# --- 10f. An ambiguous CREATE DATABASE is refused, like the role check ------
{
	base="$(make_fixture)"
	run_upgrade "$base" MOCK_PG_VERSION=17 MOCK_DUMP_MODE=double_createdb
	if [ "$exit_code" -ne 0 ]; then
		ok "two CREATE DATABASE statements: refused"
	else
		fail_test "two CREATE DATABASE statements: expected refusal, got 0 -- $output"
	fi
	case "$log" in
		*"restore "*) fail_test "two CREATE DATABASE statements: restored anyway -- $log" ;;
		*) ok "two CREATE DATABASE statements: no restore was attempted" ;;
	esac
	rm -rf "$base"
}

# --- 11. Nothing in the repo runs this script for you ---
# A destructive, irreversible migration must not be a side effect of a routine
# deploy command (#2304). Mentioning it in a refusal message is fine; being
# executed by update.sh / deploy.sh / the Makefile is not.
{
	callers=""
	for f in "$SCRIPTS_DIR/update.sh" "$SCRIPTS_DIR/deploy.sh" "$SCRIPTS_DIR/../Makefile"; do
		# `make scripts-test` legitimately names THIS file; that is not a caller.
		if grep 'pg_major_upgrade' "$f" 2>/dev/null | grep -qv 'test_pg_major_upgrade'; then
			callers="$callers $f"
		fi
	done
	if [ -z "$callers" ]; then
		ok "no deploy path invokes the upgrade script"
	else
		fail_test "the upgrade script is invoked from:$callers"
	fi
}

# ---------------------------------------------------------------------------
echo
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
