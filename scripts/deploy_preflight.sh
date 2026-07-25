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
# active docker-group membership would otherwise fail here. Anything this check
# cannot determine is a SKIP, never a refusal: a false positive here blocks a
# legitimate deploy of the live host. Every skip says so out loud and marks the
# final line, because a guard that quietly disappears is worse than no guard --
# the operator has to be able to tell "checked, ok" from "could not check".
#
# Returns 1 to refuse the deploy, 0 otherwise.
pg_skipped=""

pg_skip() {
	pg_skipped=1
	echo "deploy preflight: SKIPPED the Postgres version check -- $1" >&2
}

check_postgres_major() {
	local compose_json pg_facts target_major db_image volume_name probe_cmd found_major

	# The compose config is JSON; python3 is the only parser this repo already
	# depends on (scripts/check_docs.py et al.), but the deploy host does not
	# otherwise need it, so its absence is a real possibility and must be loud.
	if ! command -v python3 > /dev/null 2>&1; then
		pg_skip "python3 is not installed (needed to read the compose config)."
		return 0
	fi

	# Every deploy path is preflight -> git pull -> docker compose up
	# (scripts/update.sh, scripts/deploy.sh, DEPLOYMENT.md Section 9), so when
	# this runs the working tree still holds the OLD revision. Reading its
	# compose.yaml compares the volume against the major already deployed, which
	# always passes -- right up to the `up` after the pull, which then takes the
	# stack down (#2303). The target has to come from the INCOMING revision.
	#
	# The fetch is this guard's only network dependency, so it is a skip like any
	# other undeterminable input, never a refusal. It updates
	# refs/remotes/origin/main only: HEAD, the index and the working tree are left
	# exactly as the two checks above found them.
	if ! git fetch --quiet origin main > /dev/null 2>&1; then
		pg_skip "could not fetch origin/main (the revision the deploy will build)."
		return 0
	fi

	# `-f -` hands compose the incoming compose.yaml on stdin while the project
	# directory stays the repo root, so interpolation still reads the host's .env
	# and the volume name still resolves under the real project name. Target image
	# and volume name therefore both come from the incoming revision; resolving
	# one from each side would be worse than resolving both from either.
	compose_json="$(git show origin/main:compose.yaml 2>/dev/null | sg docker -c "docker compose -f - config --format json" 2>/dev/null || true)"
	if [ -z "$compose_json" ]; then
		pg_skip "could not read the compose config of origin/main."
		return 0
	fi

	# The tag is what follows the LAST colon of the final path segment, with any
	# @sha256 digest removed first: a registry host carries a :port that a
	# leftmost search would mistake for a major, and 5000 < 18 would refuse every
	# deploy. A leading integer in the tag is the major (18, 18.4, 17.6-alpine);
	# `latest` and a bare digest pin have none and must skip, not guess.
	pg_facts="$(printf '%s' "$compose_json" | python3 -c '
import json, re, sys
cfg = json.load(sys.stdin)
image = cfg["services"]["db"]["image"]
ref = image.rsplit("/", 1)[-1].split("@", 1)[0]
tag = ref.rsplit(":", 1)[1] if ":" in ref else ""
major = re.match(r"\d+", tag)
print(major.group(0) if major else "")
print(image)
print(cfg["volumes"]["db-data"]["name"])
' 2>/dev/null || true)"
	# One field per line, so an empty major (first field) is not swallowed.
	{ read -r target_major; read -r db_image; read -r volume_name; } <<< "$pg_facts" || true

	if [ -z "$db_image" ] || [ -z "$volume_name" ]; then
		pg_skip "could not resolve the db image and the db-data volume name from the compose config."
		return 0
	fi
	if [ -z "$target_major" ]; then
		pg_skip "no major version to read from the db image '${db_image}'."
		return 0
	fi

	# No volume yet = fresh deployment, nothing to be incompatible with. That is
	# a completed check, not a skip, so it stays silent.
	if ! sg docker -c "docker volume inspect $volume_name" > /dev/null 2>&1; then
		return 0
	fi

	# Postgres <= 17 keeps PG_VERSION at the volume root, >= 18 keeps it at
	# <major>/docker/PG_VERSION; probe both and take the oldest cluster found.
	# Read-only, so this is safe against a running db -- it is a second bind
	# mount of the same directory and Postgres holds no lock against a read.
	# Runs the db image itself with the entrypoint replaced rather than pulling a
	# helper image: no second image to version-pin and vet (DEPENDENCIES.md), and
	# `sh` replaces the very docker-entrypoint.sh whose abort this check is about.
	probe_cmd="docker run --rm --entrypoint sh -v ${volume_name}:/probedata:ro ${db_image} -c 'cat /probedata/PG_VERSION /probedata/*/docker/PG_VERSION 2>/dev/null | sort -n | head -1'"
	if ! found_major="$(sg docker -c "$probe_cmd" 2>/dev/null)"; then
		pg_skip "could not read PG_VERSION from volume '${volume_name}'."
		return 0
	fi

	case "$found_major" in
		# The volume exists but holds no cluster -- nothing to compare against.
		'') return 0 ;;
		# Unreadable PG_VERSION. Must not be compared: an integer test against a
		# non-number is fatal under `set -e` and would wrongly block the deploy.
		*[!0-9]*)
			pg_skip "unreadable PG_VERSION in volume '${volume_name}'."
			return 0
			;;
	esac

	if [ "$found_major" -lt "$target_major" ]; then
		echo "deploy preflight: volume '${volume_name}' holds PostgreSQL ${found_major} data, but origin/main's compose.yaml deploys ${db_image}." >&2
		echo "  PostgreSQL ${target_major} cannot read a PostgreSQL ${found_major} cluster: the db container aborts during" >&2
		echo "  entrypoint init (your data is left untouched) and migrate/api stay down behind its healthcheck." >&2
		echo "  Migrate the data BEFORE deploying -- the dump must be taken while PostgreSQL ${found_major} is still" >&2
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
