#!/usr/bin/env bash
#
# pg_major_upgrade.sh: migrate the db-data volume to the PostgreSQL major the
# incoming revision deploys -- dump, verify, archive, fresh volume, restore
# (issue #2304).
#
# DELIBERATELY INVOKED. Nothing in this repo calls it: not scripts/update.sh,
# not `make update`, not scripts/deploy.sh. The API is unavailable for the whole
# run and the volume swap is irreversible, so an unattended partial failure is
# worse than a supervised one. `scripts/deploy_preflight.sh` refuses the deploy
# and names this script; the operator runs it when they have chosen to, inside a
# maintenance window.
#
# The three properties worth auditing before you trust it with your database:
#
#   * The dump is taken while the OLD major is still what `db` runs. A
#     postgres:18 image ships no PostgreSQL 17 binary, so a dump taken after the
#     image swap is not a dump of this cluster at all. The run refuses unless the
#     running container's pg_dumpall is the major that wrote the volume.
#   * The dump is verified before anything destructive happens: pg_dumpall's own
#     exit status (not that a file appeared), plus PostgreSQL's end-of-dump
#     marker. Either check failing aborts with the volume untouched.
#   * A readable copy of the data that is not the file just written exists at
#     every instant. The old volume is archived to a host path and the archive is
#     listed back BEFORE `docker volume rm` runs, so the moment the volume goes
#     there are two independently verified copies, not one.
#   * The restore runs under ON_ERROR_STOP=1, so a statement that errors halfway
#     through exits non-zero instead of reporting a success the operator would
#     then bring the stack up on. See the comment above the restore for the two
#     collisions that are removed rather than tolerated.
#
# Usage:
#   scripts/pg_major_upgrade.sh
#
# Artifacts (dump, volume archive, restore log) land in $MCSD_PG_UPGRADE_DIR,
# defaulting to a timestamped directory NEXT TO the repo -- never inside it, as
# the deploy preflight refuses a dirty working tree. Removing them once satisfied
# is the operator's call; this script never does.
set -euo pipefail

# Resolved with builtins only, matching scripts/deploy_preflight.sh.
script_dir="${BASH_SOURCE[0]%/*}"
[ "$script_dir" = "${BASH_SOURCE[0]}" ] && script_dir="."
script_dir="$(cd "$script_dir" && pwd)"

say() { echo "pg upgrade: $*"; }
die() {
	echo "pg upgrade: $*" >&2
	exit 1
}

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || die "not a git checkout (run from the repo root)."
cd "$repo_root"

# Written just before the old volume is released and removed once the restore
# has succeeded, so its presence means "a previous run got into the destructive
# phase and did not come out". Without it a re-run cannot tell a COMPLETED
# upgrade from a new cluster left PARTIALLY RESTORED: both are simply "the
# volume holds PostgreSQL <target>", and reporting "nothing to do" for the
# second is how an operator ends up bringing the stack up on half a database.
# Lives in the repo root and is gitignored, matching update.sh's .last-deploy-sha
# -- a fixed path both runs compute identically, and one `git status` ignores.
sentinel="$repo_root/.pg-upgrade-incomplete"

# shellcheck source=scripts/pg_cluster_lib.sh
. "$script_dir/pg_cluster_lib.sh"

# ── 1. Preconditions ─────────────────────────────────────────────────────────
# This run advances the checkout to origin/main (step 5) -- it has to, because
# the restore must land in a container started from the NEW image. So the same
# two conditions the deploy preflight enforces apply here: a pull onto a stray
# branch would merge, and a dirty tree would block the fast-forward halfway
# through a migration.
branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || echo "")"
if [ "$branch" != "main" ]; then
	die "checkout is on '${branch:-a detached HEAD}', not 'main'. This run fast-forwards to origin/main; restore it first: git checkout main"
fi
if [ -n "$(git status --porcelain)" ]; then
	git status --short >&2
	die "working tree is dirty. This run fast-forwards to origin/main; commit, stash, or discard the changes above first."
fi

