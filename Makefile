# Root-level unified entry commands for the Python (api/) + Go (worker/)
# monorepo. Each target fans out to the per-module commands documented in
# api/README.md and worker/README.md.
#
# Usage:
#   make format         # auto-format both modules
#   make lint           # lint + typecheck both modules (no writes)
#   make test           # run both test suites
#   make check          # full gate: lint + test (what CI and pre-push run)
#   make hooks-install  # install the git hooks (one-time)

.PHONY: all check lint format test docs-check \
	api-env-check api-lint api-format api-test \
	worker-lint worker-format worker-test worker-test-race worker-e2e-compile \
	relay-lint relay-format relay-test relay-test-race \
	webui-lint webui-format webui-test webui-build webui-e2e \
	openapi-gen openapi-check \
	proto-lint proto-gen proto-check proto-breaking \
	bootstrap hooks-install hooks-check hooks-test

# golangci-lint is not part of the Go distribution; it is installed into a
# module-local, gitignored ./.bin (see worker/README.md).
GOLANGCI_VERSION := v2.12.2
GOLANGCI := worker/.bin/golangci-lint

# Per-worktree golangci-lint analysis cache. The default shared cache
# (~/.cache/golangci-lint) outlives the agent worktrees under .claude/worktrees/
# and retains findings keyed to since-deleted sibling paths, failing a later
# pre-push in an unrelated worktree with phantom issues (#375). Scoping the cache
# to this worktree's git dir -- unique per worktree, never tracked, swept with
# the worktree -- removes the cross-worktree contamination structurally. CI
# runners are fresh, so this is a no-op there.
GOLANGCI_LINT_CACHE := $(shell git rev-parse --absolute-git-dir)/golangci-lint-cache

# protoc code-generation plugins. Pinned + documented (proto/README.md,
# docs/dev/DEPENDENCIES.md). The Go plugins install into the same gitignored
# worker/.bin; the Python generators come from the api/ dev group (uv).
PROTOC_GEN_GO_VERSION := v1.36.11
PROTOC_GEN_GO_GRPC_VERSION := v1.6.2
PROTOC_GEN_GO := worker/.bin/protoc-gen-go
PROTOC_GEN_GO_GRPC := worker/.bin/protoc-gen-go-grpc

all: check

# Full verification gate. Matches the pre-push hook and CI.
check: hooks-check lint test webui-build openapi-check proto-check docs-check

lint: api-lint worker-lint relay-lint webui-lint proto-lint

format: api-format worker-format relay-format webui-format

test: api-test worker-test worker-e2e-compile relay-test webui-test hooks-test

# docs/ convention gate (docs/README.md Conventions): relative links resolve,
# no section-mark glyph, no 'v1' versioning term. Pure stdlib python3, no deps.
docs-check:
	python3 scripts/check_docs.py --self-test
	python3 scripts/check_docs.py

# ---------------------------------------------------------------------------
# api/ (Python via uv)
# ---------------------------------------------------------------------------

# Preflight for every api/ gate: fail loud when the active Python environment
# resolves mc_server_dashboard_api from a DIFFERENT checkout than this one. Agent
# worktrees under .claude/worktrees/ inherit VIRTUAL_ENV pointing at the primary
# checkout's api/.venv, so depending on the uv version `uv run mypy/pytest` can
# silently check the primary checkout's sources (on main) instead of the branch
# -- a false pass that costs a CI round (#566). The check runs through the SAME
# `cd api && uv run` path as the real gate so it observes exactly what the gate
# would, and fails (not warns) with the `uv sync` fix, since a wrong-source gate
# invalidates everything after it. On a fresh checkout (CI, the primary checkout)
# there is no shadowing, so it is a silent no-op. Prerequisite of the api-*
# targets so a directly-invoked `make api-test` is guarded too.
api-env-check:
	cd api && uv run python ../scripts/check_api_env.py

