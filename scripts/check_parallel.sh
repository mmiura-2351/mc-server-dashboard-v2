#!/usr/bin/env bash
# Parallel make-check orchestrator (issue #1735).
#
# Runs the same gates as `make check` but overlaps independent module chains
# in Phase 1, then runs the generation-based drift checks (proto-check,
# openapi-check) in Phase 2 after all readers have finished. This avoids the
# read-during-rewrite race between generators and lint/test/build targets.
#
# Phase 1a — Go lint (serial; golangci-lint holds a host-global lock for the
#            duration of a run, so worker-lint and relay-lint cannot overlap):
#   worker-lint, relay-lint
#
# Phase 1b — reader chains (parallel, disjoint module dirs):
#   A: api-lint → api-test              (api/)
#   B: webui-lint → webui-test → webui-build  (webui/)
#   C: worker-test → worker-e2e-compile (worker/)
#   D: relay-test → relay-e2e-compile   (relay/)
#   E: proto-lint                       (proto/)
#   F: hooks-check → hooks-test         (.githooks/)
#   G: docs-check                       (docs/)
#   H: scripts-test                     (scripts/)
#   I: migrations-check                 (api/migrations/)
#   J: test-client-check                (api/tests/, issue #1980)
#
# Phase 2 — drift checks (serial; generators write files read by Phase 1):
#   proto-check  (proto-gen + git diff; writes api/worker/relay stubs)
#   openapi-check (openapi-gen + git diff; writes webui files)
#   Skipped entirely if Phase 1 already failed (no point running generators
#   on a known-broken tree).
#
# Bounded parallelism: 10 background jobs on a 4-core host. The heavy chains
# (A, B) are CPU-bound; the lighter ones (C-J) finish quickly and free cores.
# golangci-lint is capped at --concurrency=2 by the Makefile, and pytest-xdist
# uses -n auto (4 workers). Oversubscription is transient and tolerable.
#
# That budget assumes the run owns the host, so the run takes a host-global
# flock first and waits, naming the holder, when another gate has it (#2513).

set -uo pipefail

# The worktree this run belongs to, carried in our own command line (#2605).
# `make check` passes $(CURDIR); a direct invocation falls back to the current
# directory. Nothing this script spawns carries the path -- the sub-makes,
# pytest and vitest below all run with a bare argv -- so this argument is the
# only thing in the process listing that ties a running gate to its worktree.
# It is what makes `pgrep -f <worktree>` find a gate orphaned by a killed
# `git push`, whose process group then holds the rest of the tree
# (docs/dev/AGENTS.md Section 3). Printing it also stamps the worktree on the
# run's own output, so a pasted log says which one produced it.
worktree=${1:-$PWD}
echo "=== check: $worktree ==="

# --- One gate at a time per host (#2513) ---
#
# The measured cause of the timeout reds: several agent worktrees each ran a
# gate at once on a 4-core box, and every one of them fans pytest out with
# `-n auto`, which sizes the pool to the host's core count. Four gates therefore
# claimed ~16 pytest workers plus four vitest pools and four Go suites (observed
# load average 7-17). The fs-heavy api tests lost first, because an fsync under
# that load waits on the other runs' writeback: tests that take 3.6 s alone were
# killed at the 120 s per-test cap (~33x), on diffs containing no Python. The
# answer is mutual exclusion rather than a bigger budget -- with runs serialised,
# `-n auto` on an otherwise idle box is the right size again.
#
# The lock is host-global, so it must not live inside a worktree or under a
# per-shell TMPDIR (which would silently give each shell its own lock and no
# mutual exclusion at all); MCSD_CHECK_LOCK_FILE exists so the self-test can run
# against an isolated one, not as a knob to relocate the real lock.
#
# The waiter names the holder, because a wait with no explanation is
# indistinguishable from a hang. The holder is often the SAME worktree: the
# descriptor is inherited by everything this script spawns, so a gate orphaned
# by a killed `git push` keeps the lock until it finishes, and the next run in
# that worktree now waits for it and says so -- where before it raced the
# survivor and died mid-suite (#2605, docs/dev/AGENTS.md Section 3).
#
# MCSD_CHECK_LOCK_HELD makes the lock re-entrant. `make scripts-test` runs this
# script (test_check_parallel_lock.sh, test_check_parallel_identity.sh) from
# inside a running gate, which would otherwise wait for the lock its own parent
# holds, forever.
lock_file=${MCSD_CHECK_LOCK_FILE:-/tmp/mcsd-check.lock}
if [ -z "${MCSD_CHECK_LOCK_HELD:-}" ]; then
    # Append rather than truncate: opening the file must not erase the holder
    # line before the lock is even taken.
    exec 9>>"$lock_file" || {
        echo "FAIL: cannot open the gate lock file $lock_file" >&2
        exit 1
    }
    lock_holder() {
        local line
        line=$(head -n 1 "$lock_file" 2>/dev/null) || line=""
        printf '%s' "${line:-<unknown>}"
    }
    if ! flock -n 9; then
        echo "=== check: another gate holds $lock_file; waiting ==="
        echo "===   held by: $(lock_holder)"
        while ! flock -w 60 9; do
            echo "===   still waiting; held by: $(lock_holder)"
        done
        echo "=== check: lock acquired ==="
    fi
    printf '%s (pid %d, since %s)\n' "$worktree" "$$" "$(date '+%Y-%m-%dT%H:%M:%S%z')" >"$lock_file"
    export MCSD_CHECK_LOCK_HELD=1
