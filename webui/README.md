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
point later repo integration hooks into.

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

The browser only ever talks to the Vite dev server, which proxies the API paths
**and** the WebSocket event streams to a local API — so the UI and the API share
one origin and no CORS is added anywhere (WEBUI_SPEC.md 7.7). Start a local API
on its `http_port` (default `8000`), then `npm run dev`; requests under the API
roots (`/auth`, `/users`, `/admin`, `/communities`, `/workers`, `/versions`,
`/ports`, `/audit`, `/backups`, plus the ops endpoints) reach the API, and every
other path falls through to the SPA. Point the proxy elsewhere with
`VITE_API_PROXY_TARGET` (e.g. `VITE_API_PROXY_TARGET=http://localhost:9000 npm run dev`).