# ── 2. Is an upgrade actually pending? ───────────────────────────────────────
# Same facts, same source, as the guard that sends operators here: the target
# comes from origin/main, because that is the revision whose `db` image the
# restore has to land in (scripts/pg_cluster_lib.sh explains why not the tree).
pg_resolve_compose_facts || die "$pg_reason"
[ -n "$pg_db_user" ] || die "POSTGRES_USER is not set for the db service -- the dump and restore need it (see .env)."
[ -n "$pg_db_name" ] || die "POSTGRES_DB is not set for the db service -- the restore needs it (see .env)."

probe_status=0
pg_probe_cluster_major "$pg_volume_name" "$pg_db_image" || probe_status=$?
if [ "$probe_status" -eq 1 ]; then
	die "$pg_reason"
fi

# ── 2b. Did a previous run leave this volume mid-migration? ──────────────────
# The sentinel records that a PREVIOUS run entered the destructive phase and did
# not come out. That is a fact about that run, not about what the volume holds
# now, so it is read BEFORE this run branches on the volume's contents. The
# destructive phase releases the volume and then waits on compose to recreate it
# and initdb to populate it; a crash in either gap (kill -9, a reboot -- nothing
# a trap could catch) leaves the volume missing, or present and empty. Both of
# those reach a "nothing to do" exit below, which is the most dangerous thing
# this script can say to an operator whose running data is at that instant only
# in the dump and archive the sentinel names.
#
# A genuine major mismatch is the one state that is NOT ambiguous: the volume
# still holds the old cluster, so no previous run can have got past the removal,
# and the upgrade this script exists for is still pending. That case proceeds
# whatever the marker says -- which is also what keeps a stale marker from
# refusing every future upgrade, since a successful run clears it.
refuse_unfinished_run() {
	local headline="$1" line
	shift
	echo "pg upgrade: ${headline}" >&2
	for line in "$@"; do
		echo "  ${line}" >&2
	done
	echo "  The unfinished run recorded:" >&2
	sed 's/^/    /' "$sentinel" >&2
	echo "  Recover from that run's archive (below), or re-restore its dump by hand; then delete" >&2
	echo "  ${sentinel} once the cluster is good again." >&2
	exit 1
}
if [ -f "$sentinel" ]; then
	# Nothing was restored in either of the first two: saying "PARTIALLY
	# RESTORED" there would send the operator hunting for a half-written cluster
	# that does not exist. What is true is simpler and worse.
	if [ "$probe_status" -eq 2 ]; then
		refuse_unfinished_run \
			"volume '${pg_volume_name}' does not exist, but a previous run of this script did not finish." \
			"That run released the volume and never got a replacement into place. NOTHING has been" \
			"restored: this deployment has no database, and the only copies of its data are the dump" \
			"and archive recorded below. Do not deploy -- that would start the stack on an EMPTY volume."
	elif [ -z "$pg_cluster_major" ]; then
		refuse_unfinished_run \
			"volume '${pg_volume_name}' holds no PostgreSQL cluster, but a previous run of this script did not finish." \
			"That run released the old volume and its replacement never finished initializing. NOTHING" \
			"has been restored: the only copies of this deployment's data are the dump and archive" \
			"recorded below. Do not deploy -- that would start the stack on an EMPTY volume."
	elif [ "$pg_cluster_major" -eq "$pg_target_major" ]; then
		refuse_unfinished_run \
			"volume '${pg_volume_name}' holds PostgreSQL ${pg_cluster_major}, but a previous run of this script did not finish restoring into it." \
			"That cluster is PARTIALLY RESTORED. Do not bring the stack up on it."
	fi
fi

if [ "$probe_status" -eq 2 ]; then
	say "volume '${pg_volume_name}' does not exist -- nothing to do (the next deploy creates it at PostgreSQL ${pg_target_major})."
	exit 0
fi
if [ -z "$pg_cluster_major" ]; then
	say "volume '${pg_volume_name}' holds no PostgreSQL cluster -- nothing to do."
	exit 0
fi
# The re-run case: after a successful upgrade the volume already matches, so a
# second invocation stops here instead of starting over. What makes that safe is
# the sentinel check above -- without it this exit cannot tell a COMPLETED
# upgrade from the wreckage of one that died halfway.
if [ "$pg_cluster_major" -eq "$pg_target_major" ]; then
	say "volume '${pg_volume_name}' already holds PostgreSQL ${pg_cluster_major}, which is what origin/main deploys (${pg_db_image}) -- nothing to do."
	exit 0
