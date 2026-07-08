#!/usr/bin/env bash
# Parallel make-check orchestrator (issue #1735).
#
# Runs the same gates as `make check` but overlaps independent module chains
# in Phase 1, then runs the generation-based drift checks (proto-check,
# openapi-check) in Phase 2 after all readers have finished. This avoids the
# read-during-rewrite race between generators and lint/test/build targets.
#
# Phase 1 — reader chains (parallel, disjoint module dirs):
#   A: api-lint → api-test              (api/)
#   B: webui-lint → webui-test → webui-build  (webui/)
#   C: worker-lint → worker-test → worker-e2e-compile  (worker/)
#   D: relay-lint → relay-test → relay-e2e-compile     (relay/)
#   E: proto-lint                       (proto/)
#   F: hooks-check → hooks-test         (.githooks/)
#   G: docs-check                       (docs/)
#
# Phase 2 — drift checks (serial; generators write files read by Phase 1):
#   proto-check  (proto-gen + git diff; writes api/worker/relay stubs)
#   openapi-check (openapi-gen + git diff; writes webui files)
#
# Bounded parallelism: 7 background jobs on a 4-core host. The heavy chains
# (A, B) are CPU-bound; the lighter ones (C-G) finish quickly and free cores.
# golangci-lint is capped at --concurrency=2 by the Makefile, and pytest-xdist
# uses -n auto (4 workers). Oversubscription is transient and tolerable.

set -uo pipefail

LOGDIR=$(mktemp -d)
trap 'rm -rf "$LOGDIR"' EXIT

failed_chains=()

# Run a named chain: chain_name target1 [target2 ...]
# Each target in the chain runs serially (lint before test preserves cache
# warming). Output goes to a per-chain log file.
run_chain() {
    local name=$1; shift
    local log="$LOGDIR/$name.log"
    local rc=0
    for target in "$@"; do
        if ! make "$target" >>"$log" 2>&1; then
            rc=1
            break
        fi
    done
    return $rc
}

# --- Pre-flight: ensure golangci-lint binary exists before Phase 1 ---
# worker-lint and relay-lint both depend on worker/.bin/golangci-lint.
# Running both concurrently without the binary would race on `go install`.
make worker/.bin/golangci-lint >"$LOGDIR/golangci-install.log" 2>&1 || {
    echo "FAIL: golangci-lint install" >&2
    cat "$LOGDIR/golangci-install.log" >&2
    exit 1
}

# --- Phase 1: reader chains in parallel ---
echo "=== Phase 1: reader chains (parallel) ==="

run_chain api      api-lint api-test &
pids[0]=$!; names[0]=api

run_chain webui    webui-lint webui-test webui-build &
pids[1]=$!; names[1]=webui

run_chain worker   worker-lint worker-test worker-e2e-compile &
pids[2]=$!; names[2]=worker

run_chain relay    relay-lint relay-test relay-e2e-compile &
pids[3]=$!; names[3]=relay

run_chain proto    proto-lint &
pids[4]=$!; names[4]=proto

run_chain hooks    hooks-check hooks-test &
pids[5]=$!; names[5]=hooks

run_chain docs     docs-check &
pids[6]=$!; names[6]=docs

# Wait for all Phase 1 chains; collect failures.
for i in "${!pids[@]}"; do
    if ! wait "${pids[$i]}"; then
        failed_chains+=("${names[$i]}")
    fi
done

# Report Phase 1 failures (dump logs) but continue to Phase 2 so all
# failures are visible in one run.
if (( ${#failed_chains[@]} > 0 )); then
    echo "" >&2
    echo "=== Phase 1 failures ===" >&2
    for name in "${failed_chains[@]}"; do
        echo "--- $name ---" >&2
        cat "$LOGDIR/$name.log" >&2
        echo "" >&2
    done
fi

# --- Phase 2: drift checks (serial, after all readers) ---
echo "=== Phase 2: drift checks ==="

# proto-check runs proto-gen (writes api/worker/relay stubs) then diffs.
# openapi-check runs openapi-gen (writes webui files) then diffs.
# Run proto-check first because openapi-gen internally imports api source
# that proto-gen writes to (api/src/mcsd/).
for target in proto-check openapi-check; do
    log="$LOGDIR/$target.log"
    if ! make "$target" >"$log" 2>&1; then
        failed_chains+=("$target")
        echo "--- $target ---" >&2
        cat "$log" >&2
        echo "" >&2
    fi
done

# --- Final verdict ---
if (( ${#failed_chains[@]} > 0 )); then
    echo "FAILED chains: ${failed_chains[*]}" >&2
    exit 1
fi

echo "=== All checks passed ==="
