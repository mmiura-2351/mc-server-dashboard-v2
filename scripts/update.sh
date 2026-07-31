#!/usr/bin/env bash
#
# update.sh: selective rebuild with change detection.
#
# Pulls the latest main, detects which components changed since the last deploy
# (via .last-deploy-sha), and rebuilds only what changed in the correct order:
# api -> relay -> worker (worker last because its restart bounces MC servers).
#
# Two separate records, because change detection and the operator need
# different facts (#2311):
#
#   .last-deploy-sha     the revision the running stack was BUILT AND STARTED
#                        from. Written as soon as `docker compose up -d`
#                        succeeds: the containers are running that revision
#                        whether or not it goes on to answer /api/healthz, and
#                        that is the base change detection must diff against.
#                        Absent means "unknown" and forces a full rebuild.
#   .last-deploy-health  whether that revision then passed the post-deploy
#                        healthcheck -- `ok` or `failed`. Absent also reads as
#                        "not verified", so an interrupted run is never
#                        mistaken for a confirmed one.
#
# Usage:
#   make update           # selective rebuild
#   make update FORCE=1   # rebuild all components unconditionally
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
	echo "update: not a git checkout." >&2
	exit 1
}
cd "$repo_root"

STAMP_FILE=".last-deploy-sha"
HEALTH_FILE=".last-deploy-health"

# ── 1. Preflight ─────────────────────────────────────────────────────────────
scripts/deploy_preflight.sh

# ── 2. Pull latest main ──────────────────────────────────────────────────────
echo "update: pulling latest main..."
git pull --ff-only origin main

# ── 3. Determine what changed ────────────────────────────────────────────────
build_api=0
build_relay=0
build_worker=0

head_sha="$(git rev-parse HEAD)"

# What is running, and was it ever confirmed healthy? An unreadable stamp is the
# same fact as a missing one -- "unknown" -- and has to be named as such here
# rather than reaching `git diff` as an empty or bogus revision (#2311).
last_sha=""
if [ -f "$STAMP_FILE" ]; then
	last_sha="$(tr -d '[:space:]' < "$STAMP_FILE")"
	if [ -n "$last_sha" ] && ! git cat-file -e "${last_sha}^{commit}" 2>/dev/null; then
		echo "update: WARNING -- ${STAMP_FILE} names '${last_sha}', which is not a commit in this checkout." >&2
		last_sha=""
	fi
fi

last_health=""
if [ -f "$HEALTH_FILE" ]; then
	last_health="$(tr -d '[:space:]' < "$HEALTH_FILE")"
fi

if [ "${FORCE:-}" = "1" ]; then
	echo "update: FORCE=1 -- rebuilding all components."
	build_api=1
	build_relay=1
	build_worker=1
elif [ -z "$last_sha" ]; then
	# No usable stamp: a first run, a deployment brought up with the plain
	# `docker compose up -d --build` of DEPLOYMENT.md Section 4, or a run that
	# could not confirm what it started. There is no base to diff from, so the
	# only honest answer is "rebuild everything" -- said out loud, because it
	# bounces every running MC server.
	echo "update: WARNING -- no usable ${STAMP_FILE}: the revision the running stack was" >&2
	echo "  built from is unknown, so there is no base to detect changes against." >&2
	echo "  Rebuilding ALL components." >&2
	build_api=1
	build_relay=1
	build_worker=1
elif [ "$last_sha" = "$head_sha" ]; then
	if [ "$last_health" = "ok" ]; then
		echo "update: already at $head_sha -- nothing to do."
		exit 0
	fi
	# The stack was started from HEAD but never answered /api/healthz. Nothing
	# in the tree changed, so nothing may be rebuilt; re-start and re-check it.
	echo "update: already at ${head_sha:0:7}, but its post-deploy healthcheck never passed."
	echo "update: nothing to rebuild -- restarting and re-verifying the running stack."
