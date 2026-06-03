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

.PHONY: all check lint format test \
	api-lint api-format api-test \
	worker-lint worker-format worker-test \
	bootstrap hooks-install

# golangci-lint is not part of the Go distribution; it is installed into a
# module-local, gitignored ./.bin (see worker/README.md).
GOLANGCI_VERSION := v2.12.2
GOLANGCI := worker/.bin/golangci-lint

all: check

# Full verification gate. Matches the pre-push hook and CI.
check: lint test

lint: api-lint worker-lint

format: api-format worker-format

test: api-test worker-test

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
	cd worker && ./.bin/golangci-lint run

worker-format:
	cd worker && gofmt -w .

worker-test:
	cd worker && go test ./...

# Install the pinned golangci-lint into worker/.bin if it is missing.
$(GOLANGCI):
	cd worker && GOBIN="$$(pwd)/.bin" go install \
		github.com/golangci/golangci-lint/v2/cmd/golangci-lint@$(GOLANGCI_VERSION)

# Bootstrap local tooling. uv installs the Python toolchain on first `uv run`,
# but syncing up front gives a clear, fast failure if the environment is wrong.
bootstrap: $(GOLANGCI)
	cd api && uv sync

# ---------------------------------------------------------------------------
# Git hooks
# ---------------------------------------------------------------------------

# Point git at the checked-in hooks. One command, no external dependency.
hooks-install:
	git config core.hooksPath .githooks
	@echo "git hooks installed (core.hooksPath -> .githooks)"

# ---------------------------------------------------------------------------
# proto/ (buf) -- extension point.
# proto/ does not exist on main yet (a sibling PR adds it; see #22). When it
# lands, wire `buf lint` into the lint target here, e.g.:
#   lint: api-lint worker-lint proto-lint
#   proto-lint:
#   	cd proto && buf lint
# ---------------------------------------------------------------------------
