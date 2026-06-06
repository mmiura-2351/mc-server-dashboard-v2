# webui/

The single-page web UI for mc-server-dashboard v2 (React + TypeScript + Vite).
A self-contained npm package mirroring how `api/` and `worker/` are
self-contained ([`docs/ui/WEBUI_SPEC.md`](../docs/ui/WEBUI_SPEC.md) Section 7.6).

This is the Phase 1 scaffold: a placeholder `App` wired through a one-route
router and a TanStack Query provider. The real application shell, the dev-server
API proxy, and repo/CI integration land in later Phase 1 issues — the UI stays
**same-origin** with the API, which ships no CORS (Section 7.7).

## Prerequisites

- Node — the version is pinned in [`.nvmrc`](.nvmrc) (`nvm use`); see `engines`
  in [`package.json`](package.json).

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

`npm run check` aggregates lint + typecheck + test + build — the single entry
point later repo integration hooks into.