else
	echo "update: detecting changes ${last_sha:0:7}..${head_sha:0:7}..."
	changed_files="$(git diff --name-only "$last_sha" "$head_sha")"

	while IFS= read -r file; do
		case "$file" in
			api/* | webui/*)
				build_api=1
				;;
			relay/*)
				build_relay=1
				;;
			worker/*)
				build_worker=1
				;;
			proto/contract/*)
				build_api=1
				build_worker=1
				;;
			proto/mcsd/relay/*)
				build_api=1
				build_worker=1
				build_relay=1
				;;
			proto/mcsd/controlplane/*)
				build_api=1
				build_worker=1
				;;
			compose.yaml | .env.example)
				build_api=1
				build_relay=1
				build_worker=1
				;;
		esac
	done <<< "$changed_files"

	if [ "$build_api" -eq 0 ] && [ "$build_relay" -eq 0 ] && [ "$build_worker" -eq 0 ]; then
		# Nothing to rebuild: the running images already match HEAD, so HEAD is
		# what the stack was built from. Exiting here is only honest if that
		# stack was confirmed healthy; otherwise fall through and re-verify it
		# rather than report success over an API that never answered.
		if [ "$last_health" = "ok" ]; then
			echo "update: no component changes detected -- stamping and exiting."
			echo "$head_sha" > "$STAMP_FILE"
			exit 0
		fi
		echo "update: no component changes detected, but ${last_sha:0:7}'s healthcheck never passed --"
		echo "update: restarting and re-verifying the running stack."
	fi
fi

if [ "$build_api" -eq 1 ] || [ "$build_relay" -eq 1 ] || [ "$build_worker" -eq 1 ]; then
	echo "update: components to rebuild: \
$([ "$build_api" -eq 1 ] && echo -n "api " || true)\
$([ "$build_relay" -eq 1 ] && echo -n "relay " || true)\
$([ "$build_worker" -eq 1 ] && echo -n "worker " || true)"
fi

# ── 4. Build changed components (api -> relay -> worker) ─────────────────────
if [ "$build_worker" -eq 1 ]; then
	echo ""
	echo "WARNING: worker will be rebuilt -- running MC servers will bounce."
	echo ""
fi

if [ "$build_api" -eq 1 ]; then
	echo "update: building api..."
	sg docker -c "DOCKER_BUILDKIT=1 docker build --network=host -t mcsd-api:dev -f api/Dockerfile ."
fi

build_version="$(git describe --tags --always 2>/dev/null || echo 0.0.0-dev)"

if [ "$build_relay" -eq 1 ]; then
	echo "update: building relay..."
	sg docker -c "DOCKER_BUILDKIT=1 docker build --network=host --build-arg VERSION=${build_version} -t mcsd-relay:dev ./relay"
fi

if [ "$build_worker" -eq 1 ]; then
	echo "update: building worker..."
	sg docker -c "DOCKER_BUILDKIT=1 docker build --network=host --build-arg VERSION=${build_version} -t mcsd-worker:dev ./worker"
fi

# ── 5. Deploy ─────────────────────────────────────────────────────────────────
echo "update: starting services..."
if ! sg docker -c "docker compose up -d"; then
	# Compose recreates services one at a time, so a failure part-way can leave
	# some on the newly built images and some on the old ones. No single
	# revision describes that stack, so the honest record is none: clearing the
	# stamp sends the next run down the rebuild-everything path instead of
	# letting it diff from a revision that is only half-running.
	rm -f "$STAMP_FILE" "$HEALTH_FILE"
	echo "update: ERROR -- 'docker compose up -d' failed; the running stack may now be a mix" >&2
	echo "  of revisions. Cleared ${STAMP_FILE}, so the next 'make update' rebuilds every" >&2
	echo "  component rather than trusting a base that no longer describes what is running." >&2
	exit 1
fi

# ── 6. Stamp the started revision ────────────────────────────────────────────
# Before the healthcheck, deliberately: the containers are running $head_sha
# either way, and that -- not "the last revision we managed to verify" -- is
# what change detection needs. The healthcheck verdict is recorded separately
# in step 7; clearing it here means "started, not yet verified", so a run
# interrupted mid-healthcheck cannot leave the previous `ok` standing (#2311).
echo "$head_sha" > "$STAMP_FILE"
rm -f "$HEALTH_FILE"

# ── 7. Healthcheck ────────────────────────────────────────────────────────────
api_port="${API_HTTP_PORT:-8000}"
if [ -f .env ]; then
	api_port="$(grep -E '^API_HTTP_PORT=' .env | cut -d= -f2 | tr -d '[:space:]')"
	api_port="${api_port:-8000}"
fi

echo "update: waiting for API healthcheck (port ${api_port})..."
ok=0
for i in $(seq 1 30); do
	if curl -sf "http://localhost:${api_port}/api/healthz" > /dev/null 2>&1; then
		ok=1
		break
	fi
	sleep 2
done

if [ "$ok" -ne 1 ]; then
	echo "failed" > "$HEALTH_FILE"
	echo "update: ERROR -- API healthcheck failed after 60s." >&2
	echo "  The stack is running ${head_sha:0:7}; that is stamped in ${STAMP_FILE} and the" >&2
	echo "  failure in ${HEALTH_FILE}. Investigate (docker compose logs api), then re-run" >&2
	echo "  'make update' -- it will re-start and re-check without rebuilding anything." >&2
	exit 1
fi
echo "ok" > "$HEALTH_FILE"
echo "update: API healthcheck passed."
echo "update: deployed ${head_sha:0:7} successfully."
