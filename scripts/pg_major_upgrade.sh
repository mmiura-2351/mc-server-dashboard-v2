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
case "$probe_status" in
	2)
		say "volume '${pg_volume_name}' does not exist -- nothing to do (the next deploy creates it at PostgreSQL ${pg_target_major})."
		exit 0
		;;
	1) die "$pg_reason" ;;
esac
if [ -z "$pg_cluster_major" ]; then
	say "volume '${pg_volume_name}' holds no PostgreSQL cluster -- nothing to do."
	exit 0
fi
# The re-run case: after a successful upgrade the volume already matches, so a
# second invocation stops here instead of starting over.
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
# volume is still there and the stack still starts.
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

# ── 7. Release the old volume ────────────────────────────────────────────────
# From here on the data lives in two places that are not this volume: the
# verified dump and the verified archive.
say "removing volume '${pg_volume_name}' (the verified dump and archive are the surviving copies)..."
pg_docker "docker volume rm ${pg_volume_name}" || die "could not remove volume '${pg_volume_name}'. The dump (${backup}) and archive (${archive}) are both good; remove the volume manually and re-run."

# ── 8. Restore into a healthy new cluster ────────────────────────────────────
# `--wait` blocks on the healthcheck, so the restore cannot race initdb.
say "starting PostgreSQL ${pg_target_major} on a fresh volume..."
pg_docker "docker compose up -d --wait db" || die "the PostgreSQL ${pg_target_major} db did not become healthy. The dump (${backup}) and archive (${archive}) are intact; see 'docker compose logs db'."

restore_log="$out_dir/restore.log"
say "restoring the dump into the new cluster..."
restore_status=0
pg_docker "docker compose exec -T db psql -U ${pg_db_user} -d postgres" < "$backup" 2>&1 | tee "$restore_log" || restore_status=$?
if [ "$restore_status" -ne 0 ]; then
	die "psql exited ${restore_status} during the restore; see ${restore_log}. The dump (${backup}) and the archive (${archive}) are both intact, so this is recoverable -- do not delete them."
fi

table_count="$(pg_docker "docker compose exec -T db psql -U ${pg_db_user} -d ${pg_db_name} -tAc \"select count(*) from pg_tables where schemaname = 'public'\"" 2> /dev/null | tr -d '[:space:]')" || table_count=""

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
say "  3. Two 'already exists' errors for role/database '${pg_db_name}' in the restore log are"
say "     expected -- the fresh container provisioned both from .env before the restore ran."
say "  4. Remove ${out_dir} yourself once satisfied. Nothing else will: until then it holds"
say "     the only copies of the PostgreSQL ${pg_cluster_major} data, and a bad restore can still be reverted by"
say "     unpacking the archive and pointing a throwaway postgres:${pg_cluster_major} container at it."