api-lint: api-env-check
	cd api && uv run ruff check .
	cd api && uv run ruff format --check .
	cd api && uv run mypy .
	cd api && uv run lint-imports

api-format: api-env-check
	cd api && uv run ruff format .
	cd api && uv run ruff check --fix .

api-test: api-env-check
	cd api && uv run pytest

# ---------------------------------------------------------------------------
# worker/ (Go)
# ---------------------------------------------------------------------------

worker-lint: $(GOLANGCI)
	@out="$$(cd worker && gofmt -l .)"; \
	if [ -n "$$out" ]; then \
		echo "gofmt: the following files are not formatted:"; \
		echo "$$out"; \
		echo "run 'make worker-format' to fix"; \
		exit 1; \
	fi
	cd worker && go vet ./...
	cd worker && GOLANGCI_LINT_CACHE="$(GOLANGCI_LINT_CACHE)" ./.bin/golangci-lint run

worker-format:
	cd worker && gofmt -w .

worker-test:
	cd worker && go test ./...

# Worker test suite under the race detector. This is the gate CI runs (see
# .github/workflows/worker.yml); the worker is concurrency-heavy and races have
# been chased by hand before (#308). Kept separate from `worker-test` /
# `make check` so the pre-push hook stays fast; run it locally before touching
# the supervision/pump/session/driver code.
worker-test-race:
	cd worker && go test -race ./...

# Compile-only check of the `-tags e2e` worker sources (worker/test/e2e/,
# //go:build e2e). The e2e suite itself needs the live stack (real API +
# Docker) and runs only in the dedicated CI jobs (.github/workflows/e2e.yml),
# so `make check` must NOT run it. But those files build with `-tags e2e`,
# which `worker-test` (no tag) never compiles -- a worker signature change that
# strands an e2e consumer passes the local gate and only fails later in CI
# (#768, most recently #767). `go vet` type-checks the tagged test files
# without running them: no API, no containers, no env, fast. Wired into `test`
# so the pre-push hook catches the dangling consumer the way CI does.
worker-e2e-compile:
	cd worker && go vet -tags e2e ./test/e2e/...

# ---------------------------------------------------------------------------
# relay/ (Go) -- the game ingress relay (docs/app/RELAY.md). Same Go toolchain
# and lint posture as worker/; it reuses the pinned golangci-lint installed into
# worker/.bin (one binary for both modules).
# ---------------------------------------------------------------------------

relay-lint: $(GOLANGCI)
	@out="$$(cd relay && gofmt -l .)"; \
	if [ -n "$$out" ]; then \
		echo "gofmt: the following files are not formatted:"; \
		echo "$$out"; \
		echo "run 'make relay-format' to fix"; \
		exit 1; \
	fi
	cd relay && go vet ./...
	cd relay && GOLANGCI_LINT_CACHE="$(GOLANGCI_LINT_CACHE)" ../worker/.bin/golangci-lint run

relay-format:
	cd relay && gofmt -w .

relay-test:
	cd relay && go test ./...

# Relay test suite under the race detector (the relay is concurrency-heavy:
# splice goroutines, the token rendezvous, the batched reporter). Mirrors
# worker-test-race; kept out of the fast pre-push gate, run by CI.
relay-test-race:
	cd relay && go test -race ./...

# ---------------------------------------------------------------------------
# webui/ (Node via npm)
#
# The webui `check` npm script chains lint + typecheck + test + build, and
# `build` re-runs `tsc -b` after the standalone typecheck. The granular npm
# scripts below avoid that double type-check: `webui-lint` covers Biome lint +
# format-check (biome check) and the standalone typecheck; `webui-build` runs
# the production build (which type-checks once via `tsc -b`) so type/build
# breakage is caught by the gate.
# ---------------------------------------------------------------------------

webui-lint:
	cd webui && npm run lint
	cd webui && npm run typecheck

webui-format:
	cd webui && npm run format

webui-test:
	cd webui && npm run test

webui-build:
	cd webui && npm run build

