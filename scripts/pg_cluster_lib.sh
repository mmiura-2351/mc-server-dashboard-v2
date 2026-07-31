#!/usr/bin/env bash
#
# pg_cluster_lib.sh: the PostgreSQL major-version facts the deploy scripts share.
# Not executable -- `source` it (issue #2304).
#
# Two scripts need exactly the same three facts about a deployment's Postgres
# state, and they must derive them identically or the guard and the fix would
# disagree about what "an upgrade is pending" means:
#
#   scripts/deploy_preflight.sh  refuses a deploy whose db-data volume predates
#                                the major the incoming revision runs (#2133).
#   scripts/pg_major_upgrade.sh  performs the dump/restore that clears it.
#
# Which revision those facts come from is the CALLER's, because the two callers
# stand at opposite sides of the pull and neither can use the other's answer
# (#2309):
#
#   the preflight runs PRE-pull -- it guards `git pull && docker compose up`
#     (scripts/update.sh, scripts/deploy.sh), so the tree still holds the OLD
#     revision and reading it would compare the volume against the major already
#     deployed (#2303). It resolves origin/main.
#   the upgrade script runs POST-pull -- `git pull` is how the operator obtains
#     it at all, so by the time it runs, the tree IS the incoming revision. It
#     resolves HEAD, which is both what the next `docker compose up` will deploy
#     and what the restore lands in, with no second network round trip and no
#     window for a mid-run push to move the target.
#
# That asymmetry is deliberate; `pg_resolve_compose_facts` therefore takes the
# revision rather than choosing one.
#
# Failure policy is the CALLER's, never this file's: every function reports a
# failure as a non-zero return plus a human-readable sentence in `pg_reason`,
# and says nothing itself. The preflight turns that into a loud skip (it must
# never block a legitimate deploy on something it could not determine); the
# upgrade script turns it into a refusal (it must never start a destructive
# migration on a guess).

# Docker goes through `sg docker` to match scripts/update.sh -- a session
# without active docker-group membership would otherwise fail. stdin and stdout
# pass through, so callers can pipe into and redirect out of it.
pg_docker() {
	sg docker -c "$1"
}

# Resolve the revision a deploy from THIS checkout would build: origin/main,
# fetched first. Sets `pg_target_revision` (a SHA) on success; returns 1 with
# `pg_reason` set otherwise.
#
# Only the pre-pull caller (scripts/deploy_preflight.sh) needs this. Pinning to a
# SHA here, once, is what makes "the incoming revision" a single thing for the
# whole run rather than a name re-resolved at each use: main advances here often,
# including from automated merges, so a caller that resolves it twice can
# validate one revision and deploy another.
pg_resolve_incoming_revision() {
	pg_reason=""
	pg_target_revision=""

	# The fetch is the only network dependency here, so it is a reportable
	# failure like any other undeterminable input. It updates
	# refs/remotes/origin/main only: HEAD, the index and the working tree are
	# left exactly as they were.
	#
	# It also has no deadline of its own. A hard-down network fails fast and
	# reaches the failure below; a black-holed route or a stalled proxy does not,
	# and the caller blocks with it indefinitely (#2306). 120s is far longer than
	# an incremental fetch of this repo takes on the deploy host, so a slow but
	# working link is never cut short -- a skip on every deploy would make the
	# guard useless.
	#
	# `timeout` is coreutils and present on the deploy host, but its ABSENCE must
	# degrade to the unbounded fetch this has always done. Failing here instead
	# would refuse an upgrade and blind the preflight on every run, which is the
	# one thing neither caller may do over a missing convenience.
	local fetch_status=0 fetch_deadline=120 timed_out=""
	if command -v timeout > /dev/null 2>&1; then
		timeout "$fetch_deadline" git fetch --quiet origin main > /dev/null 2>&1 || fetch_status=$?
		# Only meaningful when `timeout` actually ran it: 124 is its "the command
		# was still going" status, not one git assigns.
		if [ "$fetch_status" -eq 124 ]; then
			timed_out=1
		fi
	else
		git fetch --quiet origin main > /dev/null 2>&1 || fetch_status=$?
	fi
	if [ -n "$timed_out" ]; then
		pg_reason="fetching origin/main (the revision the deploy will build) did not finish within ${fetch_deadline}s."
		return 1
	fi
	if [ "$fetch_status" -ne 0 ]; then
		pg_reason="could not fetch origin/main (the revision the deploy will build)."
		return 1
	fi

	if ! pg_target_revision="$(git rev-parse --verify --quiet "origin/main^{commit}" 2>/dev/null)"; then
		pg_reason="could not resolve origin/main to a commit after fetching it."
		return 1
	fi
	return 0
}

