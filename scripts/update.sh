#!/usr/bin/env bash
#
# update.sh: selective rebuild with change detection.
#
# Pulls the latest main, detects which components changed since the last deploy
# (via .last-deploy-sha), and rebuilds only what changed in the correct order:
# api -> relay -> worker (worker last because its restart bounces MC servers).
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

if [ "${FORCE:-}" = "1" ]; then
	echo "update: FORCE=1 -- rebuilding all components."
	build_api=1
	build_relay=1
	build_worker=1
elif [ ! -f "$STAMP_FILE" ]; then
	echo "update: no stamp file -- first run, rebuilding all components."
	build_api=1
	build_relay=1
	build_worker=1
else
	last_sha="$(cat "$STAMP_FILE")"
	if [ "$last_sha" = "$head_sha" ]; then
		echo "update: already at $head_sha -- nothing to do."
		exit 0
	fi

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
		echo "update: no component changes detected -- stamping and exiting."
		echo "$head_sha" > "$STAMP_FILE"
		exit 0
	fi
fi

echo "update: components to rebuild: \
$([ "$build_api" -eq 1 ] && echo -n "api " || true)\
$([ "$build_relay" -eq 1 ] && echo -n "relay " || true)\
$([ "$build_worker" -eq 1 ] && echo -n "worker " || true)"

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

if [ "$build_relay" -eq 1 ]; then
	echo "update: building relay..."
	sg docker -c "DOCKER_BUILDKIT=1 docker build --network=host -t mcsd-relay:dev ./relay"
fi

if [ "$build_worker" -eq 1 ]; then
	echo "update: building worker..."
	sg docker -c "DOCKER_BUILDKIT=1 docker build --network=host -t mcsd-worker:dev ./worker"
fi

# ── 5. Deploy ─────────────────────────────────────────────────────────────────
echo "update: starting services..."
sg docker -c "docker compose up -d"

# ── 6. Healthcheck ────────────────────────────────────────────────────────────
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
	echo "update: ERROR -- API healthcheck failed after 60s." >&2
	exit 1
fi
echo "update: API healthcheck passed."

# ── 7. Stamp deployed commit ─────────────────────────────────────────────────
echo "$head_sha" > "$STAMP_FILE"
echo "update: deployed ${head_sha:0:7} successfully."
