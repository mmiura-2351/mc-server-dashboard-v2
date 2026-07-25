#!/usr/bin/env bash
#
# deploy_preflight.sh: guard the live deployment against building the wrong ref.
#
# The primary checkout is the deploy build source -- `docker compose` builds it
# from compose.yaml + .env at the repo root (docs/dev/DEPLOYMENT.md Sections 4
# and 9). Agent sessions have repeatedly left it off `main` (#432), so a rebuild
# would silently ship a stray branch or detached HEAD. Run this before any
# `docker compose up -d --build`; it refuses (exit 1) when the checkout is not on
# `main`, when the working tree is dirty, or when the db-data volume holds a
# PostgreSQL cluster older than the major the incoming revision deploys (#2133).
set -euo pipefail

# Resolved with builtins only (no `dirname`): this runs before the PATH is known
# to be usable, and a stripped PATH must still reach the library, not vanish.
script_dir="${BASH_SOURCE[0]%/*}"
[ "$script_dir" = "${BASH_SOURCE[0]}" ] && script_dir="."
script_dir="$(cd "$script_dir" && pwd)"

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
	echo "deploy preflight: not a git checkout (run from the repo root)." >&2
	exit 1
}
cd "$repo_root"

# shellcheck source=scripts/pg_cluster_lib.sh
. "$script_dir/pg_cluster_lib.sh"

fail=0

branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || echo "")"
if [ "$branch" != "main" ]; then
	ref="${branch:-detached HEAD ($(git rev-parse --short HEAD 2>/dev/null || echo unknown))}"
	echo "deploy preflight: checkout is on '${ref}', not 'main'." >&2
	echo "  The deploy source must be 'main'. Restore it: git checkout main" >&2
	fail=1
fi

if [ -n "$(git status --porcelain)" ]; then
	echo "deploy preflight: working tree is dirty." >&2
	echo "  Commit, stash, or discard local changes before deploying:" >&2
	git status --short >&2
	fail=1
fi

# The postgres image moved PGDATA to /var/lib/postgresql/<major>/docker at major
# 18 (docker-library/postgres#1259): a :18 container aborts during entrypoint
# init when the mounted volume still holds an older cluster. The data is left
# intact, but `migrate` and `api` gate on `db` being healthy, so the whole stack
# stays down until an operator migrates it (#2133). Refuse the deploy instead.
#
# The facts come from scripts/pg_cluster_lib.sh, shared with the migration this
# refusal points at (scripts/pg_major_upgrade.sh) so the guard and the fix agree
# on what "an upgrade is pending" means. Anything the library cannot determine is
# a SKIP here, never a refusal: a false positive blocks a legitimate deploy of
# the live host. Every skip says so out loud and marks the final line, because a
# guard that quietly disappears is worse than no guard -- the operator has to be
# able to tell "checked, ok" from "could not check".
#
# Returns 1 to refuse the deploy, 0 otherwise.
pg_skipped=""

pg_skip() {
	pg_skipped=1
	echo "deploy preflight: SKIPPED the Postgres version check -- $1" >&2
}

check_postgres_major() {
	local probe_status

	if ! pg_resolve_compose_facts; then
		pg_skip "$pg_reason"
		return 0
	fi

	probe_status=0
	pg_probe_cluster_major "$pg_volume_name" "$pg_db_image" || probe_status=$?
	case "$probe_status" in
		# No volume yet = fresh deployment, nothing to be incompatible with. That
		# is a completed check, not a skip, so it stays silent.
		2) return 0 ;;
		1)
			pg_skip "$pg_reason"
			return 0
			;;
	esac

	# The volume exists but holds no cluster -- nothing to compare against.
	if [ -z "$pg_cluster_major" ]; then
		return 0
	fi

	if [ "$pg_cluster_major" -lt "$pg_target_major" ]; then
		echo "deploy preflight: volume '${pg_volume_name}' holds PostgreSQL ${pg_cluster_major} data, but origin/main's compose.yaml deploys ${pg_db_image}." >&2
		echo "  PostgreSQL ${pg_target_major} cannot read a PostgreSQL ${pg_cluster_major} cluster: the db container aborts during" >&2
		echo "  entrypoint init (your data is left untouched) and migrate/api stay down behind its healthcheck." >&2
		echo "  Migrate the data BEFORE deploying -- the dump must be taken while PostgreSQL ${pg_cluster_major} is still" >&2
		echo "  running. See docs/dev/DEPLOYMENT.md Section 9 (Upgrade)." >&2
		return 1
	fi
	return 0
}

check_postgres_major || fail=1

if [ "$fail" -ne 0 ]; then
	echo "deploy preflight: refusing to deploy." >&2
	exit 1
fi

echo "deploy preflight: on clean 'main' -- ok to build.${pg_skipped:+ (Postgres version check skipped -- see above.)}"
