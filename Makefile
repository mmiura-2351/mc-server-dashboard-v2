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
	api-lint api-format api-test \
	worker-lint worker-format worker-test worker-test-race \
	webui-lint webui-format webui-test webui-build \
	proto-lint proto-gen proto-check proto-breaking \
	bootstrap hooks-install

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
check: lint test webui-build proto-check docs-check

lint: api-lint worker-lint webui-lint proto-lint

format: api-format worker-format webui-format

test: api-test worker-test webui-test

# docs/ convention gate (docs/README.md Conventions): relative links resolve,
# no section-mark glyph, no 'v1' versioning term. Pure stdlib python3, no deps.
docs-check:
	python3 scripts/check_docs.py --self-test
	python3 scripts/check_docs.py

# ---------------------------------------------------------------------------
# api/ (Python via uv)
# ---------------------------------------------------------------------------

api-lint:
	cd api && uv run ruff check .
	cd api && uv run ruff format --check .
	cd api && uv run mypy .
	cd api && uv run lint-imports

api-format:
	cd api && uv run ruff format .
	cd api && uv run ruff check --fix .

api-test:
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
	cd api && uv run python -m grpc_tools.protoc \
		-I ../proto \
		--python_out=src \
		--grpc_python_out=src \
		--mypy_out=src \
		--mypy_grpc_out=src \
		../proto/mcsd/controlplane/v1/control_plane.proto

# Drift gate: regenerate and fail if anything changed -- modified tracked stubs
# or new untracked ones (CI + `make check`).
proto-check: proto-gen
	@dirty="$$(git status --porcelain -- worker/internal/controlplane api/src/mcsd)"; \
	if [ -n "$$dirty" ]; then \
		echo "proto stubs are stale; run 'make proto-gen' and commit:"; \
		echo "$$dirty"; \
		git --no-pager diff -- worker/internal/controlplane api/src/mcsd; \
		exit 1; \
	fi

# Install the pinned Go protoc plugins into worker/.bin if missing.
$(PROTOC_GEN_GO):
	cd worker && GOBIN="$$(pwd)/.bin" go install \
		google.golang.org/protobuf/cmd/protoc-gen-go@$(PROTOC_GEN_GO_VERSION)

$(PROTOC_GEN_GO_GRPC):
	cd worker && GOBIN="$$(pwd)/.bin" go install \
		google.golang.org/grpc/cmd/protoc-gen-go-grpc@$(PROTOC_GEN_GO_GRPC_VERSION)
