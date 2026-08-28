# Web UI — Feature Inventory, Screen Map, and Spec

> Status: **Design** · Audience: contributors to `webui/`, `api/`
>
> This document inventories the v2 API surface, derives the full UI feature
> list from it, and specifies the screen structure and per-screen specs for the
> Web UI. A static mockup (no real API calls) accompanies it under
> `docs/ui/mockup/` and is kept as a design reference.
>
> The Web UI is built **in this monorepo** under `webui/`, alongside `api/`,
> `worker/`, and `proto/` (REQUIREMENTS.md Section 1.2). Section 9 records the
> decisions this specification rests on.

## Table of Contents

1. [Decisions](#1-decisions)
2. [API surface inventory](#2-api-surface-inventory)
3. [Personas and capability scoping](#3-personas-and-capability-scoping)
4. [UI feature list](#4-ui-feature-list)
5. [Screen map](#5-screen-map)
6. [Screen specs](#6-screen-specs)
7. [Cross-cutting concerns](#7-cross-cutting-concerns)
8. [Out of scope](#8-out-of-scope)
9. [Design decisions](#9-design-decisions)

---

## 1. Decisions

| Topic | Decision |
|---|---|
| Visual tone | Dark operations-console style (Grafana/Portainer family). |
| UI language | English, with all strings behind an i18n dictionary so Japanese can be added later. |
| Mockup form | Multiple static HTML pages + shared CSS/JS, mock data embedded in JS. No real API calls. |
| Placement | `webui/` in this monorepo, alongside `api/` / `worker/` / `proto/`. The mockup stays under `docs/ui/mockup/` as a design reference. |
| Stack | React + TypeScript + Vite (Section 7.6). |
| Session storage | Refresh token in an httpOnly cookie (Section 7.1), carried by the API's cookie transport (AUTH_API.md Section 3). |

## 2. API surface inventory

Complete endpoint list (generated from the FastAPI OpenAPI schema).
`[A]` = platform-admin axis; everything else is community-permission-gated.

> **`/api` prefix.** The entire HTTP API — every path in the tables
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
| POST | `/auth/login` | username + password → `{access_token, token_type}` (bearer); refresh token via cookie only. |
| POST | `/auth/session` | refresh cookie → `{access_token}` only; non-rotating bootstrap (7.1). |
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
| GET | `/communities` | Communities the caller belongs to (membership-scoped; the admin axis does not pierce isolation). |
| GET | `/admin/communities` `[A]` | All communities with `member_count`/`server_count` (`limit`/`offset`, returns `total`); the platform-axis listing. |
| POST | `/communities` `[A]` | Provision a community + initial owner. |
| GET / PATCH / DELETE | `/communities/{cid}` | Read / rename / delete. Delete also allows a platform admin to remove any community (orphan cleanup); read/rename stay membership-scoped. |
| GET / POST | `/communities/{cid}/members` | List (with `username`, `role_names`) / add an existing user by exactly one of `user_id` or exact `username`. |
| GET | `/communities/{cid}/me/permissions` | Caller's own effective set: community-wide codes + per-resource grants. Membership-gated only (Layer-1). |
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
RELAY.md Section 8; seeded on the Owner role),
`plugin:{read,manage}` (plugin/mod content management; seeded on the Owner
role), `schedule:{read,manage}` (general scheduler CRUD; seeded on the Owner
role).
Platform axis (flag-driven, not assignable to roles): `worker:manage`,
`community:provision`, `platform:monitor`.

### 2.3 Servers (lifecycle, console, files, backups, groups)

| Method | Path | Notes |
|---|---|---|
| GET / POST | `/communities/{cid}/servers` | List / create (`name`, `mc_edition`, `mc_version`, `server_type`, `config`, `accept_eula`, optional `game_port`). |
| POST | `/communities/{cid}/servers/import` | ZIP import (multipart). |
| GET / HEAD | `…/{sid}/export` | ZIP export (download). Accepts the Bearer access token, or a `?grant=` download grant so the browser can stream a multi-GB export straight to disk, or the `HttpOnly` download cookie a redemption sets so an interrupted transfer can be retried. The response declares `Cache-Control: no-store` under every credential. `HEAD` is the metadata probe: the same gate and the same headers with no body — including no `Content-Length`, since the zip is built incrementally and the `GET` declares none either — and it neither builds the zip nor records a `server:export` audit event. |
| POST | `…/{sid}/export/download-grant` | Mint that grant: `{download_url, expires_at}`, `Cache-Control: no-store`. Same `file:read` gate as the export, and the same pre-flight — a running server is 409 `server_unsettled` and no grant is issued; `auth.token.download_grant_ttl_seconds`, 30 s by default (AUTH_API.md Section 3). |
| GET / PATCH / DELETE | `…/{sid}` | Read / update (name, config, game_port) / delete. Every PATCH edit needs `server:update`. `backup_interval_hours` is not a config key; a PATCH carrying it is `422` (`retired_config_key`; DATABASE.md Section 7) — backup cadence is a `backup` schedule. A `game_port` change against a server whose `server.properties` is missing is a `409` (`server_properties_missing`): the rewrite preserves the file's other keys, so it refuses rather than republish one without `rcon.password`. |
| POST | `…/{sid}/start` · `/stop?force=` · `/restart` | Lifecycle. Stop supports force. |
| POST | `…/{sid}/command` | RCON line → `{output}`. |
| GET | `…/{sid}/files?path=&list=` | Read file (base64) or list directory (entries + `truncated`). |
| PUT / DELETE | `…/{sid}/files?path=` | Write (base64, versioned) / delete. The root `server.properties` is guarded on **every** files-API write path — `PUT`, `DELETE`, either end of a rename, an upload (direct or archive member), and a rollback: an operation that would change or drop a platform-managed key — `server-port`, the RCON triple, the resource-pack keys — is a `422` (`platform_managed_key`) naming it in the `key` member. The guard is over the keys, not the filename: editing the file's other keys is unaffected, and a root `server.properties` that carries none of those keys still deletes and still renames away. |
| POST | `…/{sid}/files/directories?path=` | mkdir. The root `server.properties` path — and anything under it, whose missing parents are created — is a `422` (`platform_managed_path`): the platform's own writes publish a file there, so no directory may stand in its place. |
| GET / HEAD | `…/{sid}/files/download?path=` | Raw download (file bytes, or a streamed ZIP for a directory). Accepts the Bearer access token, or a `?grant=` download grant so the browser can stream a multi-GB directory straight to disk, or the `HttpOnly` download cookie a redemption sets so an interrupted transfer can be retried. The response declares `Cache-Control: no-store` under every credential. `HEAD` is the metadata probe: the same gate and the same headers with no body — a file's `Content-Length` when it is known, none for the incrementally built directory ZIP — and it neither opens the download stream (no directory ZIP is built, no file bytes are streamed) nor records a `file:download` audit event. The file/directory dispatch itself is the `GET`'s, unchanged: resolving which branch a path takes reads the parent listing and, for a file, pulls one chunk to confirm it is readable. |
| POST | `…/{sid}/files/download-grant?path=` | Mint that grant: `{download_url, expires_at}`, `Cache-Control: no-store`. Same `file:read` gate as the download, and the same pre-flight — missing path 404, traversal 422 `invalid_path`, running server 409 `server_unsettled`. `path` is a **query** parameter so mint and redemption bind the identical string; `auth.token.download_grant_ttl_seconds`, 30 s by default (AUTH_API.md Section 3). |
| POST | `…/{sid}/files/upload?path=&extract=` | Multipart upload, optional ZIP extract. An upload landing on the root `server.properties` — directly or as an archive member — that changes a platform-managed key is a `422` (`platform_managed_key`); an offending archive member is caught before any write, so the whole extract is refused with nothing written. |
| POST | `…/{sid}/files/rename` | `{from, to}`. Both ends are guarded against the root `server.properties`: renaming it away is refused when it holds a platform-managed key, and renaming another file onto that name is refused when that file carries one — either is a `422` (`platform_managed_key`) naming the key. A rename **onto** that name whose source exceeds the edit cap is a `413`, since the guard compares the source bytes rather than scanning an unbounded body. Renaming a **directory** onto that name (or under it) is a `422` (`platform_managed_path`) — a directory there breaks the platform's writes; moving one already standing there away still works. |
| POST | `…/{sid}/files/search` | `{query, by, max_results}` → matching paths. |
| GET | `…/{sid}/files/history?path=` | Retained version ids. |
| POST | `…/{sid}/files/rollback?path=` | `{version_id}`. Rolling the root `server.properties` back to a version whose platform-managed keys differ from the file's current ones — a stale `server-port` or `rcon.password` — is a `422` (`platform_managed_key`); a version differing only in the user's own keys rolls back normally. |
| GET / POST | `…/{sid}/backups` | List / create on-demand backup. |
| GET | `…/{sid}/backups/statistics` | count / total bytes / newest / oldest. |
| POST | `…/{sid}/backups/upload` | Upload an off-host backup archive. |
| GET / HEAD | `…/{sid}/backups/{bid}/download` | Download archive. Accepts the Bearer access token, or a `?grant=` download grant so the browser can stream a multi-GB archive straight to disk, or the `HttpOnly` download cookie a redemption sets so an interrupted transfer can be retried. **Resumable**: the response declares `Accept-Ranges: bytes` and a strong `ETag`, and a single `Range` request is served `206` with `Content-Range` over a ranged read (`416` + `Content-Range: bytes */<size>` when unsatisfiable; a malformed or multi-range `Range` is ignored and the whole archive served). `If-Range` is honoured, so a resumed request that names a stale representation gets the current archive whole. The browser's own retry of an interrupted transfer authenticates with the `HttpOnly` download cookie a grant redemption sets, since the `?grant=` in the retried URL has expired by then (AUTH_API.md Section 3). The response declares `Cache-Control: no-store` on both the `200` and the `206`, under every credential. `HEAD` is the metadata probe a resumable client sends first: the same gate and the same headers with no body, so `Content-Length`, `Accept-Ranges` and the `ETag` are learned without starting a transfer. It never opens the archive stream and records no `backup:download` audit event; `Range` is not honoured on it (RFC 9110 Section 14.2 defines range handling for `GET` only), so a probe always reports the whole archive. |
| POST | `…/{sid}/backups/{bid}/download-grant` | Mint that grant: `{download_url, expires_at}`, `Cache-Control: no-store`. Same `backup:read` gate as the download; `auth.token.download_grant_ttl_seconds`, 30 s by default (AUTH_API.md Section 3). |
| POST | `…/{sid}/backups/{bid}/restore[?force=true]` | **Server must be stopped.** `?force=true` overrides the quarantine gate. |
| DELETE | `…/{sid}/backups/{bid}` | Delete. |
| PUT / DELETE | `…/{sid}/backups/retention` | Set / clear the scheduled-backup retention policy: `{keep_last}` (≥ 1) XOR `{daily, weekly, monthly}` (each ≥ 0, one > 0); an invalid shape is 422 `invalid_retention_policy`. Gated by `backup:schedule`. Applies only to `source=scheduled` backups — manual/uploaded rows are never auto-deleted. Setting prunes immediately; thereafter each successful scheduled backup run prunes (each deletion audited as `backup:delete`, no actor). Policy readable as `backup_retention` on the server read; null while unconfigured. |
| GET | `…/{sid}/groups` | Groups attached to this server. |
| GET / POST | `/communities/{cid}/groups` | Player groups (`kind`: `op` \| `whitelist`). |
| GET / PATCH / DELETE | `…/groups/{gid}` | Read / rename / delete. |
| POST / DELETE | `…/groups/{gid}/players[/{uuid}]` | Add / remove player (uuid + username). |
| GET / PUT / DELETE | `…/groups/{gid}/servers[/{sid}]` | List / attach / detach server. |
| GET / POST | `…/{sid}/schedules` | List / create a per-server schedule (`name`, `action` ∈ command\|start\|stop\|restart\|backup, `cron` XOR `interval_seconds`, `timezone`, `enabled`, `command` for `command`, `warning_steps` for stop/restart). Reads need `schedule:read`; writes need `schedule:manage` **and** the action's own permission (`command`→`server:command`, `start/stop/restart`→`server:{start,stop,restart}`, `backup`→`backup:schedule`) — anti-escalation. Authorization is write-time only: the runner executes as the system, so revoking a permission does not stop existing schedules. `next_run_at` is null while disabled, recomputed on enable. |
| GET / PATCH / DELETE | `…/{sid}/schedules/{scid}` | Read / edit (partial; action immutable) / delete a schedule. |
| GET | `…/{sid}/schedules/{scid}/runs` | Execution history newest-first (`schedule:read`). |

Server response fields: `id`, `community_id`, `name`, `mc_edition`,
`mc_version`, `server_type`, `config` (full blob),
`memory_limit_mb` (derived from `config['memory_limit_mb']`, null when unset),
`cpu_millis` (derived from `config['cpu_millis']`, null when unset),
`game_port`, `slug` (relay hostname prefix, auto-generated at create,
renameable via PATCH), `join_hostname` (`<slug>.<base_domain>` when relay
enabled, else null), `bedrock_address` / `bedrock_port` (Bedrock join address:
non-null only while the deployment's Bedrock gate is on AND the server carries
at least one *enabled* Geyser plugin copy — see `BEDROCK.md`; the end-to-end
Bedrock join is verified on Paper only, BEDROCK.md Section 1),
`desired_state`, `observed_state`, `observed_at`,
`assigned_worker_id`, `backup_retention` (the scheduled-backup retention
policy; null while unconfigured).

Server state model: `desired_state` ∈ {running, stopped};
`observed_state` ∈ {starting, running, stopping, stopped, restarting, crashed,
unknown} + `observed_at` + `assigned_worker_id`.
Server types: vanilla / paper / fabric / forge.
Execution backend: `container` (the only backend provided).

### 2.4 Versions, ports, fleet, audit, platform

| Method | Path | Notes |
|---|---|---|
| GET | `/versions` | Catalogued server types. |
| GET | `/versions/{type}` | Version list for a type. |
| POST | `/versions/refresh` `[A]` | Invalidate catalog cache (`?server_type=` optional). |
| GET | `/versions/jar-pool/stats` `[A]` · POST `/versions/jar-pool/gc` `[A]` | JAR pool size / garbage collection. |
| GET | `/ports/available?count=` · `/ports/check/{port}` | Free-port discovery / conflict check. |
| GET | `/workers` `[A]` | Fleet list: status, capabilities (drivers, max_servers, cpu/mem), assigned_count, heartbeat. |
| PUT / DELETE | `/workers/{wid}/drain` `[A]` | Set / clear drain. Set MARKS the worker's running servers `desired=stopped` and returns `servers_stopped` (the count marked, not yet stopped); the reconciler then stops each + takes the final snapshot ASYNCHRONOUSLY (after the grace window, ~120s default, + a tick) and only while the worker stays connected — keep the worker up until convergence, or stops+snapshots defer to a reconnect that never happens in a decommission. Confirm convergence PER SERVER (each reaching `observed=stopped` and unassigned), NOT by assigned load: drain decrements the load synchronously, so it drops to 0 before any stop runs. Clear only re-enables placement (does not restart them; un-draining before convergence transiently oversubscribes the worker). |
| GET | `/audit` `[A]` | Global audit (`community`, `operation`, `actor`, `since`, `until`, `limit`, `offset`). |
| GET | `/communities/{cid}/audit` | Community-scoped audit (same filters minus `community`). |
| GET | `/backups/statistics` `[A]` | Global backup statistics. |
| GET | `/healthz` · `/readyz` | Liveness / readiness (ops-facing, not UI-core). The Prometheus exposition is not on this API — it has its own listener. |
| GET | `/meta` | Deployment facts the Web UI reads before a server exists: `{relay_enabled, bedrock_enabled, default_memory_limit_mb, max_memory_limit_mb}`. Requires authentication. Used by the create wizard to decide whether to surface the game-port control (relay mode auto-allocates), and by the plugins tab to decide whether to show the Bedrock/Geyser discovery hint (`bedrock_enabled` = `relay_enabled` AND the deployment's Bedrock capability flag). |

### 2.5 Resource packs

Global resource pack library (not community-scoped) and per-server assignment.

| Method | Path | Notes |
|---|---|---|
| POST | `/resource-packs` | Upload a resource pack (multipart; requires `server:update` in at least one community). |
| GET | `/resource-packs` | List all resource packs (authenticated). |
| DELETE | `/resource-packs/{id}` | Delete a resource pack (uploader or platform admin; 409 when still assigned to a server). |
| GET / HEAD | `/resource-packs/{id}/download` | Download (authenticated). The response declares `Cache-Control: no-store`. `HEAD` is the metadata probe: the same gate and the same headers with no body, so a client learns the `Content-Length` without starting a transfer; it never opens the blob nor records a `resource_pack:download` audit event. |
| GET / HEAD | `/public/resource-packs/{id}/{filename}` | Public download (no auth) — the URL Minecraft clients fetch. Validates `filename` matches. The two statuses declare different caching policies, because the URL ends in the stored filename and an undeclared policy is decided by the edge's extension heuristic instead: the `200` declares `Cache-Control: public, max-age=3600, immutable` — a pack is immutable and the game client verifies it against `resource-pack-sha1`, so the max-age bounds only how long a deleted pack stays fetchable from a cache — and the `404` declares `Cache-Control: no-store`, since a pack's id and filename are both fixed at creation and a URL that 404s can never later become a `200`. `HEAD` is the metadata probe: this is the unauthenticated URL a resumable-download client probes before a transfer, and it declares a `Content-Length`, so it has a real reason to. The probe answers each status with the `GET`'s headers — the same `Cache-Control` per status — and no body, so an edge does not cache a probe differently from the download; it never opens the blob. |
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

### 2.7 Plugins & mods

Per-server plugin/mod content management (the `#plugins` tab, Section 6.14). All
paths hang off `/communities/{cid}/servers/{sid}`; the whole family is
per-resource gated on the server — `plugin:read` for the reads, `plugin:manage`
for the mutations — and every mutation requires the server **at rest** (409
`server_unsettled` / `server_busy` while it is transitional, Section 6.9). The
family is unsupported on `vanilla` servers (422 `unsupported_server_type`).

| Method | Path | Notes |
|---|---|---|
| GET | `…/{sid}/plugins` | List installed plugins/mods (`plugin:read`). |
| POST | `…/{sid}/plugins` | Install a plugin jar via multipart upload (`plugin:manage`): `display_name` form field + `file`, jar ≤ 512 MiB (413 `file_too_large`). Returns `201`; a duplicate is 409 `plugin_already_exists`. |
| GET | `…/{sid}/plugins/updates` | Batch-check every installed plugin for a newer catalog version (`plugin:read`); catalog upstream failure is 502 `catalog_upstream_failed`. |
| GET | `…/{sid}/plugins/validate` | Phase-B dependency/compatibility checklist — missing deps, unsatisfied version ranges, conflicts, MC-version mismatch (`plugin:read`). Read-only; never mutates the set. |
| POST | `…/{sid}/plugins/resolve` | Plan dependency auto-resolution: the transitive closure of required deps, each classified satisfied / needs-import / unresolvable / blocked (`plugin:read`). Read-only — nothing is downloaded or installed. |
| POST | `…/{sid}/plugins/resolve/apply` | Apply that plan: install each non-blocked needs-import dep from the catalog, then re-plan (`plugin:manage`). Per-dep install failures are isolated in `failed`. |
| GET | `…/{sid}/plugins/{pid}` | Read one installed plugin by id (`plugin:read`). |
| DELETE | `…/{sid}/plugins/{pid}` | Remove an installed plugin (`plugin:manage`). Returns `204`. |
| GET | `…/{sid}/plugins/{pid}/updates` | Check a single plugin for a newer catalog version (`plugin:read`). |
| POST | `…/{sid}/plugins/{pid}/update` | Update a plugin to a specific catalog `version_id` (`plugin:manage`); missing project 404 `catalog_project_not_found`, checksum drift 502 `checksum_mismatch`. |
| GET | `…/{sid}/plugins/{pid}/dependencies` | List a Modrinth-sourced plugin's declared dependencies, each flagged installed/missing (`plugin:read`). |
| POST | `…/{sid}/plugins/{pid}/enable` · `/disable` | Toggle a plugin on/off (`plugin:manage`). |
| POST | `…/{sid}/plugins/{pid}/side` | Override a mod's side — `both` / `server` / `client` (`plugin:manage`); re-materializes the working set. Invalid side is 422 `invalid_side`. |
| GET | `…/{sid}/client-mods` | List the server's enabled client-relevant plugins (side `client` / `both`; `plugin:read`). |
| GET / HEAD | `…/{sid}/client-mods/download` | Download those client mods bundled as `mods.zip` (`plugin:read`). The response declares `Cache-Control: no-store` — a per-server body gated by `plugin:read` must never be served from a shared cache. `HEAD` is the metadata probe: the same gate and headers with no body, and it neither builds the zip nor pulls a jar; the zip is streamed with no `Content-Length` (assembled on the fly from a variable jar set), so the probe learns existence rather than size. |
| GET | `…/{sid}/catalog/search` | Search the Modrinth catalog with auto-applied server facets (`plugin:read`): `q` query + `limit` (1–100, default 20) / `offset` paging. Catalog upstream failure is 502 `catalog_upstream_failed`. |
| GET | `…/{sid}/catalog/projects/{id_or_slug}` | Fetch a catalog project's detail + its server-compatible versions (`plugin:read`); an unknown project is 404 `catalog_project_not_found`, catalog upstream failure 502 `catalog_upstream_failed`. |
| POST | `…/{sid}/catalog/install` | Install a plugin/mod from the catalog by `project_id` + `version_id` (`plugin:manage`). Returns `201`; a missing project is 404 `catalog_project_not_found`, checksum drift 502 `checksum_mismatch`, and a duplicate 409 `plugin_already_exists`. |

The `…/{sid}/catalog/*` rows are a separate route family (`catalog.py`) —
the Modrinth browse/install that backs the same `#plugins` tab — folded in here
because they share the `plugin:*` gate and the at-rest / vanilla constraints
above.

## 3. Personas and capability scoping

| Persona | Sees | Typical UI surface |
|---|---|---|
| Community member | Only their communities; actions filtered by role permissions ∪ grants | Servers list, server detail (capabilities vary per permission) |
| Community owner | Everything in their community | + Members, Roles, Grants, Groups, Audit, Community settings |
| Platform administrator | All communities + platform area | + Admin area: Users, Communities provisioning, Workers, Version catalog/JAR pool, Global audit & backup stats |
| Unauthenticated | Login / Register only | — |

The UI derives capabilities from `GET /users/me` (admin flag) +
`GET /communities/{cid}/me/permissions` (the caller's effective set),
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
- Schedule: backup cadence is a first-class `backup` schedule on the general
  scheduler (the `…/{sid}/schedules` surface, Section 6.13), not an inline
  cadence field on this tab; the Backups tab shows a short note pointing
  `backup:schedule` holders there. There is no `backup_interval_hours` config
  key — a PATCH carrying one is 422 `retired_config_key`.
- Retention (`backup:schedule` only — hidden otherwise): a policy
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
  username (`POST …/members {username}` — no-match is a 422
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
  user); delete a community (typed-confirm) via `DELETE /communities/{cid}`.
- **Workers**: table (id, version, status incl. draining, drivers,
  assigned/max, cpu/mem, heartbeat age); drain/undrain toggle with confirm.
- **Versions**: per-type catalog freshness, refresh button (all or one type);
  JAR pool stats + GC trigger showing reclaimed bytes.
- **Audit**: global log with community filter added.

### 6.13 Server detail — Schedules

The `#schedules` tab (numbered out of tab order so that 6.8–6.12 keep their
numbers): the UI for the general scheduler.

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

The `#plugins` tab (numbered out of tab order so that 6.8–6.13 keep their
numbers): plugin/mod content management for a server. The tab label
and every content noun are loader-aware — **Plugins** for Paper, **Mods** for
Fabric/Forge — and the whole tab is **hidden for `vanilla`** (no
backend support; the tab body also self-guards with an "unsupported" notice).

- Installed list (`GET …/plugins`): a table of name (with an "update available"
  badge when a newer catalog version exists, from `GET …/plugins/updates`),
  version, source badge (`modrinth` / `local`), status pill
  (enabled / disabled), size, and — for mod loaders only — a **Side** column
  (`both` / `server` / `client`), editable with `plugin:manage`
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
  (`GET …/plugins/validate`) flags missing dependencies, unsatisfied
  version ranges, conflicts, and MC-version mismatches, or shows an all-clear
  line; **Resolve** (`POST …/plugins/resolve` → `…/resolve/apply`) plans
  and then auto-imports the missing Modrinth dependencies.
- Client modpack (mod loaders only): when at least one enabled mod is
  client-relevant (side `client` / `both`), a **Download client modpack** button
  bundles them (`GET …/client-mods/download`; response headers and the
  `HEAD` metadata probe are in the Section 2.7 inventory row).
- Bedrock hint: on a Paper server, when the deployment's Bedrock gate is on
  (`/meta`'s `bedrock_enabled`, Section 2.4) and a Geyser plugin is installed, an
  inline note links to Floodgate setup.
- Server-state gating: reads render anytime, but **every mutation requires the
  server at rest** (`desired_state` stopped and `observed_state` one of
  stopped / crashed / unknown). While not at rest a
  notice shows and all install / enable / disable / update / remove / side /
  resolve controls are disabled; a server-busy API reason (e.g.
  `server_not_stopped`) surfaces as an error toast.
- Permission gating (7.3): the tab body needs `plugin:read` (a member without it
  sees a short notice); every mutation control — install, enable / disable,
  update, remove, side, and Resolve — needs `plugin:manage`, and the per-row
  action buttons render only with it. The **Download client modpack** button is
  a read-only action and renders independently of `plugin:manage` (any reader on
  a mod-loader server with an enabled client-relevant mod sees it).

## 7. Cross-cutting concerns

### 7.1 Auth/session lifecycle
- The API-side contract these notes consume — endpoint status codes, the
  body-vs-cookie transport rules, and the refresh reuse grace window the
  single-flight mutex below guards against — is documented in
  [`AUTH_API.md`](../app/AUTH_API.md).
- Access token (short-lived; ~900 s in the live deployment) kept in memory
  only. Refresh token in an **httpOnly cookie** set by the API on login
  (`Secure; SameSite=Strict; Path=/api/auth`) — never readable by JS; carried by
  the API's cookie transport (AUTH_API.md Section 3). Transparent refresh on 401 +
  single-flight refresh mutex; hard logout on refresh failure. Page reload
  (bootstrap) re-establishes the session via the **non-rotating**
  `POST /api/auth/session` probe — it exchanges the cookie for an access token
  without rotating the refresh token, so a reload / F5 storm can never race an
  in-flight rotation and leave a revoked predecessor cookie in the jar (the
  torn-rotation logout). Rotation stays on the transparent
  `POST /api/auth/refresh` (the in-session 401-retry path), where reuse-detection
  still applies; a mid-session refresh failure hard-logs-out only when the
  server's response is auth-definitive (401 or 403 — a genuinely expired /
  revoked session). Transient failures — network errors, proxy 5xx, or garbled
  bodies — do not end the session; the original request surfaces its own error
  to the caller so the user can retry.
- WS connections carry the access token in the `Sec-WebSocket-Protocol`
  subprotocol header (`["access_token", "<jwt>"]`); on token
  rotation, sockets are reconnected (reconnect-on-rotate chosen).
- **Authenticated downloads.** An in-memory access token cannot ride a plain
  `<a href>`, so single-file / resource-pack / plugin downloads fetch the URL
  with the Authorization header and buffer the response as a Blob, capped at
  512 MiB to bound memory. **Backup archives, server exports and directory ZIPs
  are the exception**: they run to multiple GB, so the tab mints a short-lived
  self-authenticating URL (`POST …/backups/{bid}/download-grant`,
  `POST …/{sid}/export/download-grant` and
  `POST …/{sid}/files/download-grant?path=…` — all with the
  `auth.token.download_grant_ttl_seconds` TTL, 30 s by default) and clicks
  an `<a download>` at it — same-origin (7.7), so the browser saves the
  response natively with no size ceiling and no bytes read by the application.
  The grant is minted on click, never on render or on selection, and the
  `download` attribute names the file — redundant but harmless, since every
  download response (backups, server exports, directory ZIPs, resource packs
  and plugins) sends a `Content-Disposition`. A
  **single file deliberately stays on the capped fetch** even though it shares
  the download route with a directory: the API declares its `Content-Length`
  whenever the size resolves from the parent listing, so an oversize one is
  rejected up front with the too-large toast (and when it does not resolve, the
  response falls back to chunked and the byte counter still caps it), whereas
  the anchor path would save the error document under the intended filename.
  Mint-time failures (403 / 404, and 409 `server_unsettled` off the at-rest
  precondition) surface as toasts; once the click is handed off, the browser's
  download manager owns progress and errors — for an incrementally built zip
  that means bytes-so-far with no total, since there is no `Content-Length`.
  The tab does nothing further to keep such a download alive: redeeming the grant
  sets an `HttpOnly` download cookie the tab cannot see, and the browser's own
  retry of an interrupted transfer authenticates with that (AUTH_API.md
  Section 3). It is scoped to the one download's URL path, so it is never
  attached to an API call the SPA makes, and JS never reads it — the SPA's session
  model (in-memory access token, refresh cookie on `/api/auth`) is untouched.

### 7.2 Real-time strategy
- One WS per open server-detail page + one community WS for the dashboard.
- Reconnect with exponential backoff + jitter; resubscribe on open; banner
  shows degraded mode; REST polling fallback for status only.

### 7.3 Permission-driven rendering
- Capabilities come from `GET /communities/{cid}/me/permissions`:
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
  (7.3). The client branches on exactly this shape — one body, one machine
  code, on every endpoint. The auth endpoints' reason codes and the
  `permission` member are enumerated in [`AUTH_API.md`](../app/AUTH_API.md)
  Section 2.
- API error surfaced via toast + inline field errors (422 `errors` list).
- Conflict-flavored errors get a "state changed — refresh" treatment, not a raw
  error dump: the lifecycle races `invalid_transition` (except on **start**,
  where it means the server is already desired-running — a pending start, not a
  race — and gets a verb-specific message below), `transition_conflict`
  and `server_not_running` (the last only away from **restart**, which offers
  the action for a crashed server on purpose and so gets a verb-specific message
  below). A 409 that reports something other than a race is
  named instead, since refreshing is not the remedy: `server_unsettled` says the
  server must be stopped, on every surface that can receive it;
  `worker_busy` / `server_busy` say another operation on the server is still
  running and the request was refused without being applied, so the operator
  retries in a moment; the sanitized start/restart-failure categories
  `port_conflict` / `image_missing` name the cause the Worker classified;
  `command_failed`, the catch-all for a dispatch failure the Worker did not
  classify, says the action did not go through and sends the operator to check
  the server's state — a failed start is compensated back, but a failed stop or
  restart can leave the server moved, and a retry is not known to help;
  `failed_stop_orphan` says an earlier stop never finished, so the server's
  process may still be running — it never went down, and repeating the action is
  refused identically, so the message names that condition and says the host is
  converging it automatically, which it does; asking the operator to stop the
  server again would be redundant. It stays verb-agnostic for that reason: the
  sentence is about the server's state, not about what the refused verb would
  have done.
  The refetch is unconditional on every lifecycle mutation regardless of the
  toast, so a moved server still shows up.
- On **stop** and **restart** those messages are replaced by verb-specific ones —
  `command_failed` and `worker_busy` on stop, `command_failed` and
  `server_not_running` on restart (`worker_busy` on restart applied nothing, and
  `server_busy` cannot occur on either). The API dispatches after committing the
  intent and compensates only on start, so these failures leave something
  *pending* rather than undone.
  A failed stop keeps `desired_state=stopped` over a still-running process — no
  stop failure class proves the stop will not take effect — so the message says
  the server is still running and that the system will keep trying to stop it,
  rather than asking for a retry that is already happening. `worker_busy` on stop
  gets the same message for the same reason: the stop intent is committed before
  the Worker refuses. A failed restart keeps `desired_state=running`, so a server
  the Worker took down comes back on its own, and the message says so.
  `server_not_running` on restart — the Worker holding no live instance for the
  id — shares that message: the lifecycle controls offer restart for a
  server observed crashed or unknown under a running intent, so this refusal is
  not the race it is on the command surface, and what it leaves behind is the
  same `desired_state=running` the reconciler acts on.
- The same verb-specific treatment extends to the **503 `worker_unavailable`** —
  the API's rendering of a dispatch that timed out or lost the Worker session,
  where the generic "wait a moment and try again" invites a retry of an
  intent that already stands. On stop it is unambiguous: `StopServer` can only
  raise it from the dispatch itself, which runs *after* `desired_state=stopped`
  is committed and the placement load decremented, and nothing is compensated. On
  restart `desired_state=running` stands, exactly as for `command_failed`. The
  wording is deliberately *not* the pending pair above: a refusal was reported by
  a host that answered, so those messages can say what the server did, while a
  timeout answers nothing — a graceful stop merely outliving the API's dispatch
  deadline is the commonest case, and it usually succeeds — so the 503 messages
  say the outcome is **unconfirmed** and the intent stands. `no_eligible_worker`
  and `jar_unavailable` stay verb-agnostic: both are raised before any intent is
  committed.
- On **start**, `worker_busy` gets its own message, but a *hedged* one, because
  the reason is ambiguous at the edge. A **post-dispatch** `worker_busy`
  keeps `desired_state=running` and the assignment so `redispatch_start` can
  converge once the raced command settles — the start is pending; a
  **pre-dispatch** one — a refused hydrate before the start command was sent —
  compensates back to stopped, so nothing is pending. The client sees one bare
  `worker_busy` for both, so the message cannot promise the start will happen: it
  says the start *may* still be applied on its own and to start again only if the
  server stays stopped, rather than the generic "wait and try again". That
  generic retry is actively misleading on the pending path — a retry while
  `desired_state=running` raises `invalid_transition`, which on **start** carries
  its own message ("already running or starting up") rather than the generic
  state-changed toast, so the operator does not get a second, contradictory
  answer. Stop and restart keep the state-changed treatment for
  `invalid_transition`: neither leaves a pending intent a retry collides with.
- **Start keeps the verb-agnostic message for 503 `worker_unavailable`.** A start
  that demonstrably did not happen is compensated back to stopped, but a
  **post-dispatch** `worker_unavailable` is not (the start may have been applied),
  while its pre-dispatch twin — a failed hydrate, or a call that never reached the
  Worker — compensates. This is the identical pre/post ambiguity as `worker_busy`
  above, but a timeout answers *nothing*, so there is no honest thing to say
  beyond "could not reach the server host" — hence the verb-agnostic message.
- Destructive operations (delete server/community/user/backup-restore) use
  typed-confirm dialogs.

### 7.5 i18n & theming
- All strings through a `t('key')` dictionary; English is provided, Japanese
  addable. Dark theme via CSS custom properties (a light theme later is a
  token swap, not a rewrite).

### 7.6 Tech stack
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
- **`/api` namespace.** The entire HTTP API (REST, WebSocket, and
  the OpenAPI schema/docs) lives under `/api`, so it can never share a path with
  an SPA client-side route. This makes the production fallback an absolute rule:
  `/api/*` is the API, *everything else* is the SPA. Without the namespace,
  deep-links such as `/communities/{cid}`,
  `/communities/{cid}/servers/{sid}` and `/communities/{cid}/servers/new` would
  collide with API GET routes and return JSON on a hard reload.
- **Development.** The Vite dev server proxies the single `/api` prefix (REST
  *and* the WebSocket event streams) to a local API instance, so the browser sees
  a single origin (the dev server). Because `/api` is never an SPA route, the
  proxy needs no Accept-header bypass for deep-links. No CORS is added anywhere.
- **Production.** The API container serves the built SPA (`webui/dist`) via
  FastAPI `StaticFiles` with an SPA fallback, on the same origin as the API. No
  reverse proxy and no new Compose service. The `/api/*` routes (including the WS
  paths and the health/readiness probes) and `/assets/*` (the built SPA
  chunks) are both excluded from the SPA fallback: a `/api/*` path is the API,
  and an unmatched `/assets/*` request returns 404 (a stale/renamed chunk, never
  a client-side route). Every other unmatched path falls back to the SPA's
  `index.html` so client-side routing works on deep links and reloads.

## 8. Out of scope

The Web UI does not provide:

- Metrics history or persistence — only live sparklines from the WS stream.
- `/metrics` (Prometheus) visualization — operators use Grafana.
- Mobile-optimized layouts — responsive down to tablet only.
- A light theme — the CSS custom-property structure carries one, but only the
  dark theme is provided (7.5).
- Active-session listing / revocation on the account page. The session API
  exists (`GET` / `DELETE /api/users/me/sessions`, AUTH_API.md Section 7); the
  account-page surface for it is not provided.

## 9. Design decisions

The decisions this specification rests on, with the sections that carry them:

| # | Topic | Decision | Refs |
|---|---|---|---|
| D1 | Stack | **React + TypeScript + Vite** (TanStack Query, generated OpenAPI client). | 7.6 |
| D2 | Refresh-token storage | **httpOnly cookie** — never `localStorage`, so the refresh token is never readable by JS. Carried by the API's cookie transport (AUTH_API.md Section 3). | 7.1 |
| D3 | "My permissions" endpoint | The API exposes the caller's effective set at `GET /communities/{cid}/me/permissions`, so the UI scopes controls from one call per community. | 3, 7.3 |
| D4 | Member-add lookup | `POST …/members` accepts exactly one of `user_id` or an exact `username`; there is no fuzzy user search. | 6.10 |
| D5 | Where the UI lives | **`webui/` in this monorepo**, alongside `api/` / `worker/` / `proto/` (REQUIREMENTS.md Section 1.2). Mockup stays under `docs/ui/mockup/` as a design reference. | 1, header |
