# Development Workflow

Day-to-day developer workflow for this monorepo: prerequisites, first-time
setup, the common commands, where code lives, the import-direction rules, and
the proto regeneration loop.

This document covers the *mechanics* of working in the tree. The change process
(issues, branches, commits, pull requests, review, merge) is in
[`CONTRIBUTING.md`](CONTRIBUTING.md); the test-driven discipline is in
[`TESTING.md`](TESTING.md); behavioral guidance for writing code is in
[`../../CLAUDE.md`](../../CLAUDE.md).

The repo is three services, a web UI, and a shared contract: `api/` (Python),
`worker/` and `relay/` (Go), `webui/` (React/TypeScript), and `proto/` (the
buf control-plane module). A root `Makefile` provides unified commands that
fan out to the per-module tooling.

## 1. Prerequisites

Install these once on your machine. Toolchain versions are pinned per module
(see [`DEPENDENCIES.md`](DEPENDENCIES.md)).

| Tool | For | Install |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | `api/` Python toolchain + dependencies (Python is pinned in `api/.python-version`) | per the uv docs |
| [Go](https://go.dev/dl/) 1.26 | `worker/` (pinned in `worker/go.mod`) | per the Go docs |
| [Node.js](https://nodejs.org/) ≥ 24.15 (+ npm 11) | `webui/` build, lint, and test (pinned in `webui/package.json`) | per the Node.js docs; npm comes bundled |
| [buf](https://buf.build) 1.70.0 | `proto/` lint + code generation | see [`../../proto/README.md`](../../proto/README.md) |
| GNU Make | the root unified commands | usually preinstalled; otherwise your OS package manager |

`webui/.nvmrc` pins only the Node major line (`24`), but the effective minimum
is raised by transitive dependencies (e.g. `jsdom`'s `^24.15.0` floor on the
v24 line) and enforced by `engine-strict=true` in `webui/.npmrc`. A stale 24.x
below 24.15 therefore fails `make bootstrap` hard at `npm ci` rather than
warning.

`golangci-lint` and the protoc plugins are **not** installed by hand — `make
bootstrap` fetches the pinned versions into the gitignored `worker/.bin/`.

## 2. First-time setup

From the repo root:

```sh
make bootstrap      # uv sync for api/, install pinned golangci-lint into worker/.bin
make hooks-install  # point git at the checked-in hooks (.githooks/)
```

`make bootstrap` installs the `api/` toolchain from `api/uv.lock` and the Go
linter; uv also provisions the toolchain lazily on the first `uv run`, but
bootstrapping up front fails fast if the environment is wrong.

`make hooks-install` sets `core.hooksPath` to `.githooks/`. The hooks then run
automatically:

- **pre-commit** — formats and lints the modules with staged changes. If
  auto-formatting modified a staged file, the commit aborts so you can review
  and re-stage.
- **pre-push** — runs the full `make check` gate (the same one CI runs).

Do not bypass a failing hook with `--no-verify`; fix the cause and commit again
([`CONTRIBUTING.md`](CONTRIBUTING.md) Section 4).

## 3. Common commands

Run from the repo root. Each target fans out to all modules; the 90% path is
covered here.

| Task | Command | Does |
|---|---|---|
| Format all modules | `make format` | ruff format + `ruff check --fix` (api), `gofmt -w` (worker, relay), biome (webui) |
| Lint + typecheck | `make lint` | ruff, mypy, import-linter (api); gofmt-check, `go vet`, golangci-lint (worker, relay); `buf lint` (proto); biome + tsc (webui) |
| Test | `make test` | `pytest` (api), `go test ./...` + `worker-e2e-compile` (worker), `go test ./...` + `relay-e2e-compile` (relay), vitest (webui) |
| Full gate | `make check` | `hooks-check` + `lint` + `test` + `webui-build` + `openapi-check` + `proto-check` + `docs-check` + `migrations-check` — what pre-push and CI run |
| Regenerate proto stubs | `make proto-gen` | regenerate the Go + Python control-plane stubs (Section 6) |
| Check proto stubs are current | `make proto-check` | regenerate and fail if the committed stubs drift |
| Regenerate webui OpenAPI client | `make openapi-gen` | regenerate `webui/openapi.json` + `webui/src/api/schema.ts` from the api routes |
| Check webui OpenAPI client is current | `make openapi-check` | regenerate and fail if the committed client artifacts drift |
| Install pinned local tooling | `make bootstrap` | golangci-lint into `worker/.bin`, `uv sync` for api, `npm ci` for webui |
| Install git hooks | `make hooks-install` | one-time, sets `core.hooksPath` |

With the hooks installed (Section 2) there is no need to run the full
`make check` by hand before pushing — pre-push runs it. Iterate with the
targeted subset instead ([`AGENTS.md`](AGENTS.md) Section 5).

### Per-module commands

When you are working inside one module, its README has the full command list
(format/lint/test/build, run from that module's directory):

- `api/` — [`../../api/README.md`](../../api/README.md)
- `worker/` — [`../../worker/README.md`](../../worker/README.md)
- `relay/` — [`../../relay/README.md`](../../relay/README.md)
- `webui/` — [`../../webui/README.md`](../../webui/README.md)
- `proto/` — [`../../proto/README.md`](../../proto/README.md)

## 4. Where things live

The module boundaries and the Hexagonal layout are specified in
[`../app/ARCHITECTURE.md`](../app/ARCHITECTURE.md) Section 3 (module
boundaries) and Section 2 (the layering). A pointer map:

| Path | What | Reference |
|---|---|---|
| `proto/` | The buf control-plane contract (the bidi-stream service, command/event messages). No logic. | [`ARCHITECTURE.md`](../app/ARCHITECTURE.md) Section 3.1 |
| `api/src/mc_server_dashboard_api/` | The authoritative Python service. | [`ARCHITECTURE.md`](../app/ARCHITECTURE.md) Section 3.1 |
| `api/src/mcsd/` | Generated control-plane stubs (do not edit). | Section 6 below |
| `api/tests/` | pytest tests. | [`TESTING.md`](TESTING.md) |
| `worker/cmd/worker/` | Worker edge / wiring: the process entry point. | [`ARCHITECTURE.md`](../app/ARCHITECTURE.md) Section 2.1 |
| `worker/internal/domain/` | Pure core: entities, value objects, Ports. | quadrant: `domain` |
| `worker/internal/application/` | Use cases, depending only on `domain`. | quadrant: `application` |
| `worker/internal/adapters/` | Concrete Port implementations (drivers, clients). | quadrant: `adapters` |
| `worker/internal/controlplane/` | Generated control-plane stubs (do not edit). | Section 6 below |
| `relay/` | The Go game-ingress relay (hostname routing, Worker dial-back tunnel). | [`RELAY.md`](../app/RELAY.md) |
| `webui/` | The React SPA. | [`../../webui/README.md`](../../webui/README.md) |

The Hexagonal quadrants (`domain`, `application`, `adapters`, edge) and the Port
catalog are in [`ARCHITECTURE.md`](../app/ARCHITECTURE.md) Sections 2 and 5. On
the `api/` side the bounded contexts under `api/src/mc_server_dashboard_api/`
keep their own `domain/ application/ adapters/ api/` quadrants.

## 5. Import-direction rules

Dependencies always point inward to `domain`:

```
edge  →  application  →  domain  ←  adapters
```

`domain` depends on nothing else in the project; `application` depends only on
`domain`; `adapters` depend on `domain` (for the Port interfaces they implement)
and on external libraries; the edge wires concrete adapters to Ports in one
place. The full rationale is in
[`../app/ARCHITECTURE.md`](../app/ARCHITECTURE.md) Section 2.2.

On the `api/` (Python) side these rules are mechanically enforced by
[import-linter](https://import-linter.readthedocs.io/), configured in
`api/pyproject.toml`. Run it via the root gate or directly:

```sh
make lint                  # runs import-linter as part of api-lint
cd api && uv run lint-imports   # just the import contracts
```

Each bounded context carries its own contracts — a layered contract
(`domain ← application ← api`) plus forbidden-import contracts that keep
`adapters` out of `domain` / `application` and keep contexts independent of
each other; a new context adds its own. On the `worker/` (Go) side the inward
direction is a convention checked in review; `go vet` and golangci-lint run via
`make lint`.

## 6. Proto regeneration

The control-plane stubs are **checked in** under each consumer so `api/` and
`worker/` build without a proto toolchain present:

- Go: `worker/internal/controlplane/`
- Python: `api/src/mcsd/`

Never edit the generated stubs by hand. When you change `proto/`, regenerate
from the repo root:

```sh
make proto-gen
```

This drives buf (Go) plus `grpc_tools.protoc` (Python) with pinned generators;
the mechanics and version pins are in [`../../proto/README.md`](../../proto/README.md).

Freshness is enforced by a drift gate: `make proto-check` (run by `make check`
and the proto CI workflow) regenerates and fails if the committed stubs differ.
If it fails, run `make proto-gen` and commit the result.

A `proto/` contract change is one atomic change set that updates `proto/`,
`api/`, and `worker/` together — never merge a contract change that leaves one
side uncompiled or unimplemented
([`CONTRIBUTING.md`](CONTRIBUTING.md) Section 5).
