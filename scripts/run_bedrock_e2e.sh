#!/usr/bin/env bash
# Orchestrate the Bedrock relay protocol-level E2E run locally (epic #1540,
# issue #1547). Mirrors scripts/run_relay_e2e.sh's shape (one orchestration
# script both `make bedrock-e2e` and CI run) but is self-contained: it needs
# neither Postgres nor the API. The API's OpenBedrockTunnel dispatch and
# ValidateBedrockTunnel credential minting (issue #1544) are out of scope for
# this suite -- see relay/test/e2e/bedrock_relay_e2e_test.go's package doc for
# why -- so this harness proves the wire path AFTER a credential is accepted:
# relay UDP ingress -> QUIC tunnel -> Worker -> container port, against a real
# Docker container running a fake-Geyser RakNet responder
# (worker/test/e2e/stub-geyser) instead of real Geyser (a Modrinth/GeyserMC
# download would make CI flaky; real Geyser+Floodgate behavior was already
# validated live, epic #1540 issue #1542).
#
# The relay/internal/... and worker/internal/... packages are each importable
# only from within their own module's directory tree, so this drives TWO
# coordinating `go test -tags e2e` processes rather than one:
#   1. relay/test/e2e/bedrock_relay_e2e_test.go (backgrounded here) runs the
#      REAL relay/internal/bedrock.Listener with a stub credential validator.
#   2. worker/test/e2e/bedrock_e2e_test.go (foregrounded here) drives the REAL
#      worker/internal/adapters/bedrocktunnel.Manager and a real Docker
#      container against it, and owns the run's pass/fail signal.
#
# Everything it starts, it stops on exit.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

RELAY_LISTEN="${MCD_BEDROCK_E2E_LISTEN:-127.0.0.1:29675}"
STUB_GEYSER_IMAGE="${MCD_BEDROCK_E2E_STUB_GEYSER_IMAGE:-mcsd-bedrock-e2e-stub-geyser:latest}"
# The public bedrock_port the relay side binds and the scripted client pings.
# Overridable so the harness can run alongside a live bedrock-enabled
# relay-profile deployment already holding the default in the compose-published
# 19132-19231/udp window (mirrors scripts/run_relay_e2e.sh's port overrides).
# Both coordinating test files resolve it via their bedrockE2EPort helper, so
# it is forwarded to both go test invocations below.
BEDROCK_PORT="${MCD_BEDROCK_E2E_BEDROCK_PORT:-19140}"

WORK_DIR="$(mktemp -d)"
CA_FILE="$WORK_DIR/tunnel-ca.pem"
STOP_FILE="$WORK_DIR/stop"
RELAY_LOG="$WORK_DIR/relay.log"

RELAY_PID=""
cleanup() {
  touch "$STOP_FILE" 2>/dev/null || true
  if [ -n "$RELAY_PID" ] && kill -0 "$RELAY_PID" 2>/dev/null; then
    # The relay-side test exits on its own once it sees STOP_FILE (bounded by
    # its own bedrockE2EMaxServe safety net); wait briefly, then fall back to a
    # hard kill so a stuck process never leaks past this script.
    for _ in $(seq 1 20); do
      kill -0 "$RELAY_PID" 2>/dev/null || break
      sleep 0.5
    done
    kill "$RELAY_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

echo "==> building the stub-geyser image (fake RakNet responder, worker/test/e2e/stub-geyser)"
docker build -t "$STUB_GEYSER_IMAGE" worker/test/e2e/stub-geyser

echo "==> starting the relay-side Bedrock tunnel listener ($RELAY_LISTEN)"
(
  cd relay
  MCD_BEDROCK_E2E_LISTEN="$RELAY_LISTEN" \
  MCD_BEDROCK_E2E_BEDROCK_PORT="$BEDROCK_PORT" \
  MCD_BEDROCK_E2E_CA_FILE="$CA_FILE" \
  MCD_BEDROCK_E2E_STOP_FILE="$STOP_FILE" \
    go test -tags e2e -v -timeout 120s -run TestServeBedrockTunnelForE2E ./test/e2e/...
) >"$RELAY_LOG" 2>&1 &
RELAY_PID=$!

echo "==> waiting for the relay-side listener to be ready"
ready=
for _ in $(seq 1 60); do
  if grep -q "BEDROCK-E2E-RELAY-READY" "$RELAY_LOG" 2>/dev/null; then
    ready=1
    break
  fi
  if ! kill -0 "$RELAY_PID" 2>/dev/null; then
    break # the process already exited (a startup failure) — stop polling and report below.
  fi
  sleep 0.5
done
if [ -z "$ready" ]; then
  echo "relay-side listener did not become ready; log:" >&2
  cat "$RELAY_LOG" >&2
  exit 1
fi
# The CA file is written just before the ready log line; a ready line
# guarantees it is already on disk.

echo "==> running the worker-side Bedrock e2e suite"
cd worker
worker_status=0
MCD_E2E_DOCKER=1 \
MCD_E2E_STUB_GEYSER_IMAGE="$STUB_GEYSER_IMAGE" \
MCD_BEDROCK_E2E_RELAY_ADDR="$RELAY_LISTEN" \
MCD_BEDROCK_E2E_BEDROCK_PORT="$BEDROCK_PORT" \
MCD_BEDROCK_E2E_CA_FILE="$CA_FILE" \
  go test -tags e2e -v -timeout 120s -run TestBedrockTunnelEndToEnd ./test/e2e/... || worker_status=$?

echo "==> stopping the relay-side listener"
touch "$STOP_FILE"
for _ in $(seq 1 20); do
  kill -0 "$RELAY_PID" 2>/dev/null || break
  sleep 0.5
done

echo "==> relay-side log"
cat "$RELAY_LOG"

exit $worker_status
