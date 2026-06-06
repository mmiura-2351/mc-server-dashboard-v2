# Web UI — Roadmap

> Status: Accepted · Date: 2026-06-06
>
> Coarse-grained phases for building `webui/` (design: [WEBUI_SPEC.md](WEBUI_SPEC.md),
> mockup: `docs/ui/mockup/`). Each phase ships independently mergeable work and
> ends in a state the next phase builds on; fine-grained issues are cut per
> phase when it starts, not up front.

## Phase 0 — API contract stabilization (prerequisite, API-side)

Stabilize the contracts the UI will be written against, while breaking changes
are still cheap (pre-release window):

- #371 — standardize the HTTP error body shape (`{reason}` everywhere).
- #369 — refresh-reuse detection vs. legitimate concurrent SPA refresh
  (multi-tab / retry-after-timeout must not revoke the family).
- #372 — Set-Cookie hygiene for body-only clients (rides along with #369).
- #367 — auth endpoint status-code contract doc (written against the above).

**Done when:** the UI's auth layer and error handling have a single, documented
contract to target.

## Phase 1 — Scaffold & foundation

`webui/` package at the repo root (React + TypeScript + Vite):

- Tooling: lint/format/typecheck/test wired into the repo's check flow; CI.
- Generated OpenAPI client from the API schema; dev-server proxy to the API
  (same-origin posture, issue #363 assumption).
- Design tokens ported from the mockup CSS (dark theme via custom properties),
  i18n dictionary skeleton (`t()`), app shell (top bar / sidebar / routing),
  toast + modal + confirm primitives.

**Done when:** `webui/` builds, tests and lints in CI; the shell renders
against a running API (no features yet).

## Phase 2 — Auth & session

- Login / register pages; httpOnly-cookie session flow (#363/#365 transport),
  in-memory access token, single-flight transparent refresh, route guards,
  hard-logout path.
- Account page (profile, password, memberships, account deletion).
- Capability loading: `GET /communities/{cid}/me/permissions` fetch-on-switch
  + session cache + re-fetch-on-403 (SPEC 7.3).

**Done when:** a user can sign in, stay signed in across reloads and token
expiry, and the app knows their effective permissions per community.

## Phase 3 — Dashboard & live status

- Community switcher; dashboard server cards (state pills, quick actions).
- Community events WebSocket with reconnect/backoff and the degraded-mode
  polling fallback (SPEC 7.2).

**Done when:** the landing experience works end-to-end with live status.

## Phase 4 — Server operations

- Create wizard (type/version catalog, backend, port check, EULA) + ZIP import.
- Server detail: Overview (metrics strip, log tail, lifecycle controls with
  state gating), Console (log stream + RCON), Settings (rename, port, config,
  export, delete).

**Done when:** the full server lifecycle is operable from the UI.

## Phase 5 — Files, backups, players

- Files tab: browser, editor, upload/download/rename/mkdir/delete, search,
  history + rollback, running-server notices.
- Backups tab: list/stats, create, upload/download, stopped-only restore flow,
  schedule field (`backup_interval_hours` via server config).
- Players tab: attached op/whitelist groups, attach/detach.

**Done when:** all per-server data management from the spec is usable.

## Phase 6 — Community administration

- Community settings tabs: Members (add by username — 422/409 handling),
  Roles (30-code permission matrix), Grants, Groups, Audit log, General
  (rename/delete).

**Done when:** a community owner needs no API calls outside the UI.

## Phase 7 — Platform admin area

- Admin pages: overview, users (lifecycle + admin flag; "create user" depends
  on #368 if registration-closed deploys are supported), communities
  provisioning, workers (drain/undrain), versions & JAR pool, global audit.

**Done when:** platform operation needs no direct API/SQL access.

## Phase 8 — Hardening & release

- E2E tests (Playwright) over the critical flows; error/empty/loading-state
  polish; accessibility pass.
- Serving strategy for production (same-origin behind the reverse proxy),
  deployment docs (docs/dev/DEPLOYMENT.md), release integration.
- Optional: Japanese translation of the i18n dictionary.

**Done when:** the UI is deployed alongside the live API and covered by CI/E2E.

---

Ordering rationale: phases 2–7 are vertical slices in decreasing
blast-radius-of-change order — auth and live-status plumbing (2–3) shape
everything after them, while admin pages (7) touch nothing else. Phases 4–7
can overlap if parallel tracks are wanted; 0→1→2→3 is strictly sequential.
