#!/usr/bin/env bash
# Orchestrate the webui Playwright E2E run locally (issue #491).
#
# Brings up the real stack the suite needs and runs Playwright against it:
#   1. a Postgres (a throwaway Docker container, unless MCD_E2E_REUSE_DB points
#      at an already-running database via MCD_E2E_DATABASE_URL),
#   2. the API (alembic upgrade head, then uvicorn) with the control plane off
#      (no worker in CI — servers park unassigned/stopped, which the suite
#      asserts),
#   3. the seeded platform admin (register over HTTP + promote in the DB),
#   4. `playwright test`, which itself starts the Vite dev server.
#
# Everything it starts, it stops on exit. CI uses its own Postgres service and
# sets MCD_E2E_REUSE_DB=1, so there the only thing this would start is skipped.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

API_PORT="${MCD_E2E_API_PORT:-8000}"
API_URL="http://127.0.0.1:${API_PORT}"
PG_CONTAINER="mcsd-webui-e2e-pg"
PG_PORT="${MCD_E2E_PG_PORT:-5544}"
# postgres:17.6-alpine, the api.yml-vetted digest (docs/dev/DEPENDENCIES.md).
PG_IMAGE="postgres@sha256:ef257d85f76e48da1c64832459b59fcaba1a4dac97bf5d7450c77753542eee94"

started_pg=
uvicorn_pid=
FS_ROOT="$(mktemp -d)"

cleanup() {
  # uv run spawns uvicorn as a child, so kill the whole process group (we start
  # it with setsid below) rather than just the wrapper PID, which would leak the
  # uvicorn child and leave the API bound to the port across runs.
  if [ -n "$uvicorn_pid" ]; then
    kill -- "-$uvicorn_pid" 2>/dev/null || kill "$uvicorn_pid" 2>/dev/null || true
  fi
  if [ -n "$started_pg" ]; then
    docker rm -f "$PG_CONTAINER" >/dev/null 2>&1 || true
  fi
  rm -rf "$FS_ROOT"
}
trap cleanup EXIT

if [ "${MCD_E2E_REUSE_DB:-}" = "1" ]; then
  DB_URL="${MCD_E2E_DATABASE_URL:?MCD_E2E_DATABASE_URL required when MCD_E2E_REUSE_DB=1}"
else
  echo "==> starting Postgres ($PG_CONTAINER on :$PG_PORT)"
  docker rm -f "$PG_CONTAINER" >/dev/null 2>&1 || true
  docker run -d --name "$PG_CONTAINER" \
    -e POSTGRES_USER=mcsd -e POSTGRES_PASSWORD=mcsd -e POSTGRES_DB=mcsd_e2e \
    -p "${PG_PORT}:5432" "$PG_IMAGE" >/dev/null
  started_pg=1
  DB_URL="postgresql+asyncpg://mcsd:mcsd@127.0.0.1:${PG_PORT}/mcsd_e2e"
  echo "==> waiting for Postgres"
  # Probe the host-published port (not just pg_isready inside the container):
  # the published mapping can lag the in-container readiness, and a half-open
  # connection during startup shows up as a reset mid-SSL-negotiation.
  for _ in $(seq 1 60); do
    if docker exec "$PG_CONTAINER" pg_isready -U mcsd -h 127.0.0.1 >/dev/null 2>&1 \
      && (exec 3<>"/dev/tcp/127.0.0.1/${PG_PORT}") 2>/dev/null; then
      exec 3>&- 3<&- 2>/dev/null || true
      break
    fi
    sleep 1
  done
fi

export MCD_API_DATABASE__URL="$DB_URL"
export MCD_API_CONTROL__ENABLED="false"
export MCD_API_CONTROL__WORKER_CREDENTIAL="webui-e2e-worker-credential"
export MCD_API_AUTH__TOKEN__SIGNING_KEY="webui-e2e-signing-key-0123456789abcd"
export MCD_API_STORAGE__BACKEND="fs"
export MCD_API_STORAGE__FS__ROOT="$FS_ROOT"
export MCD_API_LOG__FORMAT="text"
# The suite registers several users from one host (the runner's IP), which the
# per-IP open-registration cap (default 5/hour, #362) would throttle. Disable
# just that cap for the E2E API — the flows are deliberate, not a flood.
export MCD_API_AUTH__REGISTRATION__IP_LIMIT_ENABLED="false"

echo "==> migrating the database"
# Retry: a freshly-started Postgres briefly accepts connections on a temporary
# init server before its final restart, so the first migrate can hit a reset.
migrated=
for _ in $(seq 1 10); do
  if (cd api && uv run alembic upgrade head); then
    migrated=1
    break
  fi
  sleep 2
done
[ -n "$migrated" ] || { echo "migration failed" >&2; exit 1; }

echo "==> booting the API on $API_URL"
setsid bash -c "cd api && exec uv run uvicorn mc_server_dashboard_api.app:create_app \
  --factory --host 127.0.0.1 --port '$API_PORT'" >/tmp/webui-e2e-uvicorn.log 2>&1 &
uvicorn_pid=$!

echo "==> waiting for the API to be ready"
ready=
for _ in $(seq 1 60); do
  if curl -fsS "$API_URL/healthz" 2>/dev/null | grep -q '"ok":true'; then
    ready=1
    break
  fi
  sleep 1
done
if [ -z "$ready" ]; then
  echo "API did not become ready; uvicorn log:" >&2
  cat /tmp/webui-e2e-uvicorn.log >&2
  exit 1
fi

echo "==> seeding the platform admin"
export MCD_E2E_API_URL="$API_URL"
# Promote the seeded admin in the DB — the one out-of-band bootstrap step
# (DEPLOYMENT.md 5), run via the API's pinned asyncpg so no psql is needed.
ADMIN_USERNAME="${MCD_E2E_ADMIN_USERNAME:-e2e-admin}"
export MCD_E2E_PROMOTE_CMD="cd '$REPO_ROOT/api' && MCD_API_DATABASE__URL='$DB_URL' uv run python '$REPO_ROOT/webui/e2e/promote_admin.py' '$ADMIN_USERNAME'"
node webui/e2e/seed-admin.mjs

echo "==> running Playwright"
(cd webui && npm run e2e -- "$@")
