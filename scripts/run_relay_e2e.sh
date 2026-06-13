#!/usr/bin/env bash
# Orchestrate the relay protocol-level E2E run locally (epic #659, issue #962).
#
# Brings up the REAL compose stack with the `relay` profile and drives a minimal
# protocol-level Java-edition client (handshake/status/login packets only)
# against the real relay's player listener, end to end through the real API's
# RelayService and a real Postgres:
#   1. db (+ alembic migrate), the API with the relay enabled, and the relay
#      itself (compose `relay` profile) — the fs storage backend so neither
#      SeaweedFS nor any object credential is needed for this suite,
#   2. a self-signed tunnel certificate (SAN DNS:relay, matching the in-network
#      tunnel endpoint relay:25665) — the relay tunnel listener always wants TLS,
#   3. the bootstrap platform admin (the API auto-grants platform admin to the
#      first registered user, issue #909), a community, and a STOPPED server with
#      a known slug,
#   4. `go test -tags e2e ./test/e2e/...` in relay/, which connects to the relay's
#      published :25565 and asserts the stopped/unknown protocol paths.
#
# The running-status and login paths (real MOTD through the tunnel, a game_session
# row) need a server the worker has actually BOOTED — a real Minecraft `java -jar`
# launch behind the tunnel (the API start path has no stub-JAR seam: it downloads
# and verifies a real JAR before dispatch). That real boot is too heavy and
# network-bound for the E2E budget, so this harness does NOT exercise them; the
# relay's running-server protocol logic — status cache, login splice, session
# recording — is covered in-process against the real relay components by
# relay/test/integration_test.go. See docs/app/RELAY.md Sections 4-7.
#
# Everything it starts, it stops on exit. CI runs this same script as its one
# source of orchestration truth (.github/workflows/relay-e2e.yml).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# A dedicated compose project so this harness never collides with a developer's
# running `docker compose` stack (separate volumes and container names). The
# override file renames the default network (the base compose pins it to a fixed
# `mcsd`, which a live stack already owns) so this harness is fully isolated.
PROJECT="mcsd-relay-e2e"
COMPOSE=(docker compose -p "$PROJECT"
  -f "$REPO_ROOT/compose.yaml"
  -f "$REPO_ROOT/scripts/compose.relay-e2e.yaml"
  --profile relay)

# Host ports the suite binds/connects to. All three are overridable so the harness
# can run alongside a live relay-profile deployment that already holds the defaults.
# MCD_RELAY_E2E_GAME_PORT and MCD_RELAY_E2E_TUNNEL_PORT are written into the compose
# env file; scripts/compose.relay-e2e.yaml picks them up to remap the relay's host
# publish ports (overriding the hard-coded 25565:25565 / 25665:25665 in the base
# compose.yaml). The Go test client reads MCD_RELAY_E2E_GAME_ADDR, which is built
# from RELAY_GAME_PORT below, so all three variables affect the same port.
API_PORT="${MCD_RELAY_E2E_API_PORT:-8081}"
RELAY_GAME_PORT="${MCD_RELAY_E2E_GAME_PORT:-25565}"
RELAY_TUNNEL_PORT="${MCD_RELAY_E2E_TUNNEL_PORT:-25665}"
BASE_DOMAIN="mc.test"

ENV_FILE="$(mktemp)"
TLS_DIR="$(mktemp -d)"

cleanup() {
  # MCD_RELAY_E2E_KEEP=1 leaves the stack up for debugging (tear it down manually
  # with `docker compose -p mcsd-relay-e2e down -v`).
  if [ "${MCD_RELAY_E2E_KEEP:-}" != "1" ]; then
    "${COMPOSE[@]}" --env-file "$ENV_FILE" down -v --remove-orphans >/dev/null 2>&1 || true
    rm -f "$ENV_FILE"
    rm -rf "$TLS_DIR"
  else
    echo "MCD_RELAY_E2E_KEEP=1: leaving the stack up (env file: $ENV_FILE)" >&2
  fi
}
trap cleanup EXIT

echo "==> generating the self-signed tunnel certificate (SAN DNS:relay)"
# The in-network tunnel endpoint the Worker dials is relay:25665, so the cert's
# SAN must be DNS:relay (Go ignores CN and requires a matching SAN).
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 \
  -keyout "$TLS_DIR/tunnel-key.pem" \
  -out "$TLS_DIR/tunnel-cert.pem" \
  -days 3650 -nodes \
  -subj "/CN=mcsd-relay-tunnel" \
  -addext "subjectAltName=DNS:relay" >/dev/null 2>&1