fi
if [ "$pg_cluster_major" -gt "$pg_target_major" ]; then
	die "volume '${pg_volume_name}' holds PostgreSQL ${pg_cluster_major}, NEWER than the ${pg_target_major} that origin/main deploys (${pg_db_image}). Downgrading a cluster is not something this script handles; check what origin/main pins before going further."
fi

say "PostgreSQL ${pg_cluster_major} -> ${pg_target_major} upgrade of volume '${pg_volume_name}' (origin/main deploys ${pg_db_image})."

# ── 3. The running db must still BE the old major ────────────────────────────
# The one constraint the whole script hangs on. Ask the binary that will take
# the dump, not the compose file that was supposed to have started it.
running_version="$(pg_docker "docker compose exec -T db pg_dumpall --version" 2> /dev/null)" ||
	die "the 'db' service is not running, so there is nothing to dump. Start it from the revision that deployed PostgreSQL ${pg_cluster_major} (docker compose up -d --wait db) and re-run."
running_major="$(printf '%s' "$running_version" | sed -n 's/.*(PostgreSQL) \([0-9][0-9]*\).*/\1/p')"
[ -n "$running_major" ] || die "could not read a PostgreSQL major from the running db container's pg_dumpall: '${running_version}'."
if [ "$running_major" -ne "$pg_cluster_major" ]; then
	die "the running 'db' container is PostgreSQL ${running_major}, but volume '${pg_volume_name}' holds a PostgreSQL ${pg_cluster_major} cluster. The dump must be taken by the major that wrote the cluster -- PostgreSQL ${running_major} ships no ${pg_cluster_major} binary. Bring the db up from the revision that deployed ${pg_cluster_major} and re-run."
fi

out_dir="${MCSD_PG_UPGRADE_DIR:-${repo_root%/*}/mcsd-pg-upgrade-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$out_dir" || die "could not create the artifact directory '${out_dir}'."
out_dir="$(cd "$out_dir" && pwd)"
# The path is interpolated into a docker command string below.
case "$out_dir" in
	*[[:space:]]*) die "the artifact directory '${out_dir}' contains whitespace; set MCSD_PG_UPGRADE_DIR to a path without it." ;;
esac
say "artifacts will be written to ${out_dir}"

# ── 4. Stop the writers, then dump, then verify the dump ─────────────────────
# `api` is the only DB client (`worker` and `relay` reach it over gRPC), so
# stopping it is what makes the dump a consistent end state; the others go too
# because they only error against a stopped API. `db` stays up -- it is what
# takes the dump.
say "stopping the writers (api worker relay cloudflared)..."
pg_docker "docker compose stop api worker relay cloudflared" || die "could not stop the writer services; nothing has been changed."

backup="$out_dir/pg${pg_cluster_major}-dumpall.sql"
say "dumping the PostgreSQL ${pg_cluster_major} cluster to ${backup} ..."
dump_status=0
pg_docker "docker compose exec -T db pg_dumpall -U ${pg_db_user}" > "$backup" || dump_status=$?
if [ "$dump_status" -ne 0 ]; then
	die "pg_dumpall exited ${dump_status}. NOTHING has been changed -- volume '${pg_volume_name}' still holds the PostgreSQL ${pg_cluster_major} cluster and 'docker compose up -d' brings the stack back. The partial output is at ${backup}."
fi
# A file that is non-empty and looks plausible is not a verified dump: a broken
# pipe or a full disk leaves exactly that, and the redirection above created the
# file either way. pg_dumpall's own closing marker is the end-to-end evidence.
if ! tail -n 5 "$backup" | grep -q 'PostgreSQL database cluster dump complete'; then
	die "the dump at ${backup} does not end with PostgreSQL's completion marker, so it is truncated. NOTHING has been changed -- volume '${pg_volume_name}' still holds the PostgreSQL ${pg_cluster_major} cluster and 'docker compose up -d' brings the stack back."
fi
say "dump verified: pg_dumpall exited 0 and the output ends with PostgreSQL's completion marker."