# Playwright E2E over the critical flows against a real API + Postgres (issue
# #491). Deliberately NOT part of `make check` — it is the slow path: it boots
# Postgres (Docker), the API, a seeded admin, and a browser. The orchestration
# script brings the stack up and tears it down; extra args pass through to
# `playwright test` (e.g. `make webui-e2e ARGS=auth.spec.ts`). Browsers must be
# installed once: `cd webui && npx playwright install chromium`.
webui-e2e:
	scripts/run_webui_e2e.sh $(ARGS)

# ---------------------------------------------------------------------------
# webui OpenAPI client artifacts (webui/openapi.json + webui/src/api/schema.ts)
#
# Both are generated from the api/ route table but committed by hand. The
# `openapi` npm script chains the two generators: `openapi:export` dumps
# `app.openapi()` to webui/openapi.json (deterministic — sorted keys, stable
# formatting; see api/src/.../export_openapi.py and its determinism test), then
# `openapi:generate` runs openapi-typescript over that JSON to webui/src/api/
# schema.ts. Mirrors the proto-gen / proto-check pair.
# ---------------------------------------------------------------------------

openapi-gen:
	cd webui && npm run openapi

# Drift gate: regenerate the client artifacts and fail if they differ from the
# committed copies (CI + `make check`). Catches an api route change that landed
# without regenerating the webui contract.
openapi-check: openapi-gen
	@if ! git diff --exit-code -- webui/openapi.json webui/src/api/schema.ts; then \
		echo "webui OpenAPI artifacts are stale; run 'make openapi-gen' and commit the result."; \
		exit 1; \
	fi

# Install the pinned golangci-lint into worker/.bin if it is missing.
$(GOLANGCI):
	cd worker && GOBIN="$$(pwd)/.bin" go install \
		github.com/golangci/golangci-lint/v2/cmd/golangci-lint@$(GOLANGCI_VERSION)

# Bootstrap local tooling. uv installs the Python toolchain on first `uv run`,
# but syncing up front gives a clear, fast failure if the environment is wrong.
bootstrap: $(GOLANGCI)
	cd api && uv sync
	cd webui && npm ci

# ---------------------------------------------------------------------------
# Git hooks
# ---------------------------------------------------------------------------

# Point git at the checked-in hooks. One command, no external dependency.
hooks-install:
	git config core.hooksPath .githooks
	@echo "git hooks installed (core.hooksPath -> .githooks)"

