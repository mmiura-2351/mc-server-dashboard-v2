# Documentation Index

Long-form documentation for the Minecraft Server Dashboard **v2**. A top-level
`README.md` (planned) will carry the elevator pitch and quick start; everything
that needs more than a paragraph lives here.

> **This repository is in early construction.** The canonical document today is
> [`REQUIREMENTS.md`](REQUIREMENTS.md). The application and development docs
> below are filled in as the system is designed and built; entries marked
> *(planned)* do not exist yet.

Docs are split by intent:

- **`app/`** — how the running system works: architecture,
  persisted data, the HTTP surface, the API↔Worker control-plane contract,
  runtime configuration, cross-cutting behaviour. Read these when reasoning
  about the system itself.
- **`dev/`** — how to work *on* the system: development workflow, testing
  discipline, release procedure, dependency policy. Read these when changing the
  codebase or operating a deployment.

---

## Requirements

| Doc | What it covers |
|---|---|
| [`REQUIREMENTS.md`](REQUIREMENTS.md) | What v2 must do and the architectural constraints it must satisfy: the API/Worker split, pluggable execution, the Community model, two-layer authorization, data/storage lifecycle, and the resolved design decisions. The source of truth for scope. |

## Application docs (`app/`)

| Doc | What it covers |
|---|---|
| [`app/ARCHITECTURE.md`](app/ARCHITECTURE.md) | The Hexagonal (Ports & Adapters) layering, the `api/` / `worker/` / `proto/` module boundaries and dependency direction, the catalog of domain Ports per side, and the architecture-level design decisions from `REQUIREMENTS.md` Section 9.1. |
| [`app/CONFIGURATION.md`](app/CONFIGURATION.md) | Runtime configuration for `api/` and `worker/`: sources and precedence, secret handling, config-driven adapter selection (Storage backend, token service, execution drivers), the authentication-hardening knobs and defaults, and snapshot-cadence settings. |
| [`app/DATABASE.md`](app/DATABASE.md) | The persistence model for the core entities (`REQUIREMENTS.md` Appendix B): tables, keys, relationships, the desired/observed-state split on `Server`, cascade behavior, and the M1 persistence-technology decision behind the persistence Port. Metadata only — bulk artifacts live in `Storage`. |

More entries are written during the design phase: the `proto/` control-plane
contract reference, `CONFIGURATION.md`, `DATABASE.md`, and the storage/execution
adapter references.

## Development docs (`dev/`)

| Doc | What it covers |
|---|---|
| [`dev/CONTRIBUTING.md`](dev/CONTRIBUTING.md) | The change workflow: issues, branch naming, commits, pull requests, review hygiene, and squash-merge. |
| [`dev/TESTING.md`](dev/TESTING.md) | The test-driven development discipline (Kent Beck): the red/green/refactor cycle, working disciplines, Tidy First, and what a good test looks like. Concrete tooling is per-component and forthcoming. |
| [`dev/RELEASING.md`](dev/RELEASING.md) | Versioning policy (a single repository-wide SemVer version), tag naming, and generated release notes (no hand-maintained CHANGELOG). Release automation and the version source-of-truth are forthcoming. |
| [`dev/DEPENDENCIES.md`](dev/DEPENDENCIES.md) | Pinning style, the 7-day supply-chain cooldown, security-update handling, and the automated-update policy across the Python and Go ecosystems. |
| `dev/DEVELOPMENT.md` *(planned)* | Day-to-day developer workflow: setup, common commands, debugging. Written once the toolchain lands. |

---

## Conventions

- **Language**: all documentation is English.
- **Versioning terms**: *legacy* = the old `mc-server-dashboard-api`; *v2* =
  this rebuild; *M1, M2, …* = milestones of v2. Never write "v1" for the new
  system (see `REQUIREMENTS.md`).
- **Filenames**: `UPPERCASE_SNAKE_CASE.md`. The subdirectory names (`app/`,
  `dev/`) are lowercase.
- **Section references**: write `Section 4.3` (or `section 4.3` mid-sentence).
  Do not use the section-mark glyph — it is uncommon on US keyboards and noisy
  to search for.
- **Cross-links**: use relative paths (`[RELEASING.md](RELEASING.md)` within
  the same subdirectory, `[REQUIREMENTS.md](../REQUIREMENTS.md)` across them).