# ── 5. Take the incoming revision ────────────────────────────────────────────
# Before anything destructive, so a failed fast-forward costs nothing: the
# volume is still there and the stack still starts. The pre-pull commit is kept
# for the recovery instructions -- going back to the old data means going back
# to the revision whose compose.yaml mounts it where PostgreSQL ${pg_cluster_major} expects.
old_revision="$(git rev-parse HEAD)"
say "fast-forwarding the checkout to origin/main..."
git pull --ff-only origin main || die "git pull --ff-only origin main failed. Nothing destructive has happened -- volume '${pg_volume_name}' is intact and 'docker compose up -d' brings the stack back."

# ── 6. Archive the old volume, and verify the archive ────────────────────────
# `down` first, so the archive is a quiesced cluster rather than a torn copy of
# a running one. `down` does not remove volumes.
say "stopping the stack so the volume is archived quiesced..."
pg_docker "docker compose down" || die "could not stop the stack; volume '${pg_volume_name}' is intact."

archive_name="pg${pg_cluster_major}-${pg_volume_name}.tar.gz"
archive="$out_dir/$archive_name"
say "archiving volume '${pg_volume_name}' to ${archive} ..."
pg_docker "docker run --rm --entrypoint sh -v ${pg_volume_name}:/src:ro -v ${out_dir}:/out ${pg_db_image} -c 'tar czf /out/${archive_name} -C /src .'" ||
	die "could not archive volume '${pg_volume_name}'. It is still intact and nothing has been removed -- free some space under ${out_dir} (or point MCSD_PG_UPGRADE_DIR elsewhere) and re-run."
# Listing the archive back decompresses it end to end, which is what turns "a
# file appeared" into "the bytes are readable and hold a cluster". This is the
# last check before the volume is released, and the reason it can be released.
archive_listing="$(tar tzf "$archive")" || die "the archive at ${archive} is unreadable. Volume '${pg_volume_name}' is still intact -- nothing has been removed."
case "$archive_listing" in
	*PG_VERSION*) ;;
	*) die "the archive at ${archive} contains no PG_VERSION, so it is not a PostgreSQL cluster. Volume '${pg_volume_name}' is still intact -- nothing has been removed." ;;
esac
say "archive verified: ${archive} lists back as a PostgreSQL cluster."

# Everything from here can leave the deployment without a working database, so
# every failure below prints the way back rather than just naming the two files.
# `docker volume rm` tolerates a missing volume with `|| true` so the block can
# be followed verbatim whether the failure landed before or after step 7.
#
# The MCSD_ALLOW_PRIMARY_BRANCH=1 prefix on the checkout is load-bearing, not
# decoration. On the canonical host the post-checkout hook auto-restores this
# checkout to main (.githooks/post-checkout, docs/dev/AGENTS.md Section 1), so a
# plain `git checkout <old revision>` is silently undone and the very next line
# brings the NEW major up on the OLD data -- the entrypoint abort this whole
# script exists to avoid, in the middle of the incident this block is for.
print_recovery() {
	echo "pg upgrade:" >&2
	echo "pg upgrade: To put the PostgreSQL ${pg_cluster_major} data back exactly as it was, run these in ${repo_root}:" >&2
	echo "pg upgrade:   sg docker -c 'docker compose down'" >&2
	echo "pg upgrade:   sg docker -c 'docker volume rm ${pg_volume_name}' || true" >&2
	echo "pg upgrade:   sg docker -c 'docker volume create ${pg_volume_name}'" >&2
	echo "pg upgrade:   sg docker -c \"docker run --rm --entrypoint sh -v ${pg_volume_name}:/dst -v ${out_dir}:/src:ro ${pg_db_image} -c 'tar xzf /src/${archive_name} -C /dst'\"" >&2
	echo "pg upgrade:   MCSD_ALLOW_PRIMARY_BRANCH=1 git checkout ${old_revision}   # the revision that deployed PostgreSQL ${pg_cluster_major}" >&2
	echo "pg upgrade:   sg docker -c 'docker compose up -d --wait db'" >&2
	echo "pg upgrade:   rm ${sentinel}" >&2
	echo "pg upgrade: That restores the volume from the archive and starts the old major against it." >&2
	echo "pg upgrade: Compose warns that the volume 'was not created by Docker Compose' -- expected, and harmless." >&2
	echo "pg upgrade: MCSD_ALLOW_PRIMARY_BRANCH=1 is not optional on that checkout: without it this repo's" >&2
	echo "pg upgrade: post-checkout hook restores the checkout to main (docs/dev/AGENTS.md Section 1), and the" >&2
	echo "pg upgrade: 'up -d --wait db' above would then start PostgreSQL ${pg_target_major} on the PostgreSQL ${pg_cluster_major} data it just" >&2
	echo "pg upgrade: put back. With the override the hook only prints a loud NOTICE -- expected. As a command" >&2
	echo "pg upgrade: prefix it applies to that one checkout, which is left on a detached HEAD; 'git checkout main'" >&2
	echo "pg upgrade: (no override needed) before re-running this script." >&2
	echo "pg upgrade: The dump (${backup}) is the second, independent copy if the archive ever fails." >&2
}
die_recoverable() {
	echo "pg upgrade: $*" >&2
	print_recovery
	exit 1
}

