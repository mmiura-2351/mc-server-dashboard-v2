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
| GET / PATCH / DELETE | `/communities/{cid}` | Read / rename / delete. Delete also allows a platform admin to remove any community (orphan cleanup); read/rename stay membership-scoped. Delete answers 409 `community_has_servers` while the community holds any server: its servers are deleted first. |
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
| GET / POST | `/communities/{cid}/servers` | List / create (`name`, `mc_edition`, `mc_version`, `server_type`, `config`, `accept_eula`, optional `game_port`). A `config` that breaks the blob's bounds is a `422` naming the rule it broke — `config_invalid_shape` / `config_null_value` / `config_lone_surrogate` / `config_too_large` (the rules and their limits are in DATABASE.md Section 7). The `name` is stored trimmed; one that is blank or carries a control character (C0, DEL or C1) is a `422` (`invalid_server_name`), with no server created (#3957). A lone surrogate never reaches that check: the body's own validation refuses it as `422 validation_error`. |
| POST | `/communities/{cid}/servers/import` | ZIP import (multipart). The `name` form field is refused as create's `name` is (`422 invalid_server_name`, or `422 validation_error` for a lone surrogate), before the server row is created (#3957). A member named `server.properties/…` would publish that name as a directory, which the platform's own writes cannot survive — it is a `422` (`platform_managed_path`), refused before the server row is created, so nothing is left behind. Only the root name is guarded: a nested `backups/server.properties/…` member is ordinary user data. A traversal (zip-slip) member is a `422` (`invalid_path`) the same way. A member at a root name starting `.mcsd_generation` — the Worker's generation marker — or under one is a `422` (`reserved_name`), refused the same way rather than dropped. A body the zip reader fails on — not a zip at all, cut short, damage the reader detects in a member, or an encrypted member, the metadata included — is a `422` (`invalid_archive`), refused before the server row is created. The import counts toward the archive-upload limit it shares with `…/backups/upload` and `…/files/upload?extract=true`: at most four archive uploads at a time across the three routes. One past that is a `503` (`upload_busy`), refused before its body is read into memory and before the server row is created; sending it again shortly is the remedy (#3912). |
| GET / HEAD | `…/{sid}/export` | ZIP export (download). Accepts the Bearer access token, or a `?grant=` download grant so the browser can stream a multi-GB export straight to disk, or the `HttpOnly` download cookie a redemption sets so an interrupted transfer can be retried. The response declares `Cache-Control: no-store` under every credential. `HEAD` is the metadata probe: the same gate and the same headers with no body — including no `Content-Length`, since the zip is built incrementally and the `GET` declares none either — and it neither builds the zip nor records a `server:export` audit event. |
| POST | `…/{sid}/export/download-grant` | Mint that grant: `{download_url, expires_at}`, `Cache-Control: no-store`. Same `file:read` gate as the export, and the same pre-flight — a running server is 409 `server_unsettled` and no grant is issued; `auth.token.download_grant_ttl_seconds`, 30 s by default (AUTH_API.md Section 3). |
| GET / PATCH / DELETE | `…/{sid}` | Read / update (name, config, game_port) / delete. Every PATCH edit needs `server:update`. A `name` that is blank or carries a control character or a lone surrogate is a `422` (`invalid_server_name`), with nothing changed (#3957). The check is on the name sent, even when it equals the stored one, and the Settings tab always sends the name: a server stored with such a name before the rule takes no save until it is renamed. Reads return a stored name as it is. `backup_interval_hours` is not a config key; a PATCH carrying it is `422` (`retired_config_key`; DATABASE.md Section 7) — backup cadence is a `backup` schedule. A `config` breaking the blob's bounds is a `422` naming the rule it broke, the same four reasons create answers with — `config_invalid_shape` / `config_null_value` / `config_lone_surrogate` / `config_too_large` (rules and limits: DATABASE.md Section 7). A `game_port` change against a server whose `server.properties` is missing is a `409` (`server_properties_missing`): the rewrite preserves the file's other keys, so it refuses rather than republish one without `rcon.password`. A DELETE needs the server at rest (`409 server_not_stopped` otherwise) and no Worker assigned: a stop holds its Worker until the final snapshot settles and the server is released, which can wait for the reconciler, and a delete before then is a `409` (`server_assigned`) whose `detail` says to stop the server and wait for the release. A DELETE that waited out the lifecycle lock's acquire budget behind another lifecycle operation is the `409 server_busy` instead, which a retry in a moment clears. |
| POST | `…/{sid}/start` · `/stop?force=` · `/restart` | Lifecycle. Stop supports force. A stop of a server already `desired=stopped` but still assigned to a Worker (a drained server still running, a stop left half-done) is accepted and converges it at once, as the reconciler would after its grace window; with no Worker assigned it is `409 invalid_transition`, and while another stop of that server is still converging it is `409 transition_conflict`. A stop of a server `desired=running` with no Worker assigned yet withdraws the start intent and dispatches nothing. A `503 worker_unavailable` from any of the three carries a `worker_state` member, `not_connected` or `no_answer`, when the API knows why the Worker gave no answer ([AUTH_API.md](../app/AUTH_API.md) Section 2); Section 7.4 says how the UI uses it. |
| POST | `…/{sid}/command` | RCON line → `{output}`. `line` is at most 1,446 UTF-8 bytes (`422`; "Console lines" below). |
| GET | `…/{sid}/files?path=&list=` | Read file (base64) or list directory (entries + `truncated`). |
| PUT / DELETE | `…/{sid}/files?path=` | Write (base64, versioned) / delete. The root `server.properties` is guarded on **every** files-API write path — `PUT`, `DELETE`, either end of a rename, an upload (direct or archive member), and a rollback: an operation that would change or drop a platform-managed key — `server-port`, the RCON triple, the resource-pack keys — is a `422` (`platform_managed_key`) naming it in the `key` member. The guard is over the keys, not the filename: editing the file's other keys is unaffected, and a root `server.properties` that carries none of those keys still deletes and still renames away. A `PUT` to a path **under** that name is a `422` (`platform_managed_path`) instead — the write creates the name as a directory, which the key guard cannot see and the platform's writes cannot survive. A `PUT` to a root name starting `.mcsd_generation` — the Worker's generation marker — or under one is a `422` (`reserved_name`), running or at rest; a `DELETE` of such an entry stays allowed, so one planted before the guard can be removed while the server is stopped. |
| POST | `…/{sid}/files/directories?path=` | mkdir. The root `server.properties` path — and anything under it, whose missing parents are created — is a `422` (`platform_managed_path`): the platform's own writes publish a file there, so no directory may stand in its place. A root name starting `.mcsd_generation`, or a path under one, is a `422` (`reserved_name`). |
| GET / HEAD | `…/{sid}/files/download?path=` | Raw download (file bytes, or a streamed ZIP for a directory). Accepts the Bearer access token, or a `?grant=` download grant so the browser can stream a multi-GB directory straight to disk, or the `HttpOnly` download cookie a redemption sets so an interrupted transfer can be retried. The response declares `Cache-Control: no-store` under every credential. `HEAD` is the metadata probe: the same gate and the same headers with no body — a file's `Content-Length` when it is known, none for the incrementally built directory ZIP — and it neither opens the download stream (no directory ZIP is built, no file bytes are streamed) nor records a `file:download` audit event. The file/directory dispatch itself is the `GET`'s, unchanged: resolving which branch a path takes reads the parent listing and, for a file, pulls one chunk to confirm it is readable. The `GET` then begins its stream **before** the response headers, so a path that went away after that dispatch is a `404` and a storage outage a `503` (`storage_unavailable`) rather than a `200` whose body never arrives; only a failure once the body is flowing still aborts the transfer. |
| POST | `…/{sid}/files/download-grant?path=` | Mint that grant: `{download_url, expires_at}`, `Cache-Control: no-store`. Same `file:read` gate as the download, and the same pre-flight — missing path 404, traversal 422 `invalid_path`, running server 409 `server_unsettled`. `path` is a **query** parameter so mint and redemption bind the identical string; `auth.token.download_grant_ttl_seconds`, 30 s by default (AUTH_API.md Section 3). |
| POST | `…/{sid}/files/upload?path=&extract=` | Multipart upload, optional ZIP extract. An upload landing on the root `server.properties` — directly or as an archive member — that changes a platform-managed key is a `422` (`platform_managed_key`); an offending archive member is caught before any write, so the whole extract is refused with nothing written. An upload landing **under** that name — `path` naming it, or a member named `server.properties/…` — is a `422` (`platform_managed_path`), caught in the same pre-write scan; the check is on the resulting path, so the same member under a subdirectory is ordinary user data. An upload or member landing at a root name starting `.mcsd_generation`, or under one, is a `422` (`reserved_name`), caught in the same scan. With `extract=true`, a body the zip / tar.gz reader fails on — not an archive at all, cut short, damage the reader detects, an encrypted member or an unsupported compression method — is a `422` (`invalid_archive`); the whole archive is read before the first write, so nothing is written. A tar.gz is read to its gzip end, so a header past the first that does not read, no end-of-archive marker, anything but zeros past it, a gzip CRC32 or ISIZE that does not match, and bytes after the gzip stream that are neither another gzip member nor zero padding are a `422` (`invalid_archive`) too (#3778), and so are a sparse member whose map reaches past its stored data, which would make the reader seek back, a chain of extended headers too long for the reader, and a walk over the header limits, whose `detail` names the limit: more than 1 MiB of headers before a member, extended headers (PAX records, long names) included, or members that hold more than 32 PAX records each, global ones included, or more than 8 Mi characters of names, link names and PAX records in all (#3810). Every byte a tar.gz decompresses counts toward the extracted-size cap, headers, extended-header data and skipped member data included (#3810). With `extract=true` the upload counts toward the archive-upload limit it shares with `…/backups/upload` and `…/servers/import`: at most four archive uploads at a time across the three routes, two being checked and two waiting. One past that is a `503` (`upload_busy`), refused before its body is read into memory and before anything is written; sending it again shortly is the remedy (#3843). An upload without `extract` is never refused this way. The part's filename names the file written under `path`, so one that cannot name a file is a `422` (`invalid_filename`), refused before the upload is read into memory (and, with `extract=true`, before an archive-upload slot is taken), with nothing written: a name that is blank, carries a control character (C0, DEL or C1), a lone surrogate or a Unicode noncharacter, contains `/` or `\`, or is over 255 bytes in UTF-8 (#3922). The path rules above still apply to a name that passes. |
| POST | `…/{sid}/files/rename` | `{from, to}`. Both ends are guarded against the root `server.properties`: renaming it away is refused when it holds a platform-managed key, and renaming another file onto that name is refused when that file carries one — either is a `422` (`platform_managed_key`) naming the key. A rename **onto** that name whose source exceeds the edit cap is a `413`, since the guard compares the source bytes rather than scanning an unbounded body. Renaming a **directory** onto that name, or anything — a file included — to a path **under** it, is a `422` (`platform_managed_path`) — a directory there breaks the platform's writes; moving one already standing there away still works. A destination at a root name starting `.mcsd_generation`, or under one, is a `422` (`reserved_name`); the source is not guarded, so such an entry can still be renamed away. |
| POST | `…/{sid}/files/search` | `{query, by, max_results}` → matching paths. |
| GET | `…/{sid}/files/history?path=` | Retained version ids. |
| POST | `…/{sid}/files/rollback?path=` | `{version_id}`. Rolling the root `server.properties` back to a version whose platform-managed keys differ from the file's current ones — a stale `server-port` or `rcon.password` — is a `422` (`platform_managed_key`); a version differing only in the user's own keys rolls back normally. A path at a root name starting `.mcsd_generation`, or under one, is a `422` (`reserved_name`), even for a version retained before the name was reserved. |
| GET / POST | `…/{sid}/backups` | List / create on-demand backup. A working set whose archive would decompress past the 8 GiB backup cap is a `409` (`working_set_too_large`), with nothing written: such a backup could be neither restored nor uploaded again (#3859). |
| GET | `…/{sid}/backups/statistics` | count / total bytes / newest / oldest. |
| POST | `…/{sid}/backups/upload` | Upload an off-host backup archive. A restore republishes the archive's members verbatim as the working set, so a member named `server.properties/…` — or a directory member named `server.properties` — is a `422` (`platform_managed_path`), refused before the archive is stored; a nested `backups/server.properties/…` member is ordinary user data. A member at a root name starting `.mcsd_generation`, or under one, is a `422` (`reserved_name`), refused before the archive is stored. A non-archive or otherwise unsafe member stays a `422` (`invalid_archive`), whose `detail` names the refusal (for a member, its name and the reason; a name over 256 characters is cut to its first 256 and marked `…(truncated, N chars)`). The archive is read to its gzip end, so a header past the first that does not read, no end-of-archive marker, anything but zeros past it, a gzip CRC32 or ISIZE that does not match, or bytes after the gzip stream that are neither another gzip member nor zero padding is a `422` (`invalid_archive`) too (#3778), and so are a sparse member whose map reaches past its stored data, which would make the reader seek back, a chain of extended headers too long for the reader, and a walk over the header limits, whose `detail` names the limit: more than 1 MiB of headers before a member, extended headers (PAX records, long names) included, or members that hold more than 32 PAX records each, global ones included, or more than 8 Mi characters of names, link names and PAX records in all (#3810). The archive is published under the server's lifecycle lock, so while another lifecycle operation holds that lock past its acquire budget the upload is a `409` (`server_busy`), refused without being stored; the operator retries. The upload counts toward the archive-upload limit it shares with `…/files/upload?extract=true` and `…/servers/import`: at most four archive uploads at a time across the three routes, two being checked and two waiting. One past that is a `503` (`upload_busy`), refused before its body is read into memory and without being stored; sending it again shortly is the remedy (#3843). Once its body is read, an upload whose client disconnects still runs to its end and is stored. |
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
| GET / POST | `…/{sid}/schedules` | List / create a per-server schedule (`name`, `action` ∈ command\|start\|stop\|restart\|backup, `cron` XOR `interval_seconds`, `timezone`, `enabled`, `command` for `command`, `warning_steps` for stop/restart, `only_when_running` for backup — defaults to `true`, skipping an occurrence that finds the server stopped; rejected on any other action). Reads need `schedule:read`; writes need `schedule:manage` **and** the action's own permission (`command`→`server:command`, `start/stop/restart`→`server:{start,stop,restart}`, `backup`→`backup:schedule`) — anti-escalation. Authorization is write-time only: the runner executes as the system, so revoking a permission does not stop existing schedules. `next_run_at` is null while disabled, recomputed on enable. |
| GET / PATCH / DELETE | `…/{sid}/schedules/{scid}` | Read / edit (partial; action immutable) / delete a schedule. |
| GET | `…/{sid}/schedules/{scid}/runs` | Execution history newest-first (`schedule:read`). |

**Request body limits.** Every route reads at most a fixed amount of request
body and answers a larger one **413**, by `Content-Length` before reading
anything and by streamed size otherwise — so a body is never buffered, parsed or
spooled to disk past its route's limit. The limits are constants:

| Routes | Most body read | 413 `reason` |
|---|---|---|
| Every route not listed below (all JSON routes) | 1 MiB | `request_too_large` |
| `PUT …/files` | 11,250,352 bytes: twice the base64 of a 4 MiB file (2 × 5,592,408 characters) + 64 KiB. Twice, because a JSON encoder may write the `/` of base64 as `\/`, and a file of all `0xFF` bytes is nothing but `/`; a body that `\uXXXX`-escapes its characters is not provided for | `file_too_large` |
| `POST …/files/upload`, `POST …/servers/import` | 512 MiB file + 1 MiB of multipart envelope | `file_too_large` |
| `POST …/backups/upload` | 512 MiB file + 1 MiB | `too_large` |
| `POST …/plugins` | 512 MiB jar + 1 MiB | `file_too_large` |
| `POST /resource-packs` | 256 MiB pack + 1 MiB | `too_large` |
| `POST /data-plane/…/snapshot` (Worker only) | 50 GiB, with its own `Content-Length` gate | `snapshot_too_large` |

Each listed limit is the route's own cap plus room for its envelope, so the
route's own check still decides at the cap and the answer is the same reason
however far past it the body was. `GET`, `HEAD` and `OPTIONS` requests are not
read at all. A request that carries `Transfer-Encoding` has no declared length,
whatever `Content-Length` it also carries (the server frames it as chunked and
ignores the length): it is bounded by streamed size only. An edit's `content_base64` longer than 5,592,408 characters is
refused 413 `file_too_large` before it is decoded; one that decodes to more than
4 MiB is refused the same way after.

**A body sent without a credential is held to 64 KiB.** The credential is
checked from the request's headers alone, before its body is read. A valid user
access token or the Worker credential keeps the route's limit above. Any other
request is held to **64 KiB** of body, or to its route's limit if that is
smaller: no `Authorization` header, a malformed or invalid one, an expired
token, or a deactivated account's token. Past 64 KiB such a request is answered
`413 credential_required_for_body`, by `Content-Length` before reading anything
and by streamed size otherwise. That reason is distinct from the route's own cap
(`request_too_large` and the reasons in the table above). Within the limit the
body is read and the route answers as usual, with its `401`, after the read. The
routes that take a body without a credential are `POST /users`, `POST
/auth/login`, `/auth/refresh` and `/auth/logout`, and their bodies are far below
the limit. A caller with a valid credential whom the route refuses gets its
`403` or `404` once the body, within the route's limit, has been read. An upload
or a large edit sent with an expired access token is therefore answered `413
credential_required_for_body`, not `401`; the client refreshes and sends it
again, as on a 401 (Section 7.1).

**An answer given before the body was read through** — the refusals above, a
`413`, or any response to a request whose body was not read, on any method and
whether or not a route matched (a `404` or `405` included) — is delivered in one
of two ways. If at most **16 MiB** of the declared body is still unread, the
server reads and drops it (for at most 10 seconds) and then answers, so the
client finishes sending, receives the answer, and the connection stays usable.
Otherwise — a longer rest, a transfer-encoded body (no declared length, even
beside a `Content-Length`), a rest that does not arrive in time, a body the
client is holding back for `100 Continue`, or any unread body at all on a `GET`,
`HEAD` or `OPTIONS` — nothing more is read: the response carries
`Connection: close` and the connection is closed once it is sent, so a client
does not decide how long it stays open. A request with no body (neither header,
or a length of zero) is never affected. A client still writing a large body at that moment may see
the connection reset before it reads the answer; a client that must have the
answer for a large upload sends `Expect: 100-continue`, or reads while it writes.

**Console lines.** A console line is one RCON command, and the game server
accepts at most **1,446 UTF-8 bytes** of command (a longer packet makes it drop
the connection without answering). `POST …/command`'s `line` and a schedule's
`command` are held to that, and a schedule warning step's `message` to 1,442
bytes (it is broadcast as `say <message>`); past it they are a `422`
`validation_error` whose entry is `string_too_long`. The limit is in bytes, so
non-ASCII text reaches it in fewer characters. A schedule stored before the
limit existed is not rewritten: an over-long command fails its run with the
detail `console_line_too_long`, and an over-long warning is not broadcast.
On a network path that splits the command packet (a smaller MTU than Docker's
default bridge), the game server can drop a command near the limit by closing
its console connection without answering; that command fails with `409`
`command_dropped` (`CONTROL_PLANE.md` Section 3.1), and the Console tab says so
instead of reporting a failed connection. A server that ran the command and
then crashed looks the same, so the line says the outcome is unknown and asks
the user to check the console output and server state before retrying.

**A refused `server.properties`.** The server's own code can replace its
`server.properties` with a symbolic link, a FIFO or other special file, or a
file over 4 MiB, and the host then refuses to read it (`SECURITY.md`
Section 6). A start, a restart (on its relaunch), a console command and a
backup of the running server each need that file, and each fails with `409`
`server_properties_unreadable` (`CONTROL_PLANE.md` Section 7) instead of
`command_failed`. The response carries only the reason: which refusal it was,
and the host path, stay in the logs, and nothing of the file's content is ever
sent. The lifecycle toast and the Console line tell the user to restore a
regular `server.properties` while the server is stopped, giving a backup
restore as the example. They do not point at the Files tab: a snapshot leaves a
link or special file out, so the stored copy then has no `server.properties`
at all, and the Files tab refuses to recreate the platform-managed keys
(`rcon.password` among them) that a backup restore re-applies. The Backups tab
says a stopped server can still be backed up, since that backup needs no
console connection. A graceful **stop** of such a server is not refused: it
skips the final save-all the platform sends first, which needs RCON, and falls
back to a stop signal. The Worker waits while the server's own shutdown save
keeps writing, but a server killed after the idle window or at the cap is left
with a partial save (issue #3436). The stop answers `200` as usual; only the
host log says the flush was skipped.

**A command the host did not get to.** The host runs one server's commands one
at a time, and its long-running ones share a few slots with other servers'
work, so a command can wait there (`CONTROL_PLANE.md` Section 5, "Expiry"). A
command still waiting when the API's own deadline is about to pass is answered
without being run. A start, a stop, a restart, a console command and a backup
of a running server then fail with `409` `command_expired` instead of
`command_failed`: nothing was applied, so retrying is safe. The lifecycle toast
and the Console line say the host was busy and ask the user to try again; the
Console line does not say the console connection failed, since it was never
opened. A start whose start command expired is set back to stopped as for any
refused start. One whose hydrate expired stays pending on its host, since the
answer does not rule out an instance of the server there, and the system
retries it (`CONTROL_PLANE.md` Section 5). A stop keeps its intent, so the
system replays it on its own as well. The Files
tab keeps its generic failure message, and a backup keeps the Backups tab's
generic one. A stop's final snapshot never fails this way: it runs however long
it waits.

**Storage outages on the working-set routes.** When the store holding a
server's working set cannot serve the request, the routes whose failure is safe
to retry answer **503 `storage_unavailable`** — retry the same request
unchanged:

- every read: `GET …/files` (read and list), `…/files/history`,
  `…/files/version`, `…/files/search`, `…/files/download` and its
  `download-grant`;
- the writes a repeat converges for: `PUT …/files`, `…/files/upload` (archive
  extract included — the repeat rewrites every member) and
  `…/files/directories`. The interrupted attempt may already have applied the
  change (a file can be replaced before its write reports the outage);
  repeating it completes it. Each attempt that got as far as replacing a file
  retained the content it replaced, so a repeat can use up one more slot of
  that file's bounded version history than a first-time success would;
- `POST …/start`: the EULA check, and the `accept_eula` write, both run before
  anything is started.

`DELETE …/files`, `…/files/rename` and `…/files/rollback` are **not** in that
set and answer the generic **500**: interrupted after the mutation itself, a
repeat finds the source already gone (404) or the destination already occupied
(409), so a 503 would promise a retry that cannot finish the job. For a
rollback the thing that can be gone is the version: restoring it retains the
file it replaces, and when the file's version history is full that evicts the
oldest version — possibly the one being rolled back to — so the file may
already hold the restored content while the same rollback again is a 404.
Reload the listing to see what the failed request left. The same holds for `PATCH …/{sid}` when the store
fails while it reads `server.properties`. These statuses describe the object
storage backend, which reports an outage as such; the filesystem backend
reports a device fault on these routes as a 500.

**A lost lifecycle lock.** A route that runs under the server's lifecycle lock
(DATABASE.md Section 7) answers **503 `lifecycle_lock_lost`** when the lock's
database session ended before a write that relied on the lock: it stopped
before that write. Retrying takes the lock afresh. What it wrote before
stopping stands, as after any other failure at that point. A restore that had
already published answers **503 `restore_incomplete`** instead (Section 6.7).
An import whose server already exists, and an update whose row change has
committed, answer 503 `seed_failed`, as for their other failures after that
commit. A loss found only when the operation releases the lock, after its last
write, changes nothing: the operation answers as it finished.

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
`assigned_worker_id`, `crash_reason` and `crash_detail` (the last crash's
reason and the Worker's detail, both null unless the server is crashed;
DATABASE.md Section 7), `crash_not_restarted` (whether the reconciler leaves
that crash alone, false under a stopped intent; CONFIGURATION.md Section 5.6),
`backup_retention` (the scheduled-backup retention policy; null while
unconfigured).

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
| PUT / DELETE | `/workers/{wid}/drain` `[A]` | Set / clear drain. Set MARKS the worker's running servers `desired=stopped` and returns `servers_stopped` (the count marked, not yet stopped); the reconciler then stops each + takes the final snapshot ASYNCHRONOUSLY (on the first reconciler tick once [`reconciler.grace_seconds`](../app/CONFIGURATION.md#56-divergence-reconciler) has elapsed since the drain (or since a later worker report for that server), so 660–720 s after the drain at the stock 660 s grace and 60 s `reconciler.interval_seconds`) and only while the worker stays connected — keep the worker up until convergence. If the worker disconnects first and stays gone, the reconciler releases each server `reconciler.grace_seconds` after the disconnect (the disconnect restarts the wait; after an API restart, no earlier than the API start plus `reconciler.grace_seconds`) without its final snapshot: the snapshot is skipped, not deferred, so a later start on another worker begins from the server's last published snapshot. If the drained worker comes back, is un-drained and is given the server again, the start reuses the scratch that worker kept, when it is at least as new as the store, instead of hydrating. Confirm convergence PER SERVER (each reaching unassigned, at `observed=stopped`, `crashed` for one that crashed, or `unknown` when its worker went away first), NOT by assigned load: drain decrements the load synchronously, so it drops to 0 before any stop runs. An operator stop of such a server skips the wait and stops it at once (`…/{sid}/stop`). Clear only re-enables placement (does not restart them; un-draining before convergence transiently oversubscribes the worker). Drain is persisted: it survives an API restart as well as a worker reconnect, and a drained worker re-registers `draining` until cleared. Set and clear answer `404` for a worker that has not registered with the running API, so a worker that went away before an API restart can no longer be drained; move its servers with a stop and a start instead ([CONFIGURATION.md](../app/CONFIGURATION.md#56-divergence-reconciler) Section 5.6, "A Worker that never reconnects"). |
| GET | `/audit` `[A]` | Global audit (`community`, `operation`, `actor`, `since`, `until`, `limit`, `offset`). |
| GET | `/communities/{cid}/audit` | Community-scoped audit (same filters minus `community`). |
| GET | `/backups/statistics` `[A]` | Global backup statistics. |
| GET | `/healthz` · `/readyz` | Liveness / readiness (ops-facing, not UI-core). The Prometheus exposition is not on this API — it has its own listener. |
| GET | `/meta` | Deployment facts the Web UI reads before a server exists: `{relay_enabled, bedrock_enabled, default_memory_limit_mb, max_memory_limit_mb}`. Requires authentication. Used by the create wizard to decide whether to surface the game-port control (relay mode auto-allocates), and by the plugins tab to decide whether to show the Bedrock/Geyser discovery hint (`bedrock_enabled` = `relay_enabled` AND the deployment's Bedrock capability flag). |

### 2.5 Resource packs

Global resource pack library (not community-scoped) and per-server assignment.

| Method | Path | Notes |
|---|---|---|
| POST | `/resource-packs` | Upload a resource pack (multipart; requires `server:update` in at least one community). An object-store outage answers 503 `storage_unavailable`: the file is stored before the pack row is inserted, so no pack was created and the upload is safe to send again (each attempt uses a fresh id). The failed attempt can leave a stored file no pack references — never served, and reclaimed by the resource-pack GC ([`STORAGE.md`](../app/STORAGE.md) Section 4.3). The filename becomes the last segment of the pack's storage key and of its public URL, so one that cannot be that is a 422 `invalid_filename`, refused before the upload is read into memory, and nothing is stored: a name that is blank, carries a control character (C0, DEL or C1), a lone surrogate or a Unicode noncharacter (U+FFFE, U+FFFF and the other 64, #3922), contains `/` or `\`, or is over 255 bytes in UTF-8. Any other Unicode name is accepted, an unassigned code point included. A name not ending `.zip` is a 422 `filename_not_zip`. Packs stored before the filename check keep their names, and list, download and GC as before. The `display_name` is stored stripped; stripped, one that is empty, over 200 characters, or carries a control character or a lone surrogate is a 422 `invalid_display_name`, refused before the upload is read into memory (#3922). |
| GET | `/resource-packs` | List all resource packs (authenticated). |
| DELETE | `/resource-packs/{id}` | Delete a resource pack (uploader or platform admin; 409 when still assigned to a server). The pack row is deleted first and the stored file's removal is best-effort, so an object-store outage still answers 204 — the pack is gone and a repeat is a 404 — and leaves an orphaned file that is logged, never served, and reclaimed by the resource-pack GC ([`STORAGE.md`](../app/STORAGE.md) Section 4.3). |
| GET / HEAD | `/resource-packs/{id}/download` | Download (authenticated). The response declares `Cache-Control: no-store`. `HEAD` is the metadata probe: the same gate and the same headers with no body, so a client learns the `Content-Length` without starting a transfer; it never opens the blob nor records a `resource_pack:download` audit event. |
| GET / HEAD | `/public/resource-packs/{id}/{filename}` | Public download (no auth) — the URL Minecraft clients fetch. Validates `filename` matches the stored name. `filename` is the rest of the path, so a name stored with a `/` before the upload refused one matches too, escaped as `%2F` or not (#3922); it is only compared with the stored name, so a path shaped like a traversal names nothing. The two statuses declare different caching policies, because the URL ends in the stored filename and an undeclared policy is decided by the edge's extension heuristic instead: the `200` declares `Cache-Control: public, max-age=3600, immutable` — a pack is immutable and the game client verifies it against `resource-pack-sha1`, so the max-age bounds only how long a deleted pack stays fetchable from a cache — and the `404` declares `Cache-Control: no-store`, since a pack's id and filename are both fixed at creation and a URL that 404s can never later become a `200`. `HEAD` is the metadata probe: this is the unauthenticated URL a resumable-download client probes before a transfer, and it declares a `Content-Length`, so it has a real reason to. The probe answers each status with the `GET`'s headers — the same `Cache-Control` per status — and no body, so an edge does not cache a probe differently from the download; it never opens the blob. |
| POST | `…/{sid}/resource-pack` | Assign a resource pack to a server (`server:update`). Body: `{resource_pack_id, require_resource_pack, resource_pack_prompt}`. A storage failure writing `server.properties` answers 503 `seed_failed`: the assignment is not committed, so assigning again is safe. The file is not rolled back with it and may already carry the new pack keys. A storage outage while **reading** `server.properties` answers 503 `storage_unavailable`; that read precedes the assignment, so nothing changed. |
| DELETE | `…/{sid}/resource-pack` | Unassign (`server:update`). A storage outage reading or rewriting `server.properties` answers 503 `storage_unavailable`: both precede the removal of the assignment, so it still stands and unassigning again is safe. The file may already have had its pack keys cleared. |
| GET | `…/{sid}/resource-pack` | Get the current assignment (`server:read`). |

### 2.6 Real-time (WebSocket)

| Path | Notes |
|---|---|
| `WS /communities/{cid}/servers/{sid}/events?streams=status,log,metrics,notification` | Typed frames `{stream, ts, payload}`. `status`: `{state, detail, reason}` for an observed transition (`reason`: the crash reason the Worker classified — `forge_install_failed` / `forge_install_out_of_memory` / `forge_install_java_incompatible` — or `""`; CONTROL_PLANE.md Section 6), `{desired_state}` for a change of the server's intent, or `{assigned_worker_id}` for a placement or release, each carried by the frame of the write that changed it (see below) · `log`: `{line, stream}` · `metrics`: `{cpu_millis, memory_bytes, player_count}` · `notification`: `{kind, title, detail}` (operator notice) · `gap`: client fell behind (always delivered) · `snapshot`: `{state, desired_state, assigned_worker_id, crash_reason, crash_detail, crash_not_restarted}` (see below). |
| `WS /communities/{cid}/events` | Community-wide **status + notification** firehose; frames carry `server_id`. `snapshot`: `{servers: [{server_id, state, desired_state, assigned_worker_id, crash_reason, crash_detail, crash_not_restarted}]}` with `server_id: null` (see below). |

Missed frames are never replayed (delivery is best-effort), so a connection
that carries status opens with a **`snapshot`** frame and gets a fresh one
right after a `gap` frame when status frames were dropped since the previous
such check (a gap that dropped only other streams cost no status and is not
followed by one; neither is a gap whose status losses happened while the
previous gap was being delivered, since that gap's snapshot already covers
them): the persisted observed and desired states, the assigned Worker
(`assigned_worker_id`, `null` when none) and the last crash (`crash_reason` and
`crash_detail`, both `null` unless the server is crashed, and
`crash_not_restarted`) of the stream's scope, read together from each server's row — the server for the per-server stream (only when `status` is among the
subscribed streams), every server of the community for the community stream.
Its `ts` is the time the snapshot was read. Its `crash_reason` and `crash_detail`
are the stored `reason` and `detail` of the crashed `status` frame. The live
`detail` of any other state is not stored, so the snapshot has no counterpart
for it. No status frame carries `crash_not_restarted`. A client derives it as
the API does, except that it checks the running intent where it uses the flag:
a crashed frame sets it from the reason, since the crash is then the newest
write to the row, and a `{desired_state}` frame clears it, since an intent
commit is a newer write. Other writes that bump the row's `updated_at`
publish no frame (CONFIGURATION.md Section 5.6), so after one of them a client
keeps the old value until its next read or snapshot. A client applies it like status frames, and
on the community stream it is the complete server set, so a server missing from
the client's list (or absent from the snapshot) means the set changed. A
server deleted before its per-server snapshot can be read closes the socket
with 4404.

Every change of a server's observed state publishes a `status` frame, after
the write that made it commits: a Worker-reported transition, and equally the
states the API records on its own — `unknown` for each server of a Worker whose
session drops (except a crashed one, which keeps its state) and for each
in-flight server at API startup, and the state a
lifecycle operation converges from a command's outcome (CONTROL_PLANE.md
Section 6). Those API-originated frames have an empty `detail` and `reason`. A
write that leaves the state as it was publishes nothing.

Every change of a server's desired state publishes a `status` frame
`{desired_state}` too, after the write that made it commits: a start, stop or
drain from any session, a start the API reverts after its dispatch failed,
and the reconciler's own starts and stops, which go through the same
operations. The frame carries no `state`: an intent change is not an
observation, and a client keeps the observed state it holds.

Every change of a server's assignment publishes a `status` frame carrying
`assigned_worker_id` too — the Worker's id, or `null` once released — after
the write that made it commits: a start's placement and a failed start's
revert (in the frame of their `desired_state`), the reconciler's placement of a
desired-running server and its undo when the launch never reached the Worker,
and every release of a stopped server: after its final snapshot, on a late
final-snapshot result, and the reconciler's recovery of a stop left holding
its Worker. A release changes nothing else, so its frame is
`{assigned_worker_id: null}` alone, except where the same write converges the
observed state (a stop left `stopping`, released as `unknown`). Deleting a
server changes no assignment: the API refuses it while a Worker is assigned.
A write that changes neither the intent nor the assignment (a restart)
publishes nothing.

A client applies each `status` frame by setting the fields it carries, so
another tab's dashboard offers the actions of the current intent without a
reload (#3428), and gates Start and Stop on the current assignment (#3818).

Ordering: the subscription is registered before the snapshot is read, so every
status transition published from then on reaches the subscriber's buffer.
Right before each snapshot read, the status frames still buffered are
discarded: each was published, hence committed, before the read, so the
snapshot already shows it or a newer state, and delivering one after the
snapshot could only roll the client back. A status frame published after the
discard is delivered after the snapshot, in order; one that raced the read may
repeat what the snapshot showed, which is harmless because status frames are
idempotent sets. It may also be older than the snapshot — published while the
read was in flight, then overtaken by a write the read saw. When the two writes
happen one after the other — a Worker reports `running`, then its session
drops — the newer write publishes its own frame after the older one, so the
client still ends on the newer state.

Residual (#3307): that last step holds for sequential writes only. Each write
publishes when its own commit returns, so two writes of one server's state or
assignment made on different database connections publish in the order their
commit acknowledgements reach the API's event loop, which can differ from the order
the database committed them in. The frame of the earlier commit can then be
published last, and a client is left on that older value until the next frame
that carries the same field for the server (a status frame for the observed
state, an intent frame for the desired state, an assignment frame for the
Worker), the snapshot that follows a reconnect or a `gap` that dropped status,
or a REST refetch that starts after the stale frame. Closing this needs
per-server sequencing of status frames, tracked separately in #3307.

Auth: browsers pass the access token via `Sec-WebSocket-Protocol` as two
subprotocols `["access_token", "<jwt>"]`; the server echoes `access_token` as
the accepted subprotocol (RFC 6455). The `Authorization: Bearer` header is also
honored for non-browser clients. Close codes mirror REST: 4400 bad `streams`,
4401 unauthenticated, 4403 forbidden, 4404 not found / not a member.
A database read the socket cannot get answered — a transient database error
(refused or lost connection, exhausted pool, server starting up or shutting
down), or no answer in time (a frozen database) — is none of those: the
socket is closed with the standard **1013** (Try Again Later) and the API logs
one WARNING line, no traceback. At the handshake the socket is refused with it
before accept, like the denials above, so a browser sees a failed handshake.
The whole handshake, authentication and the gate together, has one 8 s
deadline, re-checked right before accept: a handshake that runs out of time
is refused and never accepted. The deadline is the API's own bound on the
handshake. It is kept below the 10 s open timeout of uvicorn's legacy
`websockets` backend, so the refusal arrives before that backend drops the
connection. The backend the API selects today (websockets-sansio) has no such
server-side open timeout. Mid-stream, with a 10 s bound on each read, the
same applies to the
status snapshot, the community stream's per-server membership lookup and the
re-check below. Any other error is not masked.
Delivery is best-effort; REST keeps working if the socket dies (FR-MON-4).

Re-check: every 60 s mid-stream the server repeats, for the token the socket
was opened with, what REST does on every request, and closes the socket with
the code the handshake would have used:

- It re-authenticates: the token is verified again and its user loaded again,
  so an account deactivated or deleted since the handshake is closed with
  **4401**, as its next REST call would be answered 401. These are all the
  per-request conditions REST has: an access token is not bound to a session,
  so a logout, a revoked session or a password change ends the refresh token
  only, and the access token (and with it the socket) stays valid until it
  expires, on REST and here alike.
- It re-authorizes the user as just loaded: a member removed (4404), a
  permission or grant revoked (4403), a deleted server (4404).
- It is bounded: a re-check that fails on a transient database error, or has
  no answer within 10 s (a frozen database), closes the socket with **1013**
  rather than leaving the stream silent while it waits. The bound limits the
  wait only: the abandoned query's cleanup, and the pooled connection it
  holds, last until the database answers or the connection's transport fails
  — indefinitely, for a database that stays unreachable.

Lifetime: a socket's only credential is the access token it was opened with,
so it lives no longer than that token. At the instant that token
expires the server closes it with **4419** (token expired); nothing is
delivered after that, however busy the stream. 4419 and the mid-stream 4401
name different causes — the token lapsed, or the re-check no longer accepts it
— and share one remedy: the client refreshes its session before reconnecting
(Section 7.1) instead of retrying with the token it has. A session that is
still good gets a fresh token and reconnects; one that is over is signed out.
The two can swap at the boundary: the re-check verifies the token's expiry
too, so one that runs just as the token lapses can win the race against the
expiry timer and close 4401 instead of 4419, which the shared remedy makes
harmless. The remedy differs only for a rejection at the handshake, which a
browser never observes as a close code at all (a close before accept reaches
it as a failed handshake) and retries on the reconnect backoff, asking REST
after repeated failures whether it is being refused (Section 7.1). A
mid-stream 4403 / 4404 has no remedy to retry: the client stops (Section 7.1).
Consequence: a dashboard left open sees its sockets cut and
re-established once per access-token lifetime; the `snapshot` frame on
reconnect is what keeps that seamless.

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
| POST | `…/{sid}/plugins` | Install a plugin jar via multipart upload (`plugin:manage`): `display_name` form field + `file`, jar ≤ 512 MiB (413 `file_too_large`). Returns `201`; a duplicate is 409 `plugin_already_exists`. A jar filename that cannot name a file is a 422 `invalid_filename`, refused before the upload is read into memory, under the file upload's rule (Section 2.3, #3922). The `display_name` is stored stripped, under the resource-pack upload's rule (#3957): stripped, one that is empty, over 200 characters, or carries a control character or a lone surrogate is a 422 `invalid_display_name`, also refused before the upload is read into memory. The filename is checked first. The Web UI sends the file's name less `.jar` as the display name, so its toast for `invalid_display_name` asks for a file renamed to 1–200 characters before `.jar`. |
| GET | `…/{sid}/plugins/updates` | Batch-check every installed plugin for a newer catalog version (`plugin:read`); catalog upstream failure is 502 `catalog_upstream_failed`. |
| GET | `…/{sid}/plugins/validate` | Phase-B dependency/compatibility checklist — missing deps, unsatisfied version ranges, conflicts, MC-version mismatch (`plugin:read`). Read-only; never mutates the set. |
| POST | `…/{sid}/plugins/resolve` | Plan dependency auto-resolution: the transitive closure of required deps, each classified satisfied / needs-import / unresolvable / blocked (`plugin:read`). Read-only — nothing is downloaded or installed. |
| POST | `…/{sid}/plugins/resolve/apply` | Apply that plan: install each non-blocked needs-import dep from the catalog, then re-plan (`plugin:manage`). Per-dep install failures are isolated in `failed`. |
| GET | `…/{sid}/plugins/{pid}` | Read one installed plugin by id (`plugin:read`). |
| DELETE | `…/{sid}/plugins/{pid}` | Remove an installed plugin (`plugin:manage`). Returns `204`. |
| GET | `…/{sid}/plugins/{pid}/updates` | Check a single plugin for a newer catalog version (`plugin:read`). |
| POST | `…/{sid}/plugins/{pid}/update` | Update a plugin to a specific catalog `version_id` (`plugin:manage`); missing project 404 `catalog_project_not_found`, checksum drift 502 `checksum_mismatch`. The new version's file name is taken as the catalog install takes it, and an over-long one is the same 422 `invalid_filename` (#3957). |
| GET | `…/{sid}/plugins/{pid}/dependencies` | List a Modrinth-sourced plugin's declared dependencies, each flagged installed/missing (`plugin:read`). |
| POST | `…/{sid}/plugins/{pid}/enable` · `/disable` | Toggle a plugin on/off (`plugin:manage`). A jar missing from the cache when the working set needs it is 409 `plugin_jar_missing` over the committed toggle (see **Missing jars** below). |
| POST | `…/{sid}/plugins/{pid}/side` | Override a mod's side — `both` / `server` / `client` (`plugin:manage`); re-materializes the working set. Invalid side is 422 `invalid_side`; a jar missing from the cache when the working set needs it is 409 `plugin_jar_missing` over the committed side (see **Missing jars** below). |
| GET | `…/{sid}/client-mods` | List the server's enabled client-relevant plugins (side `client` / `both`; `plugin:read`). |
| GET / HEAD | `…/{sid}/client-mods/download` | Download those client mods bundled as `mods.zip` (`plugin:read`). The response declares `Cache-Control: no-store` — a per-server body gated by `plugin:read` must never be served from a shared cache. `HEAD` is the metadata probe: the same gate and headers with no body, and it neither builds the zip nor pulls a jar; the zip is streamed with no `Content-Length` (assembled on the fly from a variable jar set), so the probe learns existence rather than size. The `GET` opens the first jar before the response headers: a jar missing from the content-addressed cache answers 409 `plugin_jar_missing` (see **Missing jars** below) and a cache outage 503 `storage_unavailable` (see **Storage outages** below); a later jar missing or unreachable aborts the transfer. |
| GET | `…/{sid}/catalog/search` | Search the Modrinth catalog with auto-applied server facets (`plugin:read`): `q` query + `limit` (1–100, default 20) / `offset` paging. Catalog upstream failure is 502 `catalog_upstream_failed`. |
| GET | `…/{sid}/catalog/projects/{id_or_slug}` | Fetch a catalog project's detail + its server-compatible versions (`plugin:read`); an unknown project is 404 `catalog_project_not_found`, catalog upstream failure 502 `catalog_upstream_failed`. |
| POST | `…/{sid}/catalog/install` | Install a plugin/mod from the catalog by `project_id` + `version_id` (`plugin:manage`). Returns `201`; a missing project is 404 `catalog_project_not_found`, checksum drift 502 `checksum_mismatch`, and a duplicate 409 `plugin_already_exists`. The catalog's file name is installed as its basename, with each character the upload rule refuses (a control character, a lone surrogate or a Unicode noncharacter) replaced by `_`. A name that rule still refuses after that, one over 255 bytes in UTF-8, is a 422 `invalid_filename`, with nothing downloaded or written (#3957). |

**Storage outages.** A plugin's jar is first resolved through the
content-addressed jar cache, then recorded, then written into the working set.
An outage of the cache answers **503 `storage_unavailable`** on the routes that
reach it before anything is recorded — `POST …/plugins`,
`POST …/catalog/install` and `POST …/plugins/{pid}/update` — and on
`GET …/client-mods/download`, which begins its stream before the response
headers (an outage on a later jar still aborts the transfer). Nothing was
installed or changed, so the request is safe to send again. An outage that
strikes the working-set step **after** the plugin was recorded is not a 503 on
any of these routes, nor on remove / enable / disable / side: the record is
already committed, a repeat would answer 409 or 404 instead of finishing, and
the routes answer the generic 500. The plugin list then shows the recorded
state; the jar in the working set may not match it.

**Missing jars.** A plugin row can outlive its jar in the content-addressed
cache (a garbage-collection race or an external deletion). Wherever a route
needs that jar it answers **409 `plugin_jar_missing`**: the client-mods
download (for its first jar, before the headers — nothing changes), and enable /
disable / side when the working set has no file for the plugin and has to be
rebuilt from the cache. Those three commit the new state first, so it stands:
the plugin list shows it, the working set holds no file for the plugin, and
repeating the request is a no-op. The committed change is audited as the
operation's success, and the tab refetches its list after the 409 so the row
shows that state. Removing the plugin and installing it again
(upload or catalog install) restores its jar.

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
- Quick actions on card: start / stop / restart (permission-scoped). Several
  rows can fail at once, so each action's toast names its server (7.4).
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
   step 1 → upload form (`POST …/servers/import`). An import answering 503
   `upload_busy` (too many archive uploads being checked at once, #3912)
   created nothing; its message asks the user to try again shortly.

### 6.4 Server detail — Overview
- Header: name, state pill (+ `detail` from last status event, e.g. crash
  category), desired-vs-observed mismatch hint ("applying…" while the
  reconciler converges; not shown for a crash it does not restart, see the
  crash banner), worker id, port. A stopped intent is reached at rest
  (observed stopped, crashed or unknown) once no Worker is assigned: while one
  still is, the stop is still finishing (its final snapshot and the release,
  or for `unknown` the stop itself) and the hint shows. Every release
  publishes a status frame (Section 2.6), so the hint clears with it. The
  dashboard rows mark drift by the same rule.
- Crash banner: while crashed, the crash's detail, a guidance line with a link
  to the Console tab, and — keyed on the crash's reason — the Forge install
  diagnostics. Both come from the server's stored last crash (`crash_detail`,
  `crash_reason`: the server read, the snapshot, and the `detail` and `reason`
  of a crashed status frame), so a page loaded after the crash, or remounted
  by a language switch, shows the same banner. The guidance line follows the
  desired state. Under a running intent the API refuses start and restart (the
  Worker holds no live instance) and the reconciler restarts the server on its
  own, so the line says the system will restart it automatically and that the
  operator can stop it instead; it names no delay. While the server reports
  `crash_not_restarted` (a `forge_install_out_of_memory` or
  `forge_install_java_incompatible` crash newer than every write to the row,
  which the reconciler does not restart because the next attempt fails the same
  way, CONFIGURATION.md Section 5.6), the line says instead that the server will
  not be restarted automatically, and to stop it, fix the cause and start it
  again. After that start the line returns to the automatic restart until the
  Worker reports the new launch. Under a stopped intent the line points to the
  Start button, which reads **Restart** for a crashed server.
  `forge_install_out_of_memory` and `forge_install_java_incompatible` each add
  a specific, localised explanation of what to change. Any `forge_install_*` reason adds a **View install log**
  toggle (hidden without `file:read`) that reads `logs/forge-install.log`
  through the files API on demand and shows its last 200 lines, opened at the end. The files API
  serves a server only at rest or running (REQUIREMENTS.md Section 6.9), and a server that crashed
  under a running intent is neither, so until it is stopped the viewer says to
  stop it first (409 `server_unsettled`); a log that was never written (404)
  and one past the read cap (413) get their own line too. A refetch that fails
  once the log is shown keeps that log on screen, where the operator scrolled
  it, under a line giving the failure and saying the log is the copy loaded
  earlier.
- Controls: Start / Stop (dropdown: graceful · force) / Restart / Export /
  Delete — each disabled by state machine (e.g. Start hidden under a running
  intent) and permission. A server crashed under a running intent offers Stop
  only: Start and Restart are hidden, since the API refuses both there. Under a
  stopped intent Stop is offered exactly while a Worker is still assigned — a
  drained server still running, or a stop left half-done — since the API then
  converges it at once, and refuses it with no Worker assigned. Under a running
  intent with no Worker assigned — a start the reconciler has yet to place —
  Stop is offered while the server reads stopped, crashed or unknown, since the
  API then withdraws the intent with no Worker to tell. It is not offered while
  the server reads running: the stopped intent would keep reading running with
  no Worker to report otherwise, which nothing converges. Start follows the
  API's start guard exactly, which reads the intent and the assignment and
  never the observed state. It is hidden under a running intent
  (`409 invalid_transition`), and offered under a stopped intent with no Worker
  assigned whatever the server reads. A server that reads starting, running,
  stopping or restarting there is one no Worker reports on any more and nothing
  converges, so Start is its way out. That reading does not say whether the
  server still runs: if it is in fact live on a Worker the row no longer names,
  the start can place a second instance on another Worker (issue #3740). A stop
  holds its Worker until the final snapshot settles, and the API refuses a
  start until the release (`409 transition_conflict`). So under a stopped
  intent with a Worker still assigned, whatever the server reads (stopping,
  still running in a drain's grace window, or at rest), Start shows disabled
  with "Waiting for the stop to finish and release the server…" (on a dashboard
  card, as the button's tooltip) until the release frame frees it. The reason
  holds whether the release is pending on a final snapshot, a re-sent stop, or
  a Worker that has gone offline. `webui/src/pages/startGuard.json` lists the
  API's answer for every (desired, observed, assigned) row, and the Web UI and
  the API's start route are both tested against it. A lifecycle request that
  succeeds holds the controls until the settle refetch answers, since until
  then the cache pairs the requested transition with the intent from before it.
  A refetch that fails, or is paused while the browser is offline or the tab is
  in the background, ends the hold too, and until the next read Start can show
  as offered; the API refuses that start (`409 invalid_transition`). Restart
  needs a running intent and an assigned Worker: it is hidden under a stopped
  intent even while the process still runs (a drain's grace window), and
  whenever no Worker is assigned, since the API refuses it in both cases
  (`409 invalid_transition`). The dashboard card quick actions follow the same
  rules and disable them.
- Delete (the Settings tab's danger zone, 6.9) needs no Worker assigned, for
  the same reason as Start: the API refuses a delete until the release
  (`409 server_assigned`). While a Worker is assigned, Delete shows disabled
  with "Stop the server and wait for it to be released before deleting it."
  beside it, and the release frame frees it. A `409 server_assigned` that a
  stale cache let through toasts the same words.
- Metrics strip: CPU / memory / players from the `metrics` stream (sparkline,
  last N samples, client-side only).
- Log tail: last ~200 lines, link to Console tab.
- Single `WS …/{sid}/events` connection shared by all tabs of this page;
  `gap` frames render as an inline "missed events" divider.

### 6.5 Server detail — Console
- Full log stream (stdout/stderr color-keyed), follow-mode toggle, text
  filter, clear.
- RCON input (`POST …/command`) with local history (↑/↓); command + `output`
  echoed into the stream view, distinct styling. A command past 1,446 UTF-8
  bytes disables Send and shows the limit beside the input (Section 2.3,
  "Console lines").
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
  | 422 | `invalid_filename` | An upload's file name cannot name a file (blank, a control character, `/` or `\`, or too long); nothing was written, and renaming the file is the remedy (#3922). |
  | 422 | `reserved_name` | A mutation's destination is, or lies under, a root name starting `.mcsd_generation`, the Worker's generation marker (Section 2.3). |
  | 422 | `invalid_archive` | The archive reader failed on an extract upload (not an archive, cut short, damage it detects, an encrypted member); nothing was written. A tar.gz is read to its gzip end, so a header that does not read, data past its end-of-archive marker and a bad gzip trailer are caught too (#3778). |
  | 413 | `file_too_large` | Read result or edit payload exceeds the file size cap. |
  | 503 | `worker_unavailable` | The Worker hosting a running server is not connected or did not answer in time. |
  | 503 | `storage_unavailable` | The store holding the server's files did not answer (Section 2.3). Shown as its own message, distinct from the Worker one: retrying is the remedy, and nothing about the server's Worker is wrong. |
  | 503 | `upload_busy` | Too many archive uploads (extracts here, backup uploads on the Backups tab, server imports) were being checked at once, so the extract was refused with nothing written (#3843). Shown as its own message: sending it again shortly is the remedy, and nothing about the server's Worker is wrong. |

### 6.7 Server detail — Backups
- Stats header (count, total size, newest/oldest).
- Table: created_at, source (`manual` / `scheduled` / `event` / `uploaded`), size, health (`healthy` / `quarantined` / `unreadable` / `unknown`), creator;
  actions: download, restore, delete.
- Restore: blocked with explanation while running (offer "stop now then
  restore" two-step); typed-confirm dialog. A `quarantined` backup requires a
  second force-acknowledge confirmation before the `?force=true` restore is
  issued. An `unreadable` backup (its archive could not be read back from
  storage) cannot be restored at all: its restore action is disabled with a
  hover explanation, and its condition badge names the storage-side remedy
  rather than the damaged-world one.
  A restore answering 503 `seed_failed`, `plugin_reconcile_incomplete`,
  `publish_incomplete` (the store failed after it published, #3564) or
  `restore_incomplete` (the lifecycle lock was lost after the publish, #3927)
  published the backup and then failed or skipped a follow-up step, so its
  message says the backup was restored and asks the user to retry the restore.
  A restore answering 503 `publish_unknown` (the store could not confirm whether
  it published, #3564) says the backup may have been restored and asks the user
  to retry.
- Create backup button (works on running servers — on-demand snapshot path);
  upload backup (file picker). A create answering 409 `working_set_too_large`
  (the server's files would make an archive past the 8 GiB backup cap, #3859)
  wrote nothing; its message names the limit and asks the user to make the
  server's files smaller, since retrying as they are fails the same way. An
  upload answering 503 `upload_busy` (too many archive uploads being checked
  at once, #3843) was not stored; its message asks the user to try again
  shortly rather than naming a server host.
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
- Danger zone: delete server (typed confirm; held while a Worker is assigned,
  6.4), export ZIP.

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
- **General**: rename; delete (typed confirm; admin/owner only). A delete
  refused with 409 `community_has_servers` shows "delete this community's
  servers first" rather than the generic error (the admin Communities page
  does the same).

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
  message}` rows) only for `stop`/`restart`; a "back up only while the server
  is running" checkbox only for `backup` (checked by default; prefilled from
  `only_when_running` on edit). API validation failures map to
  inline field errors via their typed 422 reasons (`invalid_cron`,
  `invalid_cadence`, `invalid_timezone`, `invalid_schedule_name`,
  `invalid_payload`) and the 409 duplicate name (`schedule_name_exists`).
- Delete: confirm dialog (run history cascades away with the schedule).
- Run history: per-schedule dialog, newest first — outcome badge
  (`success`/`failure`/`skipped`/`unknown`), sanitized detail, started/finished
  timestamps. Capped at 50 rows per schedule server-side. An `unknown` run is an
  action whose result the host did not confirm (issues #3660, #3781): it gets
  its own amber badge, and in place of the detail the row says the action may
  have run or may still take effect, so it is not read as a failure.
- Permission gating (7.3): the tab body needs `schedule:read`; writes are
  two-layer — `schedule:manage` **and** the action's own permission gates the
  create button's action options and each row's edit/toggle/delete
  (anti-escalation, write-time only). A `schedule:read`-only member gets a
  read-only view (run history stays available).
- Failure surfacing: `schedule_failed` NOTIFICATION frames from the community
  events socket (7.2) render as error toasts (title + sanitized detail) while
  the dashboard is open. Each names its server and collapses only with the
  same notice about that server, as lifecycle toasts do (7.4); a notice about
  a server the dashboard's list does not hold yet shows unnamed, still apart
  per server. An `unknown` run raises the same frame, titled
  "Scheduled <action>: outcome unknown" rather than "failed".

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
  single-flight refresh mutex; hard logout on refresh failure. A
  `413 credential_required_for_body` (Section 2.3, "A body sent without a
  credential") is the 401 of a large body and is treated the same way: one
  refresh, then the same request — the JSON string or `FormData` it sent — once
  more. Before it sends a body past 64 KiB, the client refreshes first if the
  access token has expired or expires within 30 seconds (read from the token's
  `exp` claim, unverified), so a large upload is not refused while it is still
  being sent; the retry stays as the backstop. Page reload
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
  to the caller so the user can retry. Logout and sign-in each start a new
  session epoch: a refresh or bootstrap result that crosses that boundary is
  discarded wherever it would take effect — it stores no token, sets no session
  status, and a rejection does not log out the newer session — a 401 retry
  (REST, upload, download) happens only while the session the request was sent
  under is still current, and the single-flight mutex is scoped to one epoch.
  This is what the webui's JS guarantees: a stale response's access token is
  never adopted, a stale rejection never logs the newer session out, and a
  stale 401 is never retried as the newer user. The httpOnly refresh cookie a
  late response sets is applied by the browser and is outside the webui's
  control; the API makes it harmless instead: logout revokes the session's whole
  rotation chain, so a successor delivered after logout is already dead and the
  newer session is at worst signed out, never authenticated as the previous user
  (AUTH_API.md Section 4).
- WS connections carry the access token in the `Sec-WebSocket-Protocol`
  subprotocol header (`["access_token", "<jwt>"]`); on token
  rotation, sockets are reconnected (reconnect-on-rotate chosen). A socket
  closed with 4419 (its token expired, Section 2.6) or 4401 (its token is no
  longer accepted mid-stream, Section 2.6) is not retried with that token: the
  client refreshes through the same single-flight refresh the 401 retry uses,
  and the rotation reconnects it. A transient refresh failure retries the
  refresh on the reconnect backoff; an auth-definitive one hard-logs-out,
  exactly as for REST — which is how a deactivated or deleted user with an
  open dashboard ends up signed out rather than reconnecting forever.
  The rest of the WS close policy:
  - **4403 / 4404 mid-stream** (a grant or permission revoked, the member
    removed, the server or community deleted): not an auth problem, so no
    refresh, and every reconnect would be refused at the handshake. The
    client stops reconnecting for good and resets the page's resource query —
    the server detail, or the dashboard's servers list (refetching the
    communities list too, which drops a community the user left) — so the
    page shows its own no-access / not-found state instead of a cached copy.
  - **1013** (Try Again Later: the API's database could not answer, Section
    2.6) and any other close: an ordinary backoff reconnect with the same token.
  - **Repeated failed handshakes.** A handshake the API refuses reaches the
    browser as a bare 1006, the same as a network failure. After 3 consecutive
    handshakes that fail before opening, the client makes one authenticated
    REST read gated like the socket, through the shared API client: the
    server read (`GET /communities/{cid}/servers/{sid}`) for the per-server
    stream, the caller's permissions (`GET /communities/{cid}/me/permissions`,
    membership-gated) for the community stream. The permissions read answers
    200 to any member, so the client applies the community stream's gate to
    it: community-level `server:read`, i.e. the code in `permissions` (the
    member's roles); a per-server grant does not count, exactly as in the
    API's check (Section 7.3 resolution with no resource). A member without it
    is treated like a 403. A 401 goes through the
    transparent refresh like any REST 401 — a rotated token reconnects the
    socket, an auth-definitive failure hard-logs-out; a 403 / 404 stops the
    client as above; success, a network error or a 5xx keeps the backoff. The
    count restarts after each probe and on every successful open, so a long
    outage costs one REST read per three reconnect attempts.
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
- Converge from the `snapshot` frame (Section 2.6) on every (re)connect and
  after every `gap` that dropped status; the one-shot REST refetch on
  reopen / `gap` stays as a belt-and-suspenders reconcile.
- A live state (status or snapshot frame) outlives the REST reads of the query
  it patches: received before the query has loaded, or while a read is in
  flight, it is re-applied when the response lands, unless the request of the
  attempt that produced the response (retries included) started after the
  frame arrived — then the response is the newer truth (it can carry a change
  no frame announced). The observed and the desired state are ordered against
  a read separately, as different frames carry them. The dashboard reconciles
  the server set the same way:
  a live server missing from the landed list, or a listed server missing from
  a newer snapshot, triggers one list refetch.

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
  (7.3). A lifecycle 503 `worker_unavailable` may carry a `worker_state`
  member (below). The client branches on exactly this shape — one body, one
  machine code, on every endpoint. The auth endpoints' reason codes and the
  `permission` and `worker_state` members are enumerated in
  [`AUTH_API.md`](../app/AUTH_API.md) Section 2.
- API error surfaced via toast + inline field errors (422 `errors` list).
- **Toast timing.** An error toast stays until it is dismissed, since many
  errors name a remedy to act on. Any other toast closes on its own after 2 s
  plus 100 ms per character of its message, clamped to 3.2–12 s. It counts
  characters, not words, because Japanese has no word breaks; about 10
  characters a second suits Japanese and leaves English time to spare. The
  pointer or keyboard focus anywhere in the toast stack pauses every
  countdown, so no toast closes or slides out from under the one being read;
  each resumes with the time it had left. Keyboard focus is focus the browser
  shows (`:focus-visible`): the focus a click or a tap leaves on a dismiss
  button shows none and does not pause the stack, and a pointer dismiss sends
  focus back out of the stack (Toast focus, below), so the other toasts keep
  counting down. On a touch screen only a finger held on the stack pauses it,
  since a touch pointer leaves when the finger lifts.
- **Toast accessibility.** An error toast is a `role="alert"`, any other a
  `role="status"`, with no explicit `aria-live`: the role already sets it, and
  repeating it double-announces in VoiceOver. The dismiss button is in the tab
  order and at least 24×24 px, and Escape dismisses the toast whose button has
  focus. The button is described by its toast's message, so it reads as more
  than "Close", the name the modal's backdrop shares.
- **Modal focus.** While a modal is open, Tab keeps focus in the topmost
  dialog and the toast stack. A Tab that lands focus anywhere else, such as
  the page behind or a dialog below, is pulled back by the topmost dialog to
  the dialog's first control, or its last after Shift+Tab, or to the dialog
  itself when it has none; the pulled-to control may scroll into view inside
  the dialog, and the page's scroll that Tab made is undone (a scroller of its
  own on the page is not). So Tab never walks the page behind. A Tab counts
  only until it has moved focus: its first focus move uses it up, and its
  keyup or a 0 ms timer ends it when it moves none. Focus that a script or the
  pointer moves outside after that is left there, even while the Tab's key is
  still down, so a confirm can close its dialog and focus the control behind
  it in any order; the next Tab from there enters the dialog at its first
  control, or its last after Shift+Tab. Tab from the page body is the
  browser's: after a focused control is removed it goes on from where that
  control was, and after a click on a toast's text it goes to that toast's
  dismiss button. A control disabled while it holds focus, as a confirm button
  is while its action runs, hands focus to the dialog itself without
  scrolling, and focus does not go back when the control is enabled again.
  Only that loss moves focus, so focus moves at most once per confirm, not on
  every busy toggle, and a screen reader is not given the dialog to announce
  anew each time. A scroller the browser stops Tab on counts as a control at
  its place: Chromium does so while its content overflows and nothing in it
  takes Tab, as with a `.modal-body` whose controls are all disabled, or a
  list nested in it that scrolls on its own. The trap looks for one only in a
  `.modal-body` with no control in it. A browser that does not stop Tab on
  such a scroller runs past it, as Chromium does past a disabled scrolling
  textarea; a Tab that runs out of the dialog that way goes on from the
  scroller in its direction, to the scroller itself if it takes focus, else
  to the next stop that does, a toast included, rather than back to the
  dialog's start or end.
- **Keys behind a modal.** While a modal is open, the page behind it acts on
  no key: the Files tab's shortcuts do nothing, and a menu or popover outside
  the topmost dialog leaves Escape to the dialog. So a key never acts on a
  selection or a menu the dialog hides. The dialog hears a key after every
  listener on the page, so the dialog is still open when the Files tab sees
  it, and it marks the Escape that closes it as handled. The Files tab's
  Escape clears the selection only when nothing has handled it, so an Escape
  the stop menu handled, with focus in the menu or outside it, keeps the
  selection even when the menu has closed before the tab sees the key. Inside
  the topmost dialog, Escape closes only the innermost layer: a popover open
  in it closes and marks the Escape handled, and the dialog leaves a handled
  Escape alone, so the next Escape closes the dialog.
- **Keys behind a menu.** While a page menu is open (the Files tab's context
  menu, the stop menu), the Files tab's shortcuts do nothing, so a key never
  acts on the listing behind the menu. The menu's own keys still work.
- **Toast focus.** While a modal is open, its focus trap runs on through the
  toast stack, so an error raised over it is reachable by keyboard: Tab past
  the dialog's last control reaches the first toast, Tab past the last toast
  wraps to the dialog's first control, and Shift+Tab runs the same cycle
  backwards. The stack stays outside the dialog, so a toast is never moved and
  announced again. Escape on a toast dismisses only that toast; Escape anywhere
  else in the modal closes a popover open in it first, then the modal. When a
  toast holding keyboard focus goes, dismissed, timed out or dropped by the
  stack cap, focus moves to the next toast's dismiss button, or the one before
  when it was the last. With no toast left, focus returns to the element it
  came into the stack from, unless that is behind an open modal; failing that,
  to the open modal, or else to the page's `main` region. A toast that goes
  holding the focus a click or a tap left skips the other toasts and sends
  focus straight back the same way, without scrolling the page, so a key
  pressed next cannot dismiss another toast unread. Focus never drops to the
  page body while any of these exists.
- **Toast stack.** At most three errors and two timed toasts show at once. A
  new toast past that drops the oldest of its own kind, so a timed toast never
  pushes out an error, nor an error a timed toast. An error raised again while
  its toast still shows, about the same server when it is about one, does not
  stack a copy: that toast shows a count badge and counts as the newest for
  the drop order. A toast is at most 32rem wide, never wider than the screen
  less 2rem, and its message wraps anywhere; the stack is at most the
  screen's height less 2rem and scrolls beyond that, so every toast stays
  reachable on a phone. A toast raised, or raised again, scrolls into view,
  unless keyboard focus is in the stack.
- **Lifecycle toasts.** A toast from a start, stop or restart names the
  server it is about, as "alpha: <message>", with the 403 denial among them:
  the dashboard rows can act on several servers at once, and the detail
  page's controls and the restore dialog's stop read the same. Such a toast
  collapses only into one about the same server, told apart by the server's
  id, since two servers may share a name. The dashboard's schedule-failure
  notices follow the same rule (6.13).
- A failed read retries only what can clear on its own — a network failure, a
  5xx, 408 or 429 — up to three times; any other 4xx is the API's answer and is
  surfaced at once instead of after the backoff.
- Conflict-flavored errors get a "state changed — refresh" treatment, not a raw
  error dump: the lifecycle races `invalid_transition` (except on **start**,
  where it means the server is already desired-running — a pending start, not a
  race — and gets a verb-specific message below), `transition_conflict`
  and `server_not_running` (the last only away from **restart**, which offers
  the action for a server observed unknown under a running intent on purpose
  and so gets a verb-specific message below). A 409 that reports something
  other than a race is named instead, since refreshing is not the remedy: `server_unsettled` says the
  server must be stopped, on every surface that can receive it;
  `worker_busy` / `server_busy` say another operation on the server is still
  running and the request was refused without being applied, so the operator
  retries in a moment; `server_assigned` on a delete says to stop the server
  and wait for it to be released before deleting it, since the hold can outlast
  a retry (6.4); the sanitized start/restart-failure categories
  `port_conflict` / `image_missing` name the cause the Worker classified, and
  `server_properties_unreadable` says the server's `server.properties` is not a
  regular file or is too large and must be restored (Section 2, "A refused
  `server.properties`");
  `command_expired` says the host was busy with other work and did not get to
  the action in time, so it was not applied and the operator tries again
  (Section 2, "A command the host did not get to"). It is the same message on
  every verb, because what it reports holds on each: the host never ran that
  command, so nothing was half-done and retrying is safe. A restart leaves
  nothing pending, so the restart message below would be wrong, and so does a
  start whose start command expired. A start whose hydrate expired stays
  pending on its Worker for the reconciler, since the answer does not rule out
  an instance there, so a retry answers `invalid_transition`
  ([`CONTROL_PLANE.md`](../app/CONTROL_PLANE.md) Section 5). A stop
  keeps its stop intent, and the system replays the stop on its own after a
  short wait, even though the host reports the server still running at about the
  same time. The wait is the full reconcile wait (about 11 minutes by default)
  instead if the API restarts in between, a newer intent commit lands, or the
  host then reports the server in another state. The message still asks for a
  retry rather than taking the stop message below, since trying again replays
  the stop at once;
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
  server observed unknown under a running intent, so this refusal is
  not the race it is on the command surface, and what it leaves behind is the
  same `desired_state=running` the reconciler acts on. A crash that lands just
  before a restart of a running server gets the same answer. The controls no
  longer offer restart for a server observed crashed under a running intent,
  since the API always refuses it there.
- The same verb-specific treatment extends to the **503 `worker_unavailable`** —
  the API's rendering of a dispatch to a Worker that is not connected or lost
  its session, of one that timed out, and of a Worker that answered that its
  container runtime did not (a start or a restart's relaunch whose container
  calls timed out, or a container a failed start left behind that it could not
  end) — where the generic "wait a moment
  and try again" invites a retry of an intent that already stands. On stop it is unambiguous: `StopServer` can only
  raise it from a dispatch, which runs *after* `desired_state=stopped`
  is committed and the placement load decremented, and nothing is compensated —
  the stop itself, or, converging a server already desired-stopped, the final
  snapshot that precedes releasing its Worker. On
  restart `desired_state=running` stands, exactly as for `command_failed`. The
  wording is deliberately *not* the pending pair above: a refusal was reported by
  a host that answered, so those messages can say what the server did, while a
  timeout answers nothing — a graceful stop merely outliving the API's dispatch
  deadline is the commonest case, and it usually succeeds — so the 503 messages
  say the outcome is **unconfirmed** and the intent stands. The restart message
  promises a restart only as long as the host stays connected: the reconciler
  starts a server that stays down only on a connected Worker. `no_eligible_worker`
  and `jar_unavailable` stay verb-agnostic: both are raised before any intent is
  committed.
- **A Worker the API names `not_connected`** (the `worker_state` member of a
  lifecycle 503, [`AUTH_API.md`](../app/AUTH_API.md) Section 2) gets its own
  stop and restart messages. The unconfirmed ones fit a Worker that is
  connected and may still finish the command. A Worker that is not connected
  gets no automatic recovery: the reconciler skips its servers until it
  returns, and nothing moves them on its own
  ([CONFIGURATION.md](../app/CONFIGURATION.md#56-divergence-reconciler)
  Section 5.6, "A Worker that never reconnects"). The stop message says the
  host is offline and the stop still stands: the server is released from the
  host after a while, and stopped there only if the host comes back before
  then. A host back after the release keeps its instance, and nothing stops
  it. A second stop releases the server at once, and a start then moves it to
  another host. The restart message says the system cannot restart the server
  and the restart is not sent again, so the operator restarts it once the host
  is back, or moves it: stop, stop again, start. Both say to move it only once
  the old host is known to be down: the release stops nothing there, and
  nothing fences an instance still alive on it, so the same world could run on
  two hosts. The messages carry these steps and that precondition themselves
  rather than pointing to that section, since not every member can read the
  repository docs. `no_answer`, and a 503 with no member (the Worker's own
  UNAVAILABLE answer), keep the unconfirmed messages. Start keeps its
  verb-agnostic message whatever the member says (next bullet).
- On **start**, `worker_busy` gets its own message, but a *hedged* one, because
  the reason is ambiguous at the edge. A **post-dispatch** `worker_busy`
  keeps `desired_state=running` and the assignment so `redispatch_start` can
  converge once the raced command settles — the start is pending; a
  **pre-dispatch** one — a refused hydrate before the start command was sent —
  keeps them too, since another command or a failed-stop orphan may still run
  the server on that Worker
  ([`CONTROL_PLANE.md`](../app/CONTROL_PLANE.md) Section 5). The message still
  does not promise the start will happen: it says the start *may* still be
  applied on its own and to start again only if the server stays stopped,
  rather than the generic "wait and try again". That
  generic retry is actively misleading on the pending path — a retry while
  `desired_state=running` raises `invalid_transition`, which on **start** carries
  its own message ("already running or starting up") rather than the generic
  state-changed toast, so the operator does not get a second, contradictory
  answer. Stop and restart keep the state-changed treatment for
  `invalid_transition`: neither leaves a pending intent a retry collides with.
- **Start keeps the verb-agnostic message for 503 `worker_unavailable`.** A start
  that demonstrably did not happen is compensated back to stopped, but a
  **post-dispatch** timeout or lost session is not (the start may have been
  applied), while its pre-dispatch twin — a failed hydrate, or a call that never
  reached the Worker — compensates, and so does a start the Worker answered it
  could not carry out because its container runtime did not. A hydrate the
  Worker answered that way keeps the start pending, since the container it
  could not end may still be running. This is the identical pre/post ambiguity as `worker_busy`
  above, but a timeout answers *nothing*, so there is no honest thing to say
  beyond "could not reach the server host" — hence the verb-agnostic message.
  The `worker_state` member does not resolve it: each state arises both before
  and after the dispatch.
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