fi

# Per-chain logs persist after the run (#2031). These used to go to a mktemp
# dir deleted on exit, so a failure left nothing behind once the terminal
# output scrolled or was truncated: three of eight reported failures could not
# even name which chain broke, and the run that produced the error is gone by
# the time anyone knows they want it. Writing under the worktree's git dir
# keeps them unique per worktree, never tracked, and swept with the worktree
# (same rationale as GOLANGCI_LINT_CACHE in the Makefile). Each run starts
# clean, so the logs always describe the most recent run.
git_dir=$(git rev-parse --absolute-git-dir 2>/dev/null) || {
    echo "FAIL: not inside a git checkout (needed to locate the log dir)" >&2
    exit 1
}
LOGDIR="$git_dir/check-logs"
rm -rf "$LOGDIR"
mkdir -p "$LOGDIR"
# On INT/TERM: kill background jobs first, then exit.
trap 'jobs -p | xargs -r kill 2>/dev/null; exit 1' INT TERM

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

# --- Phase 1a: Go lint serial (golangci-lint mutual exclusion) ---
# golangci-lint v2 takes an exclusive flock on $TMPDIR/golangci-lint.lock for
# every run. The lock is host-global -- it lives outside GOLANGCI_LINT_CACHE, so
# the Makefile's per-worktree cache does not decouple it and any two concurrent
# runs on this host contend, worktree-local or not. The Makefile passes
# --allow-serial-runners so a contender queues rather than aborting with
# "parallel golangci-lint is running" (#2031). Running the two lint targets
# (~0.8s each) serially here keeps them off each other's lock instead of paying
# that wait, then tests run in parallel.
echo "=== Phase 1a: Go lint (serial) ==="

run_chain worker-lint worker-lint || failed_chains+=(worker-lint)
run_chain relay-lint  relay-lint  || failed_chains+=(relay-lint)

# --- Phase 1b: reader chains in parallel ---
echo "=== Phase 1b: reader chains (parallel) ==="

run_chain api      api-lint api-test &
pids[0]=$!; names[0]=api

run_chain webui    webui-lint webui-test webui-build &
pids[1]=$!; names[1]=webui

run_chain worker   worker-test worker-e2e-compile &
pids[2]=$!; names[2]=worker

run_chain relay    relay-test relay-e2e-compile &
pids[3]=$!; names[3]=relay

run_chain proto    proto-lint &
pids[4]=$!; names[4]=proto

run_chain hooks    hooks-check hooks-test &
pids[5]=$!; names[5]=hooks

run_chain docs     docs-check &
pids[6]=$!; names[6]=docs

run_chain scripts  scripts-test &
pids[7]=$!; names[7]=scripts

run_chain migrations migrations-check &
pids[8]=$!; names[8]=migrations

run_chain test-client test-client-check &
pids[9]=$!; names[9]=test-client

# Wait for all Phase 1 chains; collect failures.
for i in "${!pids[@]}"; do
    if ! wait "${pids[$i]}"; then
        failed_chains+=("${names[$i]}")
    fi
done

# Report Phase 1 failures and skip Phase 2 (no point running generators on
# a known-broken tree).
if (( ${#failed_chains[@]} > 0 )); then
    echo "" >&2
    echo "=== Phase 1 failures ===" >&2
    for name in "${failed_chains[@]}"; do
        echo "--- $name ---" >&2
        cat "$LOGDIR/$name.log" >&2
        echo "" >&2
    done
    echo "FAILED chains: ${failed_chains[*]}" >&2
    echo "(Phase 2 drift checks skipped due to Phase 1 failures)" >&2
    echo "Full output per chain: $LOGDIR/<chain>.log" >&2
    exit 1
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
    echo "Full output per chain: $LOGDIR/<chain>.log" >&2
    exit 1
fi

echo "=== All checks passed ==="