# ── 7. Release the old volume ────────────────────────────────────────────────
# From here on the data lives in two places that are not this volume: the
# verified dump and the verified archive. The sentinel goes down FIRST, so it is
# present for every instant in which this deployment has no working database --
# including a crash or a Ctrl-C, which no trap would have to catch.
{
	echo "started      $(date -u +%Y-%m-%dT%H:%M:%SZ)"
	echo "upgrading    PostgreSQL ${pg_cluster_major} -> ${pg_target_major}"
	echo "volume       ${pg_volume_name}"
	echo "dump         ${backup}"
	echo "archive      ${archive}"
	echo "old revision ${old_revision}"
} > "$sentinel"

say "removing volume '${pg_volume_name}' (the verified dump and archive are the surviving copies)..."
pg_docker "docker volume rm ${pg_volume_name}" || die_recoverable "could not remove volume '${pg_volume_name}'. The dump (${backup}) and archive (${archive}) are both good."

# ── 8. Restore into a healthy new cluster ────────────────────────────────────
say "starting PostgreSQL ${pg_target_major} on a fresh volume..."
pg_docker "docker compose up -d --wait db" || die_recoverable "the PostgreSQL ${pg_target_major} db did not become healthy. The dump (${backup}) and archive (${archive}) are intact; see 'docker compose logs db'."

# `--wait` is NOT sufficient on its own, and the comment that used to claim it
# was is the reason this is here. It blocks on the compose healthcheck, which is
# `pg_isready` over the container's UNIX SOCKET -- and the official image's
# entrypoint starts a TEMPORARY server on that same socket to run its init
# scripts before shutting it down and starting the real one. On the fresh volume
# step 7 just created, `--wait` therefore returns while the cluster is still
# bootstrapping, and the restore dies a few statements in with
# "FATAL: the database system is shutting down". Measured on this image:
# pg_isready answered over the socket from t=1.6s while TCP stayed closed until
# t=10.6s -- a nine-second window in which --wait is satisfied and the server it
# is satisfied by is about to be killed.
#
# That temp server is started with `listen_addresses=''` (docker-entrypoint.sh,
# docker_temp_server_start), so it can never answer over TCP. Probing TCP is
# therefore a STRUCTURAL distinction between the bootstrap server and the real
# one rather than a guess about how long bootstrapping takes -- no consecutive
# -success counting, no sleep long enough to "probably" be safe.
say "waiting for the real server (the image's bootstrap server answers the healthcheck but not TCP)..."
db_ready=0
for _ in $(seq 1 120); do
	if pg_docker "docker compose exec -T db pg_isready -q -h 127.0.0.1 -U ${pg_db_user} -d postgres" > /dev/null 2>&1; then
		db_ready=1
		break
	fi
	sleep 1
done
if [ "$db_ready" -ne 1 ]; then
	die_recoverable "PostgreSQL ${pg_target_major} never began accepting TCP connections (waited 120s). The dump (${backup}) and archive (${archive}) are intact; see 'docker compose logs db'."
fi