# Print the db facts of <revision>'s compose.yaml, one per line: major, image,
# volume name, POSTGRES_USER, POSTGRES_DB. Returns 1, silently, when that
# revision has no readable compose config -- callers phrase their own message,
# and the history walk in scripts/pg_major_upgrade.sh treats it as "not this
# candidate" rather than as an error.
#
# `-f -` hands compose the revision's compose.yaml on stdin while the project
# directory stays the repo root, so interpolation still reads the host's .env and
# the volume name still resolves under the real project name. Image and volume
# name therefore both come from the same revision; resolving one from each side
# would be worse than resolving both from either.
pg_compose_db_facts() {
	local revision="$1" compose_json
	compose_json="$(git show "${revision}:compose.yaml" 2>/dev/null | pg_docker "docker compose -f - config --format json" 2>/dev/null || true)"
	[ -n "$compose_json" ] || return 1

	# The tag is what follows the LAST colon of the final path segment, with any
	# @sha256 digest removed first: a registry host carries a :port that a
	# leftmost search would mistake for a major, and 5000 < 18 would refuse every
	# deploy. A leading integer in the tag is the major (18, 18.4, 17.6-alpine);
	# `latest` and a bare digest pin have none and must be reported, not guessed.
	printf '%s' "$compose_json" | python3 -c '
import json, re, sys
cfg = json.load(sys.stdin)
db = cfg["services"]["db"]
image = db["image"]
ref = image.rsplit("/", 1)[-1].split("@", 1)[0]
tag = ref.rsplit(":", 1)[1] if ":" in ref else ""
major = re.match(r"\d+", tag)
env = db.get("environment") or {}
print(major.group(0) if major else "")
print(image)
print(cfg["volumes"]["db-data"]["name"])
print(env.get("POSTGRES_USER") or "")
print(env.get("POSTGRES_DB") or "")
' 2>/dev/null || true
}

# Resolve the db facts of <revision>'s compose.yaml.
#
# On success (return 0) sets, interpolated against the host's own .env:
#   pg_target_major  the Postgres major that revision deploys
#   pg_db_image      the db service's image ref
#   pg_volume_name   the project-qualified name of the db-data volume
#   pg_db_user       POSTGRES_USER  (empty if .env does not set it)
#   pg_db_name       POSTGRES_DB    (empty if .env does not set it)
#
# Returns 1 with `pg_reason` set when any of the first three cannot be resolved.
# The two credentials are best-effort: only the upgrade script needs them, and
# an unset POSTGRES_USER must not cost the preflight its version check.
pg_resolve_compose_facts() {
	local revision="$1" facts
	# Explicit, because callers run under `set -u` and a short read below (an
	# empty or truncated parse) would otherwise leave the later fields unset.
	pg_reason=""
	pg_target_major=""
	pg_db_image=""
	pg_volume_name=""
	pg_db_user=""
	pg_db_name=""

	# The compose config is JSON; python3 is the only parser this repo already
	# depends on (scripts/check_docs.py et al.), but the deploy host does not
	# otherwise need it, so its absence is a real possibility and must be loud.
	if ! command -v python3 > /dev/null 2>&1; then
		pg_reason="python3 is not installed (needed to read the compose config)."
		return 1
	fi

	if ! facts="$(pg_compose_db_facts "$revision")"; then
		pg_reason="could not read the compose config of ${revision}."
		return 1
	fi
	# One field per line, so an empty major (first field) is not swallowed.
	{
		read -r pg_target_major
		read -r pg_db_image
		read -r pg_volume_name
		read -r pg_db_user
		read -r pg_db_name
	} <<< "$facts" || true

	if [ -z "$pg_db_image" ] || [ -z "$pg_volume_name" ]; then
		pg_reason="could not resolve the db image and the db-data volume name from the compose config."
		return 1
	fi
	if [ -z "$pg_target_major" ]; then
		pg_reason="no major version to read from the db image '${pg_db_image}'."
		return 1
	fi
	return 0
}

