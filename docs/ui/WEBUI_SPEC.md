# Web UI — Feature Inventory, Screen Map, and Spec

> Status: **Draft (for alignment)** · Date: 2026-06-06
>
> This document inventories the v2 API surface as implemented today, derives
> the full UI feature list from it, and proposes the screen structure and
> per-screen specs for the Web UI milestone. A static mockup (no real API
> calls) accompanies it under `docs/ui/mockup/`.
>
> REQUIREMENTS.md Section 2.2 places the production Web UI in a separate
> repository. This spec and the mockup live here temporarily for alignment;
> where the real implementation lands is an open question (Section 9).

## Table of Contents

1. [Decisions already made](#1-decisions-already-made)
2. [API surface inventory](#2-api-surface-inventory)
3. [Personas and capability scoping](#3-personas-and-capability-scoping)
4. [UI feature list](#4-ui-feature-list)
5. [Screen map](#5-screen-map)
6. [Screen specs](#6-screen-specs)
7. [Cross-cutting concerns](#7-cross-cutting-concerns)
8. [Out of scope for the first UI cut](#8-out-of-scope-for-the-first-ui-cut)
9. [Open questions](#9-open-questions)

---

## 1. Decisions already made

| Topic | Decision |
|---|---|
| Visual tone | Dark operations-console style (Grafana/Portainer family). |
| UI language | English, with all strings behind an i18n dictionary so Japanese can be added later. |
| Mockup form | Multiple static HTML pages + shared CSS/JS, mock data embedded in JS. No real API calls. |

## 2. API surface inventory

Complete endpoint list as of `main` (dumped from the FastAPI OpenAPI schema).
`[A]` = platform-admin axis; everything else is community-permission-gated.

### 2.1 Identity & auth

| Method | Path | Notes |
|---|---|---|
| POST | `/users` | Register (username, email, password). Public. |
| POST | `/auth/login` | username + password → `{access_token, refresh_token}` (bearer). |
| POST | `/auth/refresh` | refresh token → new pair. |
| POST | `/auth/logout` | invalidates the refresh token. |
| GET / PATCH / DELETE | `/users/me` | Profile read / update (username, email) / account deletion. |
| PUT | `/users/me/password` | Change password (current + new). |
| GET | `/users` `[A]` | Paginated user list (`limit`/`offset`, returns `total`, `active`, `created_at`). |
| POST | `/users/{id}/deactivate` · `/reactivate` `[A]` | Suspend / restore login. |
| PUT | `/users/{id}/platform-admin` `[A]` | Grant/revoke the admin flag. |
| DELETE | `/users/{id}` `[A]` | Delete a user. |

### 2.2 Communities, members, roles, grants

| Method | Path | Notes |
|---|---|---|
| GET | `/communities` | Communities the caller belongs to (admin: all). |
| POST | `/communities` `[A]` | Provision a community + initial owner. |
| GET / PATCH / DELETE | `/communities/{cid}` | Read / rename / delete. |
| GET / POST | `/communities/{cid}/members` | List (with `username`, `role_names`) / add an existing user. |
| DELETE | `/communities/{cid}/members/{uid}` | Remove member (revokes roles & grants). |
| POST / DELETE | `/communities/{cid}/members/{uid}/roles[/{rid}]` | Assign / unassign a role. |
| GET / POST | `/communities/{cid}/roles` | List / create custom role (name + permission codes). |
| GET / PATCH / DELETE | `/communities/{cid}/roles/{rid}` | Read / update / delete. Preset `Owner` role is `is_preset`. |
| GET / POST | `/communities/{cid}/grants` | List (`?user_id=` filter) / create per-resource grant. `resource_type` = `server` only; permission families `server:*`, `file:*`, `backup:*`. |
| DELETE | `/communities/{cid}/grants/{gid}` | Revoke. |

Permission catalog (community axis, 29 codes — the role/grant editor's source
of truth): `server:{create,read,update,delete,start,stop,restart,command}`,
`file:{read,edit,history,rollback}`, `backup:{create,read,restore,delete,schedule}`,
`member:{read,add,remove}`, `role:{read,manage}`, `grant:{read,manage}`,
`group:{read,manage}`, `community:{read,update,delete}`, `audit:read`.
Platform axis (flag-driven, not assignable to roles): `worker:manage`,
`community:provision`, `platform:monitor`.

### 2.3 Servers (lifecycle, console, files, backups, groups)

| Method | Path | Notes |
|---|---|---|
| GET / POST | `/communities/{cid}/servers` | List / create (`name`, `mc_edition`, `mc_version`, `server_type`, `execution_backend`, `config`, `accept_eula`, optional `game_port`). `spigot` → 422 `spigot_unsupported`. |
| POST | `/communities/{cid}/servers/import` | ZIP import (multipart). |
| GET | `…/{sid}/export` | ZIP export (download). |
| GET / PATCH / DELETE | `…/{sid}` | Read / update (name, config, game_port; backend immutable) / delete. |
| POST | `…/{sid}/start` · `/stop?force=` · `/restart` | Lifecycle. Stop supports force. |
| POST | `…/{sid}/command` | RCON line → `{output}`. |
| GET | `…/{sid}/files?path=&list=` | Read file (base64) or list directory (entries + `truncated`). |
| PUT / DELETE | `…/{sid}/files?path=` | Write (base64, versioned) / delete. |
| POST | `…/{sid}/files/directories?path=` | mkdir. |
| GET | `…/{sid}/files/download?path=` | Raw download. |
| POST | `…/{sid}/files/upload?path=&extract=` | Multipart upload, optional ZIP extract. |
| POST | `…/{sid}/files/rename` | `{from, to}`. |
| POST | `…/{sid}/files/search` | `{query, by, max_results}` → matching paths. |
| GET | `…/{sid}/files/history?path=` | Retained version ids. |
| POST | `…/{sid}/files/rollback?path=` | `{version_id}`. |
| GET / POST | `…/{sid}/backups` | List / create on-demand backup. |
| GET | `…/{sid}/backups/statistics` | count / total bytes / newest / oldest. |
| POST | `…/{sid}/backups/upload` | Upload an off-host backup archive. |
| GET | `…/{sid}/backups/{bid}/download` | Download archive. |
| POST | `…/{sid}/backups/{bid}/restore` | **Server must be stopped.** |
| DELETE | `…/{sid}/backups/{bid}` | Delete. |
| GET | `…/{sid}/groups` | Groups attached to this server. |
| GET / POST | `/communities/{cid}/groups` | Player groups (`kind`: `op` \| `whitelist`). |
| GET / PATCH / DELETE | `…/groups/{gid}` | Read / rename / delete. |
| POST / DELETE | `…/groups/{gid}/players[/{uuid}]` | Add / remove player (uuid + username). |
| GET / PUT / DELETE | `…/groups/{gid}/servers[/{sid}]` | List / attach / detach server. |

Server state model: `desired_state` ∈ {running, stopped};
`observed_state` ∈ {starting, running, stopping, stopped, restarting, crashed,
unknown} + `observed_at` + `assigned_worker_id`.
Server types: vanilla / paper / fabric / forge (spigot persisted but rejected).
Execution backends: `host_process` / `container`.

### 2.4 Versions, ports, fleet, audit, platform

| Method | Path | Notes |
|---|---|---|
| GET | `/versions` | Catalogued server types. |
| GET | `/versions/{type}` | Version list for a type. |
| POST | `/versions/refresh` `[A]` | Invalidate catalog cache (`?server_type=` optional). |
| GET | `/versions/jar-pool/stats` `[A]` · POST `/versions/jar-pool/gc` `[A]` | JAR pool size / garbage collection. |
| GET | `/ports/available?count=` · `/ports/check/{port}` | Free-port discovery / conflict check. |
| GET | `/workers` `[A]` | Fleet list: status, capabilities (drivers, max_servers, cpu/mem), assigned_count, heartbeat. |
| PUT / DELETE | `/workers/{wid}/drain` `[A]` | Set / clear drain. |
| GET | `/audit` `[A]` | Global audit (`community`, `operation`, `actor`, `since`, `until`, `limit`, `offset`). |
| GET | `/communities/{cid}/audit` | Community-scoped audit (same filters minus `community`). |
| GET | `/backups/statistics` `[A]` | Global backup statistics. |
| GET | `/healthz` · `/readyz` · `/metrics` | Liveness / readiness / Prometheus (ops-facing, not UI-core). |

### 2.5 Real-time (WebSocket)

| Path | Notes |
|---|---|
| `WS /communities/{cid}/servers/{sid}/events?streams=status,log,metrics&token=…` | Typed frames `{stream, ts, payload}`. `status`: `{state, detail}` · `log`: `{line, stream}` · `metrics`: `{cpu_millis, memory_bytes, player_count}` · `gap`: client fell behind (always delivered). |
| `WS /communities/{cid}/events?token=…` | Community-wide **status-only** firehose; frames carry `server_id`. |

Auth: browsers pass the access token as `?token=` (header also honored for
non-browser clients). Close codes mirror REST: 4400 bad `streams`, 4401
unauthenticated, 4403 forbidden, 4404 not found / not a member. Authorization
is re-checked every 60 s mid-stream. Delivery is best-effort; REST keeps
working if the socket dies (FR-MON-4).

Note: the data-plane endpoints (`/data-plane/...`) are Worker-credential-only
transfer endpoints — not part of the UI surface.

## 3. Personas and capability scoping

| Persona | Sees | Typical UI surface |
|---|---|---|
| Community member | Only their communities; actions filtered by role permissions ∪ grants | Servers list, server detail (capabilities vary per permission) |
| Community owner | Everything in their community | + Members, Roles, Grants, Groups, Audit, Community settings |
| Platform administrator | All communities + platform area | + Admin area: Users, Communities provisioning, Workers, Version catalog/JAR pool, Global audit & backup stats |
| Unauthenticated | Login / Register only | — |

The API exposes no "my permissions" endpoint. The UI therefore derives
capabilities client-side from `GET /users/me` (admin flag) + the member's
roles/grants where readable, **and treats any 403/404 as the authority**
(FR-AUTHZ-6: server-side enforcement is the truth; client scoping is
convenience). Where the member cannot read roles, the UI shows actions and
handles rejection gracefully (see 7.3 and Open question Q3).

## 4. UI feature list

Grouped; each maps 1:1 to the endpoints in Section 2.

**Auth & account** — login, register, logout, token refresh (transparent),
profile edit, password change, account deletion.

**Community workspace** — community switcher; dashboard with live server
tiles (community WS); community rename/delete.

**Server operations** — create (wizard: type → version → backend → port →
EULA), import ZIP, export ZIP, start/stop/force-stop/restart, delete,
live status & uptime via WS, RCON console with command history, live log
viewer (follow/pause/filter), metrics strip (CPU/mem/players).

**File management** — directory browser, text-file editor (base64 transport),
upload (w/ ZIP extract), download, rename, delete, mkdir, search,
per-file version history + rollback.

**Backups** — on-demand create, list w/ statistics, download, upload,
restore (stopped-only, guarded confirm), delete.

**Player groups** — op/whitelist groups, player add/remove, attach/detach
to servers, per-server attached-group view.

**Membership & access** — members list/add/remove, role assign/unassign,
role editor over the 29-code catalog, per-server grants editor.

**Audit** — community audit log w/ filters; global audit (admin).

**Platform admin** — user management (list/deactivate/reactivate/delete/
admin-flag), community provisioning, worker fleet view + drain/undrain,
version catalog refresh, JAR pool stats/GC, global backup statistics.

**Utilities** — port availability picker in server create/edit.

## 5. Screen map

```
/login                    Login (→ /register)
/register                 Self-service registration

(authenticated shell: top bar = community switcher · user menu;
 left nav = community scope; admin area is a separate nav group)

/communities/:cid                      Dashboard (server tiles, live status)
/communities/:cid/servers/new         Server create wizard
/communities/:cid/servers/:sid        Server detail
   #overview   status / controls / metrics / live log tail
   #console    RCON console + full log stream
   #files      file browser / editor / history
   #backups    backups + statistics
   #players    attached op/whitelist groups
   #settings   name / config / port / export / danger zone
/communities/:cid/settings            Community settings
   #members    members + role assignment
   #roles      role editor
   #grants     per-server grants
   #groups     player groups (community-wide)
   #audit      community audit log
   #general    rename / delete
/account                              Profile / password / sessions / delete

/admin                                Platform overview (workers summary, global stats)
/admin/users                          User management
/admin/communities                    Provision / list communities
/admin/workers                        Fleet (capabilities, load, heartbeat, drain)
/admin/versions                       Catalog + JAR pool
/admin/audit                          Global audit log
```

Navigation model: **community is the primary scope** (switcher in the top
bar, like an org switcher). Admin pages appear only for platform admins.

## 6. Screen specs

### 6.1 Login / Register
- Login: username + password → store token pair; on 401 show inline error
  (brute-force lockout surfaces as a generic failure — do not leak detail).
- Register: username / email / password + client-side strength hints
  mirroring FR-AUTH-4 (min length, no username/email inside password);
  server remains authoritative.

### 6.2 Dashboard (community home)
- Grid of server cards: name, type/version badge, backend badge, observed
  state pill (color-coded: running=green, starting/stopping/restarting=amber,
  crashed=red, stopped=gray, unknown=striped), game port, assigned worker.
- Live updates over `WS /communities/{cid}/events` (status stream); on WS
  loss fall back to 10s polling of `GET …/servers` with a "live degraded"
  indicator.
- Quick actions on card: start / stop / restart (permission-scoped).
- Empty state → CTA to the create wizard.

### 6.3 Server create wizard
1. **Type & version** — type cards from `GET /versions`; version dropdown
   from `GET /versions/{type}` (latest preselected). Spigot shown disabled
   with "use Paper" hint.
2. **Runtime** — execution backend (host_process / container), game port:
   auto-suggest from `GET /ports/available`, validate via
   `GET /ports/check/{port}` on blur.
3. **Config & EULA** — name, optional `server.properties` overrides (key
   editor), EULA checkbox (required to start later; create allows deferred
   acceptance — surfaced as a warning).
4. Create → navigate to server detail. Alternative path: "Import ZIP" tab on
   step 1 → upload form (`POST …/servers/import`).

### 6.4 Server detail — Overview
- Header: name, state pill (+ `detail` from last status event, e.g. crash
  category), desired-vs-observed mismatch hint ("starting…" spinner while
  reconciler converges), worker id, port.
- Controls: Start / Stop (dropdown: graceful · force) / Restart / Export /
  Delete — each disabled by state machine (e.g. Start hidden while running)
  and permission.
- Metrics strip: CPU / memory / players from the `metrics` stream (sparkline,
  last N samples, client-side only).
- Log tail: last ~200 lines, link to Console tab.
- Single `WS …/{sid}/events` connection shared by all tabs of this page;
  `gap` frames render as an inline "missed events" divider.

### 6.5 Server detail — Console
- Full log stream (stdout/stderr color-keyed), follow-mode toggle, text
  filter, clear.
- RCON input (`POST …/command`) with local history (↑/↓); command + `output`
  echoed into the stream view, distinct styling.
- Disabled with hint when server not running.

### 6.6 Server detail — Files
- Two-pane: directory tree / listing (with `truncated` notice) + viewer.
- Text files open in an editor (save = versioned write); binary → download
  only. Path breadcrumbs; upload (w/ "extract ZIP" toggle), mkdir, rename,
  delete; search box (`files/search`).
- History drawer per file: version list → rollback with confirm.
- Edits against a running server show "live working set — may need restart"
  notice (Section 6.9 semantics).

### 6.7 Server detail — Backups
- Stats header (count, total size, newest/oldest).
- Table: created_at, source (manual/scheduled/pre-restore…), size, creator;
  actions: download, restore, delete.
- Restore: blocked with explanation while running (offer "stop now then
  restore" two-step); typed-confirm dialog.
- Create backup button (works on running servers — save-all + snapshot path);
  upload backup (file picker).
- Schedule: per-server interval via the `backup_interval_hours` key on the
  server `config` blob (`PATCH …/servers/{sid}`) — no dedicated endpoint; the
  UI exposes it as an "every N hours" field (absent = no scheduled backups).

### 6.8 Server detail — Players
- Attached groups (`GET …/{sid}/groups`) with kind badges; attach/detach
  pickers from community groups; inline link to the community Groups tab.

### 6.9 Server detail — Settings
- Rename, game-port edit (with availability check), `config` key/value
  editor; execution backend displayed read-only (immutable post-create).
- Danger zone: delete server (typed confirm), export ZIP.

### 6.10 Community settings
- **Members**: table (username, roles as chips); add-member dialog (by
  username — see Q4 on user lookup); role chips editable inline; remove with
  confirm (explains grant/role revocation).
- **Roles**: list (preset Owner locked); editor = name + permission-matrix
  grouped by family (server/file/backup/member/role/grant/group/community/
  audit) with select-all per family.
- **Grants**: per-user list (user filter); create = pick member → pick server
  → pick permissions (restricted to server/file/backup families).
- **Groups**: op/whitelist groups; player list (uuid + name) with add/remove;
  attached-servers list with attach/detach.
- **Audit**: filterable table (operation, actor, since/until, paging).
- **General**: rename; delete (typed confirm; admin/owner only).

### 6.11 Account
- Profile (username/email) edit, password change (current + new + confirm),
  logout, delete account (typed confirm + password).

### 6.12 Admin area
- **Overview**: worker count by status, total servers running, global backup
  stats, jar-pool stats.
- **Users**: paginated table (username, email, active, admin flag,
  created_at); actions: deactivate/reactivate, grant/revoke admin, delete.
- **Communities**: list all; provision dialog (name + initial owner user).
- **Workers**: table (id, version, status incl. draining, drivers,
  assigned/max, cpu/mem, heartbeat age); drain/undrain toggle with confirm.
- **Versions**: per-type catalog freshness, refresh button (all or one type);
  JAR pool stats + GC trigger showing reclaimed bytes.
- **Audit**: global log with community filter added.

## 7. Cross-cutting concerns

### 7.1 Auth/session lifecycle
- Access token (short-lived; ~900 s in the live deployment) kept in memory;
  refresh token persisted (see Q2 for storage choice). Transparent refresh on
  401 + single-flight refresh mutex; hard logout on refresh failure.
- WS connections carry `?token=`; on token rotation, sockets are reconnected
  (or left until the 60 s re-auth closes them — reconnect-on-rotate chosen).

### 7.2 Real-time strategy
- One WS per open server-detail page + one community WS for the dashboard.
- Reconnect with exponential backoff + jitter; resubscribe on open; banner
  shows degraded mode; REST polling fallback for status only.

### 7.3 Permission-driven rendering
- Capabilities derived client-side where possible; every denied action is
  still handled at response time (403 toast "you lack server:start"; 404
  treated as nonexistence per the no-existence-signal posture).
- UI never invents authority: buttons may be optimistically shown when
  capability is unknown, but failures degrade politely.

### 7.4 Errors & confirmations
- API error envelope surfaced via toast + inline field errors (422 detail).
- Conflict-flavored errors (e.g. lifecycle races, `server_unsettled`-style
  responses) get a "state changed — refresh" treatment, not a raw error dump.
- Destructive operations (delete server/community/user/backup-restore) use
  typed-confirm dialogs.

### 7.5 i18n & theming
- All strings through a `t('key')` dictionary; English shipped, Japanese
  addable. Dark theme via CSS custom properties (a light theme later is a
  token swap, not a rewrite).

### 7.6 Tech stack (proposal — for the real implementation, not the mockup)
- SPA: **React + TypeScript + Vite**, TanStack Query (REST cache +
  invalidation), plain WebSocket wrappers, CSS modules or vanilla-extract —
  no heavy UI kit; the design system stays ours. Generated API client from
  the OpenAPI schema. *(Alternatives welcome — see Q1.)*

## 8. Out of scope for the first UI cut

- Metrics history/persistence (only live sparklines from the WS stream).
- `/metrics` (Prometheus) visualization — operators use Grafana.
- Mobile-optimized layouts (responsive down to tablet only).
- Light theme (structure ready, not shipped).

## 9. Open questions

- **Q1. Stack**: agree on React+TS+Vite (7.6) or prefer something else
  (Svelte, htmx + server templates, …)?
- **Q2. Refresh-token storage**: `localStorage` (simple, XSS-exposed) vs
  cookie + API change (httpOnly; needs CSRF posture). Default proposal:
  localStorage for the first cut, revisit before production exposure.
- **Q3. "My permissions" endpoint**: a tiny
  `GET /communities/{cid}/me/permissions` would remove all client-side
  permission guessing (7.3). Confirmed absent from the API surface — filed as
  issue #354.
- **Q4. Member-add lookup**: `POST …/members` needs a user id; there is no
  non-admin user search endpoint (`GET /users` is platform-admin-only).
  Confirmed gap — filed as issue #355. Mockup assumes exact-username entry.
- **Q5. Where does the real UI live**: separate repo per REQUIREMENTS.md, or
  a `webui/` package in this monorepo (mirroring the api/worker/proto
  layout)?
