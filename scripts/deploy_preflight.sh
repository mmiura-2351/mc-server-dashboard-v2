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
# PostgreSQL cluster older than the major compose.yaml deploys (#2133).
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
	echo "deploy preflight: not a git checkout (run from the repo root)." >&2
	exit 1
}
cd "$repo_root"

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
# Docker goes through `sg docker` to match scripts/update.sh -- a session without
# active docker-group membership would otherwise fail here and block a valid
# deploy. Every step below degrades to "skip" rather than "refuse" for the same
# reason: this guard must never be the thing that stops a legitimate deploy.
compose_json="$(sg docker -c "docker compose config --format json" 2>/dev/null || true)"
if [ -z "$compose_json" ]; then
	echo "deploy preflight: cannot read the compose config -- skipping the Postgres version check." >&2
else
	target_major="$(printf '%s' "$compose_json" | python3 -c 'import json, re, sys; m = re.search(r":(\d+)", json.load(sys.stdin)["services"]["db"]["image"]); print(m.group(1) if m else "")' 2>/dev/null || true)"
	volume_name="$(printf '%s' "$compose_json" | python3 -c 'import json, sys; print(json.load(sys.stdin)["volumes"]["db-data"]["name"])' 2>/dev/null || true)"

	found_major=""
	# No volume yet = fresh deployment, nothing to be incompatible with.
	if [ -n "$target_major" ] && [ -n "$volume_name" ] && sg docker -c "docker volume inspect $volume_name" >/dev/null 2>&1; then
		# Postgres <= 17 keeps PG_VERSION at the volume root, >= 18 keeps it at
		# <major>/docker/PG_VERSION; probe both and take the oldest cluster found.
		# Read-only, so this is safe against a running db -- it is a second bind
		# mount of the same directory and Postgres holds no lock against a read.
		probe_cmd="docker run --rm -v ${volume_name}:/probedata:ro alpine sh -c 'cat /probedata/PG_VERSION /probedata/*/docker/PG_VERSION 2>/dev/null | sort -n | head -1'"
		found_major="$(sg docker -c "$probe_cmd" 2>/dev/null || true)"
	fi

	case "$found_major" in
		# Empty or non-numeric: an uninitialized volume, or a probe that could not
		# read it. Must skip -- an integer comparison against a non-number is fatal
		# under `set -e` and would wrongly block the deploy.
		'' | *[!0-9]*) ;;
		*)
			if [ "$found_major" -lt "$target_major" ]; then
				echo "deploy preflight: volume '${volume_name}' holds PostgreSQL ${found_major} data, but compose.yaml deploys postgres:${target_major}." >&2
				echo "  postgres:${target_major} cannot read a PostgreSQL ${found_major} cluster: the db container aborts during" >&2
				echo "  entrypoint init (your data is left untouched) and migrate/api stay down behind its healthcheck." >&2
				echo "  Migrate the data BEFORE deploying -- the dump must be taken while postgres:${found_major} is still" >&2
				echo "  running. See docs/dev/DEPLOYMENT.md Section 9 (Upgrade)." >&2
				fail=1
			fi
			;;
	esac
fi

if [ "$fail" -ne 0 ]; then
	echo "deploy preflight: refusing to deploy." >&2
	exit 1
fi

echo "deploy preflight: on clean 'main' -- ok to build."
