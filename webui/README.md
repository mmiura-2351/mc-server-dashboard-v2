# webui/

The single-page web UI for mc-server-dashboard v2 (React + TypeScript + Vite).
A self-contained npm package mirroring how `api/` and `worker/` are
self-contained ([`docs/ui/WEBUI_SPEC.md`](../docs/ui/WEBUI_SPEC.md) Section 7.6).

This is the Phase 1 scaffold: a placeholder `App` wired through a one-route
router and a TanStack Query provider, plus a generated OpenAPI client and the
dev-server API proxy. The real application shell and repo/CI integration land in
later Phase 1 issues — the UI stays **same-origin** with the API, which ships no
CORS (Section 7.7).

## Prerequisites

- Node — the version is pinned in [`.nvmrc`](.nvmrc) (`nvm use`); see `engines`
  in [`package.json`](package.json).
- npm — pinned via `engines.npm` (enforced by `engine-strict=true` in
  [`.npmrc`](.npmrc)) and matched in CI, so `package-lock.json` regenerates
  deterministically. Run `npm install -g npm@<engines.npm>` if your npm differs.

## Setup

```sh
npm ci
```

Installs the toolchain from the committed `package-lock.json`.

## Commands

Run from this directory (`webui/`):

| Task | Command |
|---|---|
| Dev server | `npm run dev` |
| Build | `npm run build` |
| Test | `npm run test` |
| Lint | `npm run lint` |
| Format (apply) | `npm run format` |
| Type check | `npm run typecheck` |
| All gates | `npm run check` |
| Regenerate API client | `npm run openapi` |

`npm run check` aggregates lint + typecheck + test + build — the single entry
point later repo integration hooks into. The Vitest unit suite (`npm run test`)
covers `src/`; the Playwright E2E suite (below) is a separate, slower path that
is **not** part of `npm run check` or `make check`.

## End-to-end tests (Playwright)

The E2E suite (`webui/e2e/`) drives the real UI in a browser against a **real
API + Postgres** over the critical flows — register/login/session/logout, admin
community provisioning, the server-create wizard, and community member +
role management. Chromium only for the first cut.

The honest cheap setup: the suite runs against the Vite **dev** server (so the
dev-server proxy keeps the browser same-origin with the API, exactly as
production does), with the API booted against a throwaway Postgres, migrated,
and seeded with a platform admin.

One-time browser install:

```sh
npx playwright install chromium
```

Run the whole suite from the repo root (boots Postgres via Docker, the API, the
admin, and the browser, then tears it all down):

```sh
make webui-e2e
```

Pass extra `playwright test` args through `ARGS`, e.g. a single spec or the
Playwright UI:

```sh
make webui-e2e ARGS=auth.spec.ts
make webui-e2e ARGS=--ui
```

Notes:

- No worker runs in the harness, so a created server parks unassigned/stopped —
  the suite asserts creation + card rendering, not a running state.
- The server-create flow reads the real version catalog (Mojang/PaperMC/…); the
  API has no offline catalog seam, so that flow needs network to those
  manifests, the same dependency the API has in production.
- Override the API the UI proxies to with `MCD_E2E_API_URL`; the orchestration
  knobs (`MCD_E2E_API_PORT`, `MCD_E2E_PG_PORT`, `MCD_E2E_REUSE_DB` +
  `MCD_E2E_DATABASE_URL`) are documented in `scripts/run_webui_e2e.sh`.

## API client

The UI codes against a TypeScript client generated from the API's OpenAPI schema
(WEBUI_SPEC.md 7.6), not against hand-written types:

- [`openapi.json`](openapi.json) — the schema, exported from the FastAPI app.
- [`src/api/schema.ts`](src/api/schema.ts) — generated types
  ([openapi-typescript](https://openapi-ts.dev/)); do not edit by hand.
- [`src/api/client.ts`](src/api/client.ts) — a thin typed `fetch` helper keyed
  off the generated `paths`. Feature call sites build on this in later phases.

Both generated files are committed. Regenerate them whenever the API surface
changes:

```sh
npm run openapi
```

This runs two steps (also runnable on their own): `openapi:export` shells out to
`api/` (`uv run python -m mc_server_dashboard_api.export_openapi`) to dump the
schema hermetically — no running server, database, or network — then
`openapi:generate` runs `openapi-typescript`. A clean working tree after
`npm run openapi` means the committed client is up to date.

## Dev-server proxy

The browser only ever talks to the Vite dev server, which proxies the single
`/api` prefix — the entire HTTP API, REST **and** the WebSocket event streams —
to a local API, so the UI and the API share one origin and no CORS is added
anywhere (WEBUI_SPEC.md 7.7). Start a local API on its `http_port` (default
`8000`), then `npm run dev`; requests under `/api` reach the API, and every other
path falls through to the SPA. Since the whole API is namespaced under `/api`
(issue #498), no API path is ever also an SPA route, so the proxy needs no
Accept-header bypass for deep-links — that collision class is gone. Point the
proxy elsewhere with
`VITE_API_PROXY_TARGET` (e.g. `VITE_API_PROXY_TARGET=http://localhost:9000 npm run dev`).
