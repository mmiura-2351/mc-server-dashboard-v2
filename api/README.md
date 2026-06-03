# api/

Authoritative API service for mc-server-dashboard v2 (Python). The package
skeleton; domain code lands later per
[`docs/app/ARCHITECTURE.md`](../docs/app/ARCHITECTURE.md) Section 2.

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

## Layout

```
api/
├── src/mc_server_dashboard_api/   # package source (src-layout)
├── src/mcsd/                      # generated control-plane stubs (do not edit)
└── tests/                         # pytest tests
```

The `src/mcsd/` tree is the generated gRPC control-plane contract (package
`mcsd.controlplane.v1`). It is checked in; regenerate with `make proto-gen` from
the repo root (see [`../proto/README.md`](../proto/README.md)). It is excluded
from the ruff/mypy gates as machine-generated code.

Import-direction contracts (import-linter, configured in `pyproject.toml`)
enforce the Hexagonal dependency rules from ARCHITECTURE.md Section 2.2; the
contract is minimal until the domain packages exist.
