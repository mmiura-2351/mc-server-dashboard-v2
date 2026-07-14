# Web UI — Feature Inventory, Screen Map, and Spec

> Status: **Accepted (open questions resolved)** · Date: 2026-06-06
>
> This document inventories the v2 API surface as implemented today, derives
> the full UI feature list from it, and specifies the screen structure and
> per-screen specs for the Web UI milestone. A static mockup (no real API
> calls) accompanies it under `docs/ui/mockup/` and is kept as a design
> reference.
>
> The Web UI is built **in this monorepo** under `webui/`, alongside `api/`,
> `worker/`, and `proto/` (REQUIREMENTS.md Section 1.2). The former open
> questions are resolved in Section 9.

## Table of Contents

1. [Decisions already made](#1-decisions-already-made)
2. [API surface inventory](#2-api-surface-inventory)
3. [Personas and capability scoping](#3-personas-and-capability-scoping)
4. [UI feature list](#4-ui-feature-list)
5. [Screen map](#5-screen-map)
6. [Screen specs](#6-screen-specs)
7. [Cross-cutting concerns](#7-cross-cutting-concerns)
8. [Out of scope for the first UI cut](#8-out-of-scope-for-the-first-ui-cut)
9. [Resolved open questions](#9-resolved-open-questions)

---

## 1. Decisions already made

| Topic | Decision |
|---|---|
| Visual tone | Dark operations-console style (Grafana/Portainer family). |
| UI language | English, with all strings behind an i18n dictionary so Japanese can be added later. |
| Mockup form | Multiple static HTML pages + shared CSS/JS, mock data embedded in JS. No real API calls. |
| Placement | `webui/` in this monorepo, alongside `api/` / `worker/` / `proto/`. The mockup stays under `docs/ui/mockup/` as a design reference. |
| Stack | React + TypeScript + Vite (Section 7.6). |
| Session storage | Refresh token in an httpOnly cookie from the start (Section 7.1); requires API-side cookie support — issue #363. |

## 2. API surface inventory

Complete endpoint list as of `main` (dumped from the FastAPI OpenAPI schema).
`[A]` = platform-admin axis; everything else is community-permission-gated.

> **`/api` prefix (issue #498).** The entire HTTP API — every path in the tables
> below, the WebSocket endpoints in 2.5, and the OpenAPI schema/docs — is
> namespaced under `/api` so it can never collide with an SPA client-side route
> (see 7.7). Paths are written here without the prefix for brevity; the real path
> is `/api` + the listed path (e.g. `/api/auth/login`,
> `/api/communities/{id}`). The typed client carries the prefix automatically:
> the generated schema paths already include `/api`.

### 2.1 Identity & auth

| Method | Path | Notes |
|---|---|---|
| POST | `/users` | Register (username, email, password). Public. |
| POST | `/auth/login` | username + password → `{access_token, token_type}` (bearer); refresh token via cookie only (#636). |
| POST | `/auth/session` | refresh cookie → `{access_token}` only; non-rotating bootstrap (7.1, #512). |
| POST | `/auth/refresh` | refresh token → new pair (rotates). |
| POST | `/auth/logout` | invalidates the refresh token. |
| GET / PATCH / DELETE | `/users/me` | Profile read / update (username, email) / account deletion. |
| PUT | `/users/me/password` | Change password (current + new). |
| GET | `/admin/users` `[A]` | Paginated user list (`limit`/`offset`, returns `total`, `active`, `created_at`). |
| POST | `/admin/users` `[A]` | Create a user (username, email, password); exempt from the open-registration switch and per-IP cap. |
| POST | `/admin/users/{id}/deactivate` · `/reactivate` `[A]` | Suspend / restore login. |
| PUT | `/admin/users/{id}/platform-admin` `[A]` | Grant/revoke the admin flag. |
| DELETE | `/admin/users/{id}` `[A]` | Delete a user. |

### 2.2 Communities, members, roles, grants

| Method | Path | Notes |
|---|---|---|
| GET | `/communities` | Communities the caller belongs to (membership-scoped; the admin axis does not pierce isolation — #489). |
| GET | `/admin/communities` `[A]` | All communities with `member_count`/`server_count` (`limit`/`offset`, returns `total`); the platform-axis listing (#489). |
| POST | `/communities` `[A]` | Provision a community + initial owner. |
| GET / PATCH / DELETE | `/communities/{cid}` | Read / rename / delete. Delete also allows a platform admin to remove any community (orphan cleanup, #489); read/rename stay membership-scoped. |
| GET / POST | `/communities/{cid}/members` | List (with `username`, `role_names`) / add an existing user by exactly one of `user_id` or exact `username` (#355). |
| GET | `/communities/{cid}/me/permissions` | Caller's own effective set: community-wide codes + per-resource grants (#354). Membership-gated only (Layer-1). |
| DELETE | `/communities/{cid}/members/{uid}` | Remove member (revokes roles & grants). |
| POST / DELETE | `/communities/{cid}/members/{uid}/roles[/{rid}]` | Assign / unassign a role. |
| GET / POST | `/communities/{cid}/roles` | List / create custom role (name + permission codes). |
| GET / PATCH / DELETE | `/communities/{cid}/roles/{rid}` | Read / update / delete. Preset `Owner` role is `is_preset`. |
| GET / POST | `/communities/{cid}/grants` | List (`?user_id=` filter) / create per-resource grant. `resource_type` = `server` only; permission families `server:*`, `file:*`, `backup:*`, `plugin:*`, `schedule:*`. |
| DELETE | `/communities/{cid}/grants/{gid}` | Revoke. |

Permission catalog (community axis, 35 codes — the role/grant editor's source
of truth): `server:{create,read,update,delete,start,stop,restart,command}`,
`file:{read,edit,history,rollback}`, `backup:{create,read,restore,delete,schedule}`,
`member:{read,add,remove}`, `role:{read,manage}`, `grant:{read,manage}`,
`group:{read,manage}`, `community:{read,update,delete}`, `audit:read`,
`session:read` (relay game-session moderation surface — player IPs are PII,
RELAY.md Section 8; seeded on the Owner role by migration 0017),
`plugin:{read,manage}` (plugin/mod content management, issue #1150;
seeded on the Owner role by migration 0019),
`schedule:{read,manage}` (general scheduler CRUD, issue #1837;
seeded on the Owner role by migration 0030).
Platform axis (flag-driven, not assignable to roles): `worker:manage`,
`community:provision`, `platform:monitor`.

### 2.3 Servers (lifecycle, console, files, backups, groups)

| Method | Path | Notes |
|---|---|---|
| GET / POST | `/communities/{cid}/servers` | List / create (`name`, `mc_edition`, `mc_version`, `server_type`, `config`, `accept_eula`, optional `game_port`). |
| POST | `/communities/{cid}/servers/import` | ZIP import (multipart). |
| GET | `…/{sid}/export` | ZIP export (download). |
| GET / PATCH / DELETE | `…/{sid}` | Read / update (name, config, game_port) / delete. Every PATCH edit needs `server:update`. The retired `backup_interval_hours` key is a `422` (`retired_config_key`, #1840) — backup cadence is a `backup` schedule now. |
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
| POST | `…/{sid}/backups/{bid}/restore[?force=true]` | **Server must be stopped.** `?force=true` overrides the quarantine gate (#703). |
| DELETE | `…/{sid}/backups/{bid}` | Delete. |
| PUT / DELETE | `…/{sid}/backups/retention` | Set / clear the scheduled-backup retention policy (issue #1841): `{keep_last}` (≥ 1) XOR `{daily, weekly, monthly}` (each ≥ 0, one > 0); an invalid shape is 422 `invalid_retention_policy`. Gated by `backup:schedule`. Applies only to `source=scheduled` backups — manual/uploaded rows are never auto-deleted. Setting prunes immediately; thereafter each successful scheduled backup run prunes (each deletion audited as `backup:delete`, no actor). Policy readable as `backup_retention` on the server read; null while unconfigured. |
| GET | `…/{sid}/groups` | Groups attached to this server. |
| GET / POST | `/communities/{cid}/groups` | Player groups (`kind`: `op` \| `whitelist`). |
| GET / PATCH / DELETE | `…/groups/{gid}` | Read / rename / delete. |
| POST / DELETE | `…/groups/{gid}/players[/{uuid}]` | Add / remove player (uuid + username). |
| GET / PUT / DELETE | `…/groups/{gid}/servers[/{sid}]` | List / attach / detach server. |
| GET / POST | `…/{sid}/schedules` | List / create a per-server schedule (`name`, `action` ∈ command\|start\|stop\|restart\|backup, `cron` XOR `interval_seconds`, `timezone`, `enabled`, `command` for `command`, `warning_steps` for stop/restart). Reads need `schedule:read`; writes need `schedule:manage` **and** the action's own permission (`command`→`server:command`, `start/stop/restart`→`server:{start,stop,restart}`, `backup`→`backup:schedule`) — anti-escalation. Authorization is write-time only: the runner (#1838) later executes as the system, so revoking a permission does not stop existing schedules. `next_run_at` is null while disabled, recomputed on enable. |
| GET / PATCH / DELETE | `…/{sid}/schedules/{scid}` | Read / edit (partial; action immutable) / delete a schedule. |
| GET | `…/{sid}/schedules/{scid}/runs` | Execution history newest-first (`schedule:read`). |

Server response fields: `id`, `community_id`, `name`, `mc_edition`,
`mc_version`, `server_type`, `config` (full blob),
`memory_limit_mb` (derived from `config['memory_limit_mb']`, null when unset),
`cpu_millis` (derived from `config['cpu_millis']`, null when unset),
`game_port`, `slug` (relay hostname prefix, auto-generated at create,
renameable via PATCH), `join_hostname` (`<slug>.<base_domain>` when relay
enabled, else null), `bedrock_address` / `bedrock_port` (Bedrock join address,
epic #1540: non-null only while the deployment's Bedrock gate is on AND the
server carries at least one *enabled* Geyser plugin copy — see `BEDROCK.md`;
Paper only today), `desired_state`, `observed_state`, `observed_at`,
`assigned_worker_id`, `backup_retention` (the scheduled-backup retention
policy, issue #1841; null while unconfigured).

Server state model: `desired_state` ∈ {running, stopped};
`observed_state` ∈ {starting, running, stopping, stopped, restarting, crashed,
unknown} + `observed_at` + `assigned_worker_id`.
Server types: vanilla / paper / fabric / forge.
Execution backend: `container` (the only shipped backend).

### 2.4 Versions, ports, fleet, audit, platform

| Method | Path | Notes |
|---|---|---|
| GET | `/versions` | Catalogued server types. |
| GET | `/versions/{type}` | Version list for a type. |
| POST | `/versions/refresh` `[A]` | Invalidate catalog cache (`?server_type=` optional). |
| GET | `/versions/jar-pool/stats` `[A]` · POST `/versions/jar-pool/gc` `[A]` | JAR pool size / garbage collection. |
| GET | `/ports/available?count=` · `/ports/check/{port}` | Free-port discovery / conflict check. |
| GET | `/workers` `[A]` | Fleet list: status, capabilities (drivers, max_servers, cpu/mem), assigned_count, heartbeat. |
| PUT / DELETE | `/workers/{wid}/drain` `[A]` | Set / clear drain. Set MARKS the worker's running servers `desired=stopped` and returns `servers_stopped` (the count marked, not yet stopped); the reconciler then stops each + takes the final snapshot (#845/#849) ASYNCHRONOUSLY (after the grace window, ~120s default, + a tick) and only while the worker stays connected — keep the worker up until convergence, or stops+snapshots defer to a reconnect that never happens in a decommission. Confirm convergence PER SERVER (each reaching `observed=stopped` and unassigned), NOT by assigned load: drain decrements the load synchronously, so it drops to 0 before any stop runs. Clear only re-enables placement (does not restart them; un-draining before convergence transiently oversubscribes the worker). |
| GET | `/audit` `[A]` | Global audit (`community`, `operation`, `actor`, `since`, `until`, `limit`, `offset`). |
| GET | `/communities/{cid}/audit` | Community-scoped audit (same filters minus `community`). |
| GET | `/backups/statistics` `[A]` | Global backup statistics. |
| GET | `/healthz` · `/readyz` · `/metrics` | Liveness / readiness / Prometheus (ops-facing, not UI-core). |
| GET | `/meta` | Deployment facts the Web UI reads before a server exists (issue #1002): `{relay_enabled, bedrock_enabled, default_memory_limit_mb, max_memory_limit_mb}`. Requires authentication. Used by the create wizard to decide whether to surface the game-port control (relay mode auto-allocates), and by the plugins tab to decide whether to show the Bedrock/Geyser discovery hint (epic #1540, `bedrock_enabled` = `relay_enabled` AND the deployment's Bedrock capability flag). |

### 2.5 Resource packs (issues #1176, #1177)

Global resource pack library (not community-scoped) and per-server assignment.

| Method | Path | Notes |
|---|---|---|
| POST | `/resource-packs` | Upload a resource pack (multipart; requires `server:update` in at least one community). |
| GET | `/resource-packs` | List all resource packs (authenticated). |
| DELETE | `/resource-packs/{id}` | Delete a resource pack (uploader or platform admin; 409 when still assigned to a server). |
| GET | `/resource-packs/{id}/download` | Download (authenticated). |
| GET | `/public/resource-packs/{id}/{filename}` | Public download (no auth) — the URL Minecraft clients fetch. Validates `filename` matches. |
| POST | `…/{sid}/resource-pack` | Assign a resource pack to a server (`server:update`). Body: `{resource_pack_id, require_resource_pack, resource_pack_prompt}`. |
| DELETE | `…/{sid}/resource-pack` | Unassign (`server:update`). |
| GET | `…/{sid}/resource-pack` | Get the current assignment (`server:read`). |

### 2.6 Real-time (WebSocket)

| Path | Notes |
|---|---|
| `WS /communities/{cid}/servers/{sid}/events?streams=status,log,metrics,notification` | Typed frames `{stream, ts, payload}`. `status`: `{state, detail}` · `log`: `{line, stream}` · `metrics`: `{cpu_millis, memory_bytes, player_count}` · `notification`: `{kind, title, detail}` (operator notice) · `gap`: client fell behind (always delivered). |
| `WS /communities/{cid}/events` | Community-wide **status + notification** firehose; frames carry `server_id`. |

Auth: browsers pass the access token via `Sec-WebSocket-Protocol` as two
subprotocols `["access_token", "<jwt>"]`; the server echoes `access_token` as
the accepted subprotocol (RFC 6455). The `Authorization: Bearer` header is also
honored for non-browser clients. Close codes mirror REST: 4400 bad `streams`,
4401 unauthenticated, 4403 forbidden, 4404 not found / not a member.
Authorization is re-checked every 60 s mid-stream. Delivery is best-effort;
REST keeps working if the socket dies (FR-MON-4).

Note: the data-plane endpoints (`/api/data-plane/...`) are Worker-credential-only
transfer endpoints — not part of the UI surface.

## 3. Personas and capability scoping

| Persona | Sees | Typical UI surface |
|---|---|---|
| Community member | Only their communities; actions filtered by role permissions ∪ grants | Servers list, server detail (capabilities vary per permission) |
| Community owner | Everything in their community | + Members, Roles, Grants, Groups, Audit, Community settings |
| Platform administrator | All communities + platform area | + Admin area: Users, Communities provisioning, Workers, Version catalog/JAR pool, Global audit & backup stats |
| Unauthenticated | Login / Register only | — |

The UI derives capabilities from `GET /users/me` (admin flag) +
`GET /communities/{cid}/me/permissions` (the caller's effective set, #354),
**and still treats any 403/404 as the authority** (FR-AUTHZ-6: server-side
enforcement is the truth; client scoping is convenience). The effective set is
fetched on community switch and cached for the session (see 7.3).

## 4. UI feature list

Grouped; each maps 1:1 to the endpoints in Section 2.

**Auth & account** — login, register, logout, token refresh (transparent),
profile edit, password change, account deletion.

**Community workspace** — community switcher; dashboard with live server
tiles (community WS); community rename/delete.

**Server operations** — create (wizard: type/version → config/EULA),
import ZIP, export ZIP, start/stop/force-stop/restart, delete,
live status & uptime via WS, RCON console with command history, live log
viewer (follow/pause/filter), metrics strip (CPU/mem/players).

**File management** — directory browser, text-file editor (base64 transport),
upload (w/ ZIP extract), download, rename, delete, mkdir, search,
per-file version history + rollback.

**Backups** — on-demand create, list w/ statistics, download, upload,
restore (stopped-only, guarded confirm), delete.

**Schedules** — per-server scheduled actions (command/start/stop/restart/
backup) on a cron or interval cadence with timezone; enable/disable, run
history, pre-action player warnings for stop/restart, failure toasts via the
NOTIFICATION stream.

**Player groups** — op/whitelist groups, player add/remove, attach/detach
to servers, per-server attached-group view.

**Membership & access** — members list/add/remove, role assign/unassign,
role editor over the 35-code catalog, per-server grants editor.

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
   #schedules  scheduled actions + run history
   #plugins    installed plugins/mods + Modrinth catalog (hidden for vanilla)
   #players    attached op/whitelist groups
   #settings   name / config / port / export / danger zone
/communities/:cid/settings            Community settings
   #members    members + role assignment
   #roles      role editor
   #grants     per-server grants
   #groups     player groups (community-wide)
   #audit      community audit log
   #general    rename / delete
/account                              Profile / password / delete

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
   from `GET /versions/{type}` (latest preselected).
2. **Config & EULA** — name, game port (direct-mode only: auto-suggest from
   `GET /ports/available`, validate via `GET /ports/check/{port}` on blur),
   optional `server.properties` overrides (key editor), EULA checkbox
   (required to start later; create allows deferred acceptance — surfaced as
   a warning).
3. Create → navigate to server detail. Alternative path: "Import ZIP" tab on
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
  notice (Section 6.9 semantics). Creating a new file works while running too
  (create-through to the live working set).
- File-API failure reasons (see `CONTROL_PLANE.md` Section 7.2 for the
  authoritative catalog). The client switches on the `reason` field; the set is
  additive — handle unknown reasons gracefully.

  | HTTP | `reason` | Meaning |
  |---|---|---|
  | 422 | `invalid_path` | Path is malformed (absolute, `..`, traversal-unsafe) or an older Worker sent an unrefined denial. |
  | 422 | `is_a_directory` | Read or edit targeted a directory. |
  | 422 | `not_a_directory` | List targeted a regular file. |
  | 422 | `symlink_refused` | Path contains or resolves to a symlink (escape-vector defence). |
  | 413 | `file_too_large` | Read result or edit payload exceeds the file size cap. |

### 6.7 Server detail — Backups
- Stats header (count, total size, newest/oldest).
- Table: created_at, source (`manual` / `scheduled` / `event` / `uploaded`), size, health (`healthy` / `quarantined` / `unknown`), creator;
  actions: download, restore, delete.
- Restore: blocked with explanation while running (offer "stop now then
  restore" two-step); typed-confirm dialog. A `quarantined` backup requires a
  second force-acknowledge confirmation before the `?force=true` restore is
  issued.
- Create backup button (works on running servers — on-demand snapshot path);
  upload backup (file picker).
- Schedule: the inline "every N hours" cadence field is **retired** (issue
  #1840). Scheduled backups are now a first-class `backup` schedule on the
  general scheduler (the `…/{sid}/schedules` surface); the Backups tab only
  shows a short note pointing `backup:schedule` holders there. No PATCH of a
  `backup_interval_hours` key remains (the API 422s it as `retired_config_key`).
- Retention (issue #1843; `backup:schedule` only — hidden otherwise): a policy
  editor beside the schedule note, prefilled from `backup_retention` on the
  server read. Mode select = keep-all (no policy) / keep-last-N / tiered
  (daily / weekly / monthly buckets); Save `PUT`s `…/{sid}/backups/retention`
  (`{keep_last}` XOR `{daily, weekly, monthly}`), or `DELETE`s it when the
  mode is keep-all and a policy exists. Client-side validation mirrors the API
  rule (`keep_last` ≥ 1; tiers each ≥ 0, at least one > 0) inline; a server
  422 `invalid_retention_policy` maps to the same inline error. A hint states
  the policy prunes **only** scheduled backups (the table's source badge
  distinguishes the rows); manual / uploaded / event backups are never
  auto-deleted.

### 6.8 Server detail — Players
- Attached groups (`GET …/{sid}/groups`) with kind badges; attach/detach
  pickers from community groups; inline link to the community Groups tab.

### 6.9 Server detail — Settings
- Rename, game-port edit (with availability check), `config` key/value
  editor; execution backend displayed read-only (immutable post-create).
- Danger zone: delete server (typed confirm), export ZIP.

### 6.10 Community settings
- **Members**: table (username, roles as chips); add-member dialog by exact
  username (`POST …/members {username}`, #355 — no-match is a 422
  `user_not_found` rejection, same as an unknown `user_id`, already-member is
  409 `already_member`); role chips editable inline; remove with confirm
  (explains grant/role revocation).
- **Roles**: list (preset Owner locked); editor = name + permission-matrix
  grouped by family (server/file/backup/schedule/member/role/grant/group/
  community/audit/plugin) with select-all per family.
- **Grants**: per-user list (user filter); create = pick member → pick server
  → pick permissions (restricted to the per-server resource families:
  server/file/backup/plugin/schedule).
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
- **Communities**: paginated table of all communities (name, id, member/server
  counts) from `GET /admin/communities`; provision dialog (name + initial owner
  user); delete a community (typed-confirm) via `DELETE /communities/{cid}`
  (#489).
- **Workers**: table (id, version, status incl. draining, drivers,
  assigned/max, cpu/mem, heartbeat age); drain/undrain toggle with confirm.
- **Versions**: per-type catalog freshness, refresh button (all or one type);
  JAR pool stats + GC trigger showing reclaimed bytes.
- **Audit**: global log with community filter added.

### 6.13 Server detail — Schedules

The `#schedules` tab (issue #1842; numbered out of tab order to avoid
renumbering 6.8–6.12): the UI for the general scheduler (#1837).

- Table: name, action, human-readable cadence ("Every N min/h" or the cron
  expression), timezone, enabled toggle, last-run and next-run timestamps
  (`next_run_at` is null — shown as "—" — while disabled).
- Create/edit dialog: name; action select (create only — the action is
  immutable; the edit dialog disables it and points at delete + recreate);
  cadence = interval (minutes/hours) XOR cron expression; IANA timezone select
  (`Intl.supportedValuesOf("timeZone")`, default UTC); command-line field only
  for the `command` action; warning-steps editor (≤5 `{offset_minutes 1–120,
  message}` rows) only for `stop`/`restart`. API validation failures map to
  inline field errors via their typed 422 reasons (`invalid_cron`,
  `invalid_cadence`, `invalid_timezone`, `invalid_schedule_name`,
  `invalid_payload`) and the 409 duplicate name (`schedule_name_exists`).
- Delete: confirm dialog (run history cascades away with the schedule).
- Run history: per-schedule dialog, newest first — outcome badge
  (`success`/`failure`/`skipped`), sanitized detail, started/finished
  timestamps. Capped at 50 rows per schedule server-side.
- Permission gating (7.3): the tab body needs `schedule:read`; writes are
  two-layer — `schedule:manage` **and** the action's own permission gates the
  create button's action options and each row's edit/toggle/delete
  (anti-escalation, write-time only). A `schedule:read`-only member gets a
  read-only view (run history stays available).
- Failure surfacing: `schedule_failed` NOTIFICATION frames from the community
  events socket (7.2) render as error toasts (title + sanitized detail) while
  the dashboard is open.

### 6.14 Server detail — Plugins

The `#plugins` tab (issue #1153; numbered out of tab order to avoid renumbering
6.8–6.13): plugin/mod content management for a server. The tab label and every
content noun are loader-aware — **Plugins** for Paper, **Mods** for
Fabric/Forge (#1320) — and the whole tab is **hidden for `vanilla`** (no
backend support; the tab body also self-guards with an "unsupported" notice).

- Installed list (`GET …/plugins`): a table of name (with an "update available"
  badge when a newer catalog version exists, from `GET …/plugins/updates`),
  version, source badge (`modrinth` / `local`), status pill
  (enabled / disabled), size, and — for mod loaders only — a **Side** column
  (`both` / `server` / `client`, #1308), editable with `plugin:manage`
  (`POST …/plugins/{id}/side`). An empty list shows a "none installed" row.
- Per-row actions (`plugin:manage`): enable / disable
  (`POST …/plugins/{id}/enable` · `/disable`), update to the offered catalog
  version (`POST …/plugins/{id}/update`), remove (`DELETE …/plugins/{id}`, plain
  confirm dialog), and — for Modrinth-sourced rows — a **Dependencies** expander
  (`GET …/plugins/{id}/dependencies`) listing each dependency as
  required/optional and installed/missing.
- Install (`plugin:manage`): local `.jar` upload with a progress bar
  (`POST …/plugins`, multipart); **Browse** opens the Modrinth catalog modal — a
  debounced search (`GET …/catalog/search`) whose hits open a project detail
  view with a per-version install picker (`POST …/catalog/install`) that marks
  the already-installed version.
- Dependency health: a validation checklist under the table
  (`GET …/plugins/validate`, #1307) flags missing dependencies, unsatisfied
  version ranges, conflicts, and MC-version mismatches, or shows an all-clear
  line; **Resolve** (`POST …/plugins/resolve` → `…/resolve/apply`, #1309) plans
  and then auto-imports the missing Modrinth dependencies.
- Client modpack (mod loaders only): when at least one enabled mod is
  client-relevant (side `client` / `both`), a **Download client modpack** button
  bundles them (`GET …/client-mods/download`, #1342).
- Bedrock hint: on a Paper server, when the deployment's Bedrock gate is on
  (`/meta`'s `bedrock_enabled`, Section 2.4) and a Geyser plugin is installed, an
  inline note links to Floodgate setup (epic #1540).
- Server-state gating: reads render anytime, but **every mutation requires the
  server at rest** (observed and desired both stopped). While not at rest a
  notice shows and all install / enable / disable / update / remove / side /
  resolve controls are disabled; a server-busy API reason (e.g.
  `server_not_stopped`) surfaces as an error toast.
- Permission gating (7.3): the tab body needs `plugin:read` (a member without it
  sees a short notice); every mutation needs `plugin:manage`, which also gates
  whether the toolbar and the per-row action buttons render at all.

## 7. Cross-cutting concerns

### 7.1 Auth/session lifecycle
- The API-side contract these notes consume — endpoint status codes, the
  body-vs-cookie transport rules, and the refresh reuse grace window the
  single-flight mutex below guards against — is documented in
  [`AUTH_API.md`](../app/AUTH_API.md).
- Access token (short-lived; ~900 s in the live deployment) kept in memory
  only. Refresh token in an **httpOnly cookie** set by the API on login
  (`Secure; SameSite=Strict; Path=/api/auth`) — never readable by JS; requires the
  API-side cookie transport (issue #363). Transparent refresh on 401 +
  single-flight refresh mutex; hard logout on refresh failure. Page reload
  (bootstrap) re-establishes the session via the **non-rotating**
  `POST /api/auth/session` probe — it exchanges the cookie for an access token
  without rotating the refresh token, so a reload / F5 storm can never race an
  in-flight rotation and leave a revoked predecessor cookie in the jar (the
  torn-rotation logout, issue #512). Rotation stays on the transparent
  `POST /api/auth/refresh` (the in-session 401-retry path), where reuse-detection
  still applies; a mid-session refresh failure still hard-logs-out (a genuinely
  expired / revoked session).
- WS connections carry the access token in the `Sec-WebSocket-Protocol`
  subprotocol header (`["access_token", "<jwt>"]`, issue #1596); on token
  rotation, sockets are reconnected (reconnect-on-rotate chosen).

### 7.2 Real-time strategy
- One WS per open server-detail page + one community WS for the dashboard.
- Reconnect with exponential backoff + jitter; resubscribe on open; banner
  shows degraded mode; REST polling fallback for status only.

### 7.3 Permission-driven rendering
- Capabilities come from `GET /communities/{cid}/me/permissions` (#354):
  fetched on community switch, cached for the session, re-fetched on a 403
  (the set may have changed since cache). Controls render from
  `permissions ∪ (matching resource grant)`.
- Every denied action is still handled at response time (403 toast "you lack
  server:start", named from the `permission` extension member — see 7.4; 404
  treated as nonexistence per the no-existence-signal posture). UI never invents
  authority; failures degrade politely.

### 7.4 Errors & confirmations
- Every API error is RFC 9457 `application/problem+json`: one body shape with
  `type`, `title`, `status`, and a `reason` extension member. The machine code
  is both the terminal segment of the `type` URI (`urn:mcsd:error:<reason>`) and
  the `reason` field — the client switches on `reason`. Request-validation
  failures (422) use `reason: "validation_error"` and carry the per-field list
  in an `errors` extension member. A 403 permission denial keeps
  `reason: "forbidden"` and carries the required permission code in a
  `permission` extension member, which the client names in the denial toast
  (7.3). The client branches on exactly this shape; there is no legacy
  bare-string / `{reason}` fork. The auth endpoints' reason codes and the
  `permission` member are enumerated in [`AUTH_API.md`](../app/AUTH_API.md)
  Section 2.
- API error surfaced via toast + inline field errors (422 `errors` list).
- Conflict-flavored errors (e.g. lifecycle races, `server_unsettled`-style
  responses) get a "state changed — refresh" treatment, not a raw error dump.
- Destructive operations (delete server/community/user/backup-restore) use
  typed-confirm dialogs.

### 7.5 i18n & theming
- All strings through a `t('key')` dictionary; English shipped, Japanese
  addable. Dark theme via CSS custom properties (a light theme later is a
  token swap, not a rewrite).

### 7.6 Tech stack (decided — for the real implementation, not the mockup)
- SPA: **React + TypeScript + Vite**, TanStack Query (REST cache +
  invalidation), plain WebSocket wrappers, CSS modules or vanilla-extract —
  no heavy UI kit; the design system stays ours. Generated API client from
  the OpenAPI schema.
- Lives in `webui/` at the repo root, a self-contained npm package mirroring
  how `api/` and `worker/` are self-contained.

### 7.7 Serving & origin
- **Same-origin by design.** The API ships **no CORS middleware**, on purpose.
  The refresh cookie is `Secure; SameSite=Strict; Path=/api/auth` (see 7.1 and
  [`AUTH_API.md`](../app/AUTH_API.md) Section 5 for the cookie attributes), so a
  cross-origin SPA cannot authenticate — the browser would not attach the cookie
  to the refresh request. Every deployment posture below keeps the UI and the API
  on the same origin; do not add CORS to work around a split origin.
- **`/api` namespace (issue #498).** The entire HTTP API (REST, WebSocket, and
  the OpenAPI schema/docs) lives under `/api`, so it can never share a path with
  an SPA client-side route. This makes the production fallback an absolute rule:
  `/api/*` is the API, *everything else* is the SPA. It replaced the earlier
  posture where three deep-links (`/communities/{cid}`,
  `/communities/{cid}/servers/{sid}`, `/communities/{cid}/servers/new`) collided
  with API GET routes and returned JSON on a hard reload.
- **Development.** The Vite dev server proxies the single `/api` prefix (REST
  *and* the WebSocket event streams) to a local API instance, so the browser sees
  a single origin (the dev server). Because `/api` is never an SPA route, the
  proxy needs no Accept-header bypass for deep-links. No CORS is added anywhere
  (#378 Phase 1).
- **Production.** The API container serves the built SPA (`webui/dist`) via
  FastAPI `StaticFiles` with an SPA fallback, on the same origin as the API. No
  reverse proxy and no new Compose service. The `/api/*` routes (including the WS
  paths and the health/readiness/metrics probes) and `/assets/*` (the built SPA
  chunks) are both excluded from the SPA fallback: a `/api/*` path is the API,
  and an unmatched `/assets/*` request returns 404 (a stale/renamed chunk, never
  a client-side route). Every other unmatched path falls back to the SPA's
  `index.html` so client-side routing works on deep links and reloads (#378
  Phase 8, #498, #634).

## 8. Out of scope for the first UI cut

- Metrics history/persistence (only live sparklines from the WS stream).
- `/metrics` (Prometheus) visualization — operators use Grafana.
- Mobile-optimized layouts (responsive down to tablet only).
- Light theme (structure ready, not shipped).
- Active-session listing / revocation on the account page — the session API now
  exists (`GET`/`DELETE /api/users/me/sessions`, #387 via PR #603), but the
  account-page UI is still deferred (see #606 for the revoke-all-others
  hardening to land before the UI is added).

## 9. Resolved open questions

All of the first draft's open questions are now decided:

| # | Question | Decision | Refs |
|---|---|---|---|
| Q1 | Stack | **React + TypeScript + Vite** (TanStack Query, generated OpenAPI client). | 7.6 |
| Q2 | Refresh-token storage | **httpOnly cookie from the start** (no localStorage interim). Needs API-side cookie transport — issue #363. | 7.1 |
| Q3 | "My permissions" endpoint | **Implemented**: filed as #354, landed as `GET /communities/{cid}/me/permissions` (#357). | 3, 7.3 |
| Q4 | Member-add lookup | **Implemented**: filed as #355, landed as `POST …/members` accepting exactly one of `user_id` / exact `username` (#359). | 6.10 |
| Q5 | Where the UI lives | **`webui/` in this monorepo**, alongside `api/` / `worker/` / `proto/` (REQUIREMENTS.md Section 1.2 updated). Mockup stays under `docs/ui/mockup/` as a design reference. | 1, header |