# Read the Postgres major of the cluster in a volume.
#
#   pg_probe_cluster_major <volume-name> <probe-image>
#
# Returns 2 when the volume does not exist (a fresh deployment -- a completed
# answer, not a failure), 1 with `pg_reason` set when the volume's existence or
# the cluster's version cannot be determined, and 0 otherwise with the major in
# `pg_cluster_major`.
# An EMPTY `pg_cluster_major` on return 0 means the volume exists but holds no
# cluster.
pg_probe_cluster_major() {
	local volume="$1" image="$2" probe_cmd found_major volumes
	pg_reason=""
	pg_cluster_major=""

	# `docker volume inspect` exits non-zero for "no such volume" AND for "the
	# daemon did not answer", and the two must not share an outcome (#2301): a
	# fresh host's absent volume is a completed answer, while an unanswerable
	# question is a failure the caller has to hear about -- a guard that quietly
	# disappears is worse than no guard. Listing instead splits them on the call's
	# own exit status, so the split does not depend on an error string that varies
	# by daemon version: success means the daemon answered, and membership in that
	# answer is then the fact. The match is exact, or a `<volume>-old` left on the
	# host would invent a cluster to compare against.
	if ! volumes="$(pg_docker "docker volume ls --quiet" 2>/dev/null)"; then
		pg_reason="could not ask Docker whether volume '${volume}' exists."
		return 1
	fi
	# A here-string rather than a pipe, so the membership test has no writer to
	# kill: a quiet grep stops at its first match, and on a host whose volume
	# list does not end there `printf` takes SIGPIPE and `pipefail` turns the
	# hit into a 141 -- read below as "no such volume", which is the one answer
	# #2301 exists to keep distinct (#2447). The match stays exact.
	if ! grep -qxF -- "$volume" <<< "$volumes"; then
		return 2
	fi

	# Postgres <= 17 keeps PG_VERSION at the volume root, >= 18 keeps it at
	# <major>/docker/PG_VERSION; probe both and take the oldest cluster found.
	# Read-only, so this is safe against a running db -- it is a second bind
	# mount of the same directory and Postgres holds no lock against a read.
	# Runs the db image itself with the entrypoint replaced rather than pulling a
	# helper image: no second image to version-pin and vet (DEPENDENCIES.md), and
	# `sh` replaces the very docker-entrypoint.sh whose abort this is about.
	probe_cmd="docker run --rm --entrypoint sh -v ${volume}:/probedata:ro ${image} -c 'cat /probedata/PG_VERSION /probedata/*/docker/PG_VERSION 2>/dev/null | sort -n | head -1'"
	if ! found_major="$(pg_docker "$probe_cmd" 2>/dev/null)"; then
		pg_reason="could not read PG_VERSION from volume '${volume}'."
		return 1
	fi

	case "$found_major" in
		# Unreadable PG_VERSION. Must not be compared: an integer test against a
		# non-number is fatal under `set -e` and would wrongly block a deploy.
		'') ;;
		*[!0-9]*)
			pg_reason="unreadable PG_VERSION in volume '${volume}'."
			return 1
			;;
	esac

	pg_cluster_major="$found_major"
	return 0
}