# Preflight for `make check`: assert core.hooksPath and git identity on the
# PRIMARY checkout; skip silently on CI runners and on agent worktrees (which
# live under .claude/worktrees/ and are expected to leave main). The parallel-
# worktree tooling has been seen resetting hooksPath to the absolute .git/hooks
# path (#551/#867), silently disabling the pre-commit / pre-push / post-checkout
# gates. This FAILs (not warns) on the primary checkout so the breakage is
# visible before it causes further damage. Run `make hooks-install` to restore.
#
# Also asserts user.name/user.email are not the test identity (Test /
# test@example.com) — a GIT_DIR-leak incident once wrote those into the shared
# .git/config and caused commits to carry the wrong author (#867).
hooks-check:
	@if [ "$$CI" = "true" ]; then exit 0; fi; \
	_toplevel="$$(git rev-parse --show-toplevel 2>/dev/null)"; \
	case "$$_toplevel" in */.claude/worktrees/*) exit 0 ;; esac; \
	_fail=0; \
	if [ "$$(git config core.hooksPath)" != ".githooks" ]; then \
		echo "============================================================"; \
		echo "FAIL: git core.hooksPath is not '.githooks'"; \
		echo "  current: $$(git config core.hooksPath || echo '<unset>')"; \
		echo "  The pre-commit / pre-push / post-checkout hooks are DISABLED"; \
		echo "  for every checkout sharing this .git/config (see #551/#867)."; \
		echo "  Restore them with: make hooks-install"; \
		echo "============================================================"; \
		_fail=1; \
	fi; \
	_name="$$(git config user.name 2>/dev/null || true)"; \
	_email="$$(git config user.email 2>/dev/null || true)"; \
	if [ "$$_name" = "Test" ] || [ "$$_email" = "test@example.com" ]; then \
		echo "============================================================"; \
		echo "FAIL: git identity is the test identity (Test/test@example.com)."; \
		echo "  user.name=$$_name  user.email=$$_email"; \
		echo "  A GIT_DIR-leak incident wrote this into .git/config (#867)."; \
		echo "  Fix: git config user.name 'Your Name'"; \
		echo "       git config user.email 'your@email.com'"; \
		echo "============================================================"; \
		_fail=1; \
	fi; \
	exit $$_fail

# Unit-test the checked-in git hooks. Pure bash + a temp git repo; no external
# test runner required. Included in `make test`.
hooks-test:
	bash .githooks/test-post-checkout.sh
	bash .githooks/test-hooks-check.sh

# ---------------------------------------------------------------------------
# proto/ (buf) -- the shared control-plane contract.
#
# Stubs are checked in (see proto/README.md). `proto-gen` regenerates them
# deterministically; `proto-check` (run by `check` and CI) fails if the working
# tree's stubs drift from a fresh generation.
# ---------------------------------------------------------------------------

proto-lint:
	cd proto && buf lint

# Breaking-change gate: compare the working tree's proto/ against the contract
# on origin/main and fail on any backwards-incompatible change (those drive a
# MAJOR bump per docs/dev/RELEASING.md). Needs origin/main fetched locally; not
# part of `make check` (keeps `check` fast + local-state-independent). The proto
# CI workflow is the enforced gate; this target is for local pre-flight.
proto-breaking:
	cd proto && buf breaking . --against '../.git#branch=origin/main,subdir=proto'

# Regenerate both languages' stubs from proto/. Go via buf (pinned local
# plugins); Python via grpcio-tools + mypy-protobuf (pinned in api/ dev group).
proto-gen: $(PROTOC_GEN_GO) $(PROTOC_GEN_GO_GRPC)
	cd proto && buf generate
	# The relay is a sibling Go module and cannot import the worker module's
	# internal/ stubs, so generate it its own copy of the mcsd.relay.v1 package
	# (buf.gen.relay.yaml, scoped to the relay proto). See proto/README.md.
	cd proto && buf generate --template buf.gen.relay.yaml --path mcsd/relay/v1/relay.proto
	cd api && uv run python -m grpc_tools.protoc \
		-I ../proto \
		--python_out=src \
		--grpc_python_out=src \
		--mypy_out=src \
		--mypy_grpc_out=src \
		../proto/mcsd/controlplane/v1/control_plane.proto \
		../proto/mcsd/relay/v1/relay.proto

# Drift gate: regenerate and fail if anything changed -- modified tracked stubs
# or new untracked ones (CI + `make check`).
proto-check: proto-gen
	@dirty="$$(git status --porcelain -- worker/internal/controlplane relay/internal/genproto api/src/mcsd)"; \
	if [ -n "$$dirty" ]; then \
		echo "proto stubs are stale; run 'make proto-gen' and commit:"; \
		echo "$$dirty"; \
		git --no-pager diff -- worker/internal/controlplane relay/internal/genproto api/src/mcsd; \
		exit 1; \
	fi

# Install the pinned Go protoc plugins into worker/.bin if missing.
$(PROTOC_GEN_GO):
	cd worker && GOBIN="$$(pwd)/.bin" go install \
		google.golang.org/protobuf/cmd/protoc-gen-go@$(PROTOC_GEN_GO_VERSION)

$(PROTOC_GEN_GO_GRPC):
	cd worker && GOBIN="$$(pwd)/.bin" go install \
		google.golang.org/grpc/cmd/protoc-gen-go-grpc@$(PROTOC_GEN_GO_GRPC_VERSION)