echo "==> writing the compose env file"
# fs storage backend with no object profile: the stopped/unknown paths never boot
# a server, so SeaweedFS and its credentials are unnecessary. The relay profile is
# added on the compose command line (COMPOSE_PROFILES stays empty here).
cat >"$ENV_FILE" <<EOF
POSTGRES_USER=mcsd
POSTGRES_DB=mcsd
POSTGRES_PASSWORD=relay-e2e-postgres-password
COMPOSE_PROFILES=
MCD_API_STORAGE__BACKEND=fs
MCD_API_AUTH__TOKEN__SIGNING_KEY=relay-e2e-signing-key-0123456789abcdef0123
MCD_API_CONTROL__WORKER_CREDENTIAL=relay-e2e-worker-credential
MCD_API_STORAGE__OBJECT__ACCESS_KEY=
MCD_API_STORAGE__OBJECT__SECRET_KEY=
MCSD_SCRATCH_DIR=/tmp/mcsd-relay-e2e-scratch
DOCKER_GID=${DOCKER_GID:-$(getent group docker | cut -d: -f3)}
MCD_WORKER_GAME_BIND_IP=127.0.0.1
MCD_API_RELAY__ENABLED=true
MCD_API_RELAY__CREDENTIAL=relay-e2e-relay-credential
MCD_API_RELAY__BASE_DOMAIN=${BASE_DOMAIN}
MCD_RELAY_TUNNEL_PUBLIC_ENDPOINT=relay:25665
MCD_RELAY_TLS_DIR=${TLS_DIR}
API_HTTP_PORT=${API_PORT}
MCD_RELAY_E2E_GAME_PORT=${RELAY_GAME_PORT}
MCD_RELAY_E2E_TUNNEL_PORT=${RELAY_TUNNEL_PORT}
EOF

echo "==> building and bringing up the stack (db, api, worker, relay)"
# MCD_RELAY_E2E_NO_BUILD=1 skips the in-line image build (for environments where
# the api/worker/relay images are already built and tagged :dev — e.g. a sandbox
# whose Docker build network cannot resolve DNS; pre-build them once with
# `docker build --network=host`).
if [ "${MCD_RELAY_E2E_NO_BUILD:-}" = "1" ]; then
  "${COMPOSE[@]}" --env-file "$ENV_FILE" up -d
else
  "${COMPOSE[@]}" --env-file "$ENV_FILE" up -d --build
fi

API_URL="http://127.0.0.1:${API_PORT}"
echo "==> waiting for the API to be ready ($API_URL)"
ready=
for _ in $(seq 1 90); do
  if curl -fsS "$API_URL/api/healthz" 2>/dev/null | grep -q '"ok":true'; then
    ready=1
    break
  fi
  sleep 2
done
if [ -z "$ready" ]; then
  echo "API did not become ready; recent logs:" >&2
  "${COMPOSE[@]}" --env-file "$ENV_FILE" logs --tail=80 api >&2 || true
  exit 1
fi

echo "==> waiting for the relay to register (it learns base_domain from the API)"
# The relay logs "relay registered with API" with the learned base_domain only
# after a SUCCESSFUL Register RPC; until then it has no base_domain and drops every
# connection (the routing match fails), so the stopped/unknown assertions race the
# registration unless we wait for this exact line.
relay_ready=
for _ in $(seq 1 30); do
  if "${COMPOSE[@]}" --env-file "$ENV_FILE" logs --tail=200 relay 2>/dev/null \
      | grep -q "relay registered with API"; then
    relay_ready=1
    break
  fi
  sleep 1
done
if [ -z "$relay_ready" ]; then
  echo "relay did not register; recent logs:" >&2
  "${COMPOSE[@]}" --env-file "$ENV_FILE" logs --tail=60 relay >&2 || true
  exit 1
fi

echo "==> seeding the admin, community, and a stopped server"
# seed_relay_e2e.py registers the first user (auto platform admin), creates a
# community + a stopped server, and prints the server slug on stdout.
SLUG="$(MCD_RELAY_E2E_API_URL="$API_URL" python3 scripts/seed_relay_e2e.py)"
echo "    stopped server slug: $SLUG"

echo "==> running the relay protocol-level e2e suite"
cd relay
MCD_RELAY_E2E_GAME_ADDR="127.0.0.1:${RELAY_GAME_PORT}" \
MCD_RELAY_E2E_BASE_DOMAIN="${BASE_DOMAIN}" \
MCD_RELAY_E2E_STOPPED_SLUG="${SLUG}" \
  go test -tags e2e -v -timeout 120s ./test/e2e/...