# psql exits 0 after a statement that errored unless ON_ERROR_STOP is set, so
# without it a failed COPY leaves a table that exists and is missing rows, and
# this script reports success. Exactly two statements in a pg_dumpall output
# collide with what the new container's initdb has already done from .env:
#
#     CREATE ROLE <POSTGRES_USER>;
#     CREATE DATABASE <POSTGRES_DB> WITH TEMPLATE = ...;
#
# Both are removed as errors rather than tolerated as ones, so ON_ERROR_STOP=1
# can stay on for everything else:
#
#   * the database is dropped below, which also makes the restore more faithful
#     -- the dump's own CREATE DATABASE then rebuilds it with the OLD cluster's
#     encoding, locale and collation provider instead of this image's initdb
#     defaults;
#   * the role cannot be dropped (it is the role we connect as), so its one
#     CREATE line is filtered out of the stream. pg_dumpall emits
#     `CREATE ROLE x;` followed by `ALTER ROLE x WITH ...` precisely so the
#     CREATE is free to fail -- the ALTER carries the attributes and the
#     password, and it still runs.
#
# Nothing here matches psql's error text: those strings are locale-dependent and
# have changed between majors, so an allowlist of them would widen silently. The
# two statements are identified by name, and the count is asserted -- a dump this
# script cannot read exactly is a dump it must not guess at.
role_stmt="CREATE ROLE ${pg_db_user};"
role_lines="$(grep -c -x -F -- "$role_stmt" "$backup" || true)"
if [ "$role_lines" != "1" ]; then
	die_recoverable "expected exactly one '${role_stmt}' line in ${backup}, found ${role_lines}. Refusing to restore a dump whose bootstrap role this script cannot identify."
fi
# Counted, not merely detected, so this refuses on an ambiguous dump exactly as
# the role check above does. pg_dumpall emits at most one CREATE DATABASE per
# database, so anything above 1 means this script is misreading the file.
createdb_lines="$(awk -v db="$pg_db_name" '$1 == "CREATE" && $2 == "DATABASE" && $3 == db { n++ } END { print n + 0 }' "$backup")"
if [ "$createdb_lines" -gt 1 ]; then
	die_recoverable "found ${createdb_lines} 'CREATE DATABASE ${pg_db_name}' statements in ${backup}; expected 0 or 1. Refusing to guess which one the restore should make room for."
fi
if [ "$createdb_lines" -eq 1 ]; then
	say "dropping the empty '${pg_db_name}' this image's initdb just created, so the dump recreates it as it was..."
	pg_docker "docker compose exec -T db psql -v ON_ERROR_STOP=1 -U ${pg_db_user} -d postgres -c 'DROP DATABASE IF EXISTS ${pg_db_name}'" ||
		die_recoverable "could not drop the freshly created '${pg_db_name}'."
fi

restore_log="$out_dir/restore.log"
say "restoring the dump into the new cluster (ON_ERROR_STOP=1 -- any unexpected error aborts)..."
restore_status=0
grep -v -x -F -- "$role_stmt" "$backup" |
	pg_docker "docker compose exec -T db psql -v ON_ERROR_STOP=1 -U ${pg_db_user} -d postgres" 2>&1 |
	tee "$restore_log" || restore_status=$?
if [ "$restore_status" -ne 0 ]; then
	die_recoverable "the restore failed (exit ${restore_status}); see ${restore_log} for the statement that errored. The database is now PARTIALLY restored -- do not bring the stack up on it."
fi

table_count="$(pg_docker "docker compose exec -T db psql -U ${pg_db_user} -d ${pg_db_name} -tAc \"select count(*) from pg_tables where schemaname = 'public'\"" 2> /dev/null | tr -d '[:space:]')" || table_count=""

# The restore is in; the deployment has a working database again. Clearing the
# sentinel is what makes a later re-run say "nothing to do" instead of refusing.
rm -f "$sentinel"

# ── 9. Report ────────────────────────────────────────────────────────────────
echo
say "done -- volume '${pg_volume_name}' now holds a PostgreSQL ${pg_target_major} cluster restored from the PostgreSQL ${pg_cluster_major} dump."
say "  dump            ${backup}"
say "  volume archive  ${archive}"
say "  restore log     ${restore_log}"
say "  public tables in '${pg_db_name}' after the restore: ${table_count:-unknown}"
echo
say "NOT done by this script -- verify these before calling the upgrade finished:"
say "  1. Bring the rest of the stack up:  docker compose up -d --build"
say "  2. Exercise the app: log in, list servers, open a backup and a snapshot."
say "  3. Remove ${out_dir} yourself once satisfied. Nothing else will: until then it holds"
say "     the only copies of the PostgreSQL ${pg_cluster_major} data, and a bad restore can still be reverted by"
say "     unpacking the archive and pointing a throwaway postgres:${pg_cluster_major} container at it."
