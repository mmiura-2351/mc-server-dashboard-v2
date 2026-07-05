# api/

Authoritative API service for mc-server-dashboard v2 (Python), built on FastAPI
+ async SQLAlchemy + Alembic
([`docs/app/ARCHITECTURE.md`](../docs/app/ARCHITECTURE.md) Section 7.4). Each
bounded context follows the Hexagonal `domain / application / adapters / api`
quadrant layout (Section 2); feature domains land on this skeleton.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — manages the Python toolchain and
  dependencies. The Python version is pinned in `.python-version`.

## Setup

```sh
uv sync
```

Installs the pinned Python and the dev toolchain into `.venv/`, resolving from
the committed `uv.lock`.

## Commands

Run from this directory (`api/`):

| Task | Command |
|---|---|
| Lint | `uv run ruff check .` |
| Format check | `uv run ruff format --check .` |
| Format (apply) | `uv run ruff format .` |
| Type check | `uv run mypy .` |
| Test | `uv run pytest` |
| Import contracts | `uv run lint-imports` |

## Configuration

Configuration is read at startup with `defaults < TOML file < MCD_API_ env`
precedence (CONFIGURATION.md Sections 1–3). Secrets are env-only and masked in
logs. See [`../docs/app/CONFIGURATION.md`](../docs/app/CONFIGURATION.md)
Section 5 for the full key reference. A TOML config file is optional; point at
it with `MCD_API_CONFIG_FILE`.

## Run the dev server

```sh
export MCD_API_DATABASE__URL="postgresql+asyncpg://mcsd:mcsd@localhost:5432/mcsd"
uv run uvicorn mc_server_dashboard_api.app:create_app --factory --reload
```

Then `curl http://127.0.0.1:8000/healthz` — it returns `{"ok": ...,
"database_reachable": ...}`; `ok` is `false` (HTTP 200, degraded) when the
database is unreachable rather than crashing.

## Migrations (Alembic)

The migration chain starts from an empty baseline; entity tables land with their
features (DATABASE.md). The DB URL is read from `MCD_API_DATABASE__URL`, not from
`alembic.ini`.

```sh
uv run alembic upgrade head        # apply migrations
uv run alembic revision -m "..."   # author a new migration
```

## Layout

```
api/
├── src/mc_server_dashboard_api/   # package source (src-layout)
│   ├── app.py dependencies.py …   # edge: app factory, DI wiring, config, logging
│   └── core/                      # first bounded context (health + infra)
│       ├── domain/ application/   # pure core + use cases (Ports only)
│       └── adapters/ api/         # DB adapter + HTTP router
├── src/mcsd/                      # generated control-plane stubs (do not edit)
├── migrations/                    # Alembic env + versions
└── tests/                         # pytest tests (unit) + tests/integration
```

The `src/mcsd/` tree is the generated gRPC control-plane contract (package
`mcsd.controlplane.v1`). It is checked in; regenerate with `make proto-gen` from
the repo root (see [`../proto/README.md`](../proto/README.md)). It is excluded
from the ruff/mypy gates as machine-generated code.

Import-direction contracts (import-linter, configured in `pyproject.toml`)
enforce the Hexagonal dependency rules from ARCHITECTURE.md Section 2.2: per
context `domain` imports nothing internal, `application` depends only on
`domain`, `adapters` are not imported by `domain`/`application`, and adapters are
bound to Ports only in the wiring module (`dependencies.py`).
