# Auth API contract

> Status: **Reference** · Audience: contributors to `api/`, Web UI session layer
>
> The status-code and transport contract for the `/auth/*` endpoints (login,
> refresh, logout) and the `/users/me/sessions` session-management endpoints. It
> exists so contract changes have a place to land and a reviewer can diff a change
> against a documented baseline instead of re-deriving it from the router. **Code
> is the source of truth** (`api/src/mc_server_dashboard_api/identity/api/auth.py`
> and `.../identity/api/users.py`); where this doc and the router disagree, the
> router wins and this doc is stale.
>
> **Scope.** The `/auth/*` endpoints and the `/users/me/sessions` session
> endpoints (Section 7). Token issuance/verification semantics
> (FR-AUTH-2) live behind the `TokenService` Port
> ([`ARCHITECTURE.md`](ARCHITECTURE.md) Section 5.1); the brute-force / lockout
> behaviour that login participates in is owned by [`SECURITY.md`](SECURITY.md);
> the tunable token TTLs and cookie knobs are owned by
> [`CONFIGURATION.md`](CONFIGURATION.md) Section 5.3 and are referenced, not
> duplicated, here.

## Table of Contents

1. [Endpoints at a glance](#1-endpoints-at-a-glance)
2. [Error body shape](#2-error-body-shape)
3. [Token transport: body vs cookie](#3-token-transport-body-vs-cookie)
4. [Refresh rotation and the reuse grace window](#4-refresh-rotation-and-the-reuse-grace-window)
5. [CSRF posture](#5-csrf-posture)
6. [Audit events](#6-audit-events)
7. [Session management endpoints](#7-session-management-endpoints)
8. [Related documents](#8-related-documents)

## 1. Endpoints at a glance

The entire HTTP API is namespaced under `/api` (issue #498), so the auth
endpoints are `/api/auth/*`. The reason codes and `/users` / `/admin/users`
paths referenced below carry the same prefix.

All three endpoints accept and return JSON. Success and failure status codes:

| Endpoint | Success | Body | Failure |
|---|---|---|---|
| `POST /api/auth/login` | `200` | access + refresh pair + `Set-Cookie` (always) | `401` invalid credentials |
| `POST /api/auth/session` | `200` | access token only; **no `Set-Cookie`** (does not rotate) | `401` invalid/expired/revoked cookie, **or** no cookie |
| `POST /api/auth/refresh` | `200` | rotated access + refresh pair; `Set-Cookie` only if the request carried the cookie | `401` invalid/expired/revoked token, **or** no token in either transport |
| `POST /api/auth/logout` | `204` | empty; clearing `Set-Cookie` only if the request carried the cookie | — (idempotent; see below) |

Notable contract points, each verifiable in the router:

- **`POST /auth/login`** returns the FastAPI default `200` on success with a
  `{access_token, refresh_token, token_type: "bearer"}` body, and always sets the
  refresh cookie (it is the entry point that grants it). Both failure modes —
  unknown user and wrong password — collapse to a single `401` with no detail
  that distinguishes them (username-enumeration defence,
  [`SECURITY.md`](SECURITY.md) Section 2).
- **`POST /auth/session`** (issue #512) is the Web UI **bootstrap** path: it turns
  the httpOnly refresh cookie into a fresh **access token** and nothing more. It
  is cookie-only (no request body) and **does not rotate** — it emits no
  `Set-Cookie` and never mints a new refresh secret, so a page load / F5 can no
  longer race an in-flight rotation and leave a revoked predecessor cookie in the
  jar (the torn-rotation race that revoked the token family and bounced the user
  to `/login`). A missing cookie, or an unknown / expired / revoked one, returns
  the uniform `401`. Because restore never rotates, a re-presented rotated
  predecessor is just an invalid token here — it does **not** trip
  reuse-detection and does **not** revoke the family (that responsibility stays on
  `/auth/refresh`; see Section 4). Worker / CLI clients do not use this endpoint;
  they use `/auth/refresh`, which rotates the refresh token they hold.
- **`POST /auth/refresh`** with an empty / `{}` body **and** no cookie returns
  `401` *without invoking the use case* — a uniform `401`, not the `422` a missing
  required field would otherwise produce (changed in issue #365). An
  unknown / expired / revoked token also returns `401`; the client cannot tell
  the cases apart.
- **`POST /auth/logout`** with no token in either transport returns `204`, not
  `422` (changed in issue #365). Logout is idempotent: with nothing to revoke it
  is a clean `204` and emits no enumeration signal. A malformed body (e.g. a
  present-but-empty `refresh_token`, which violates `min_length=1`) still fails
  validation with `422`.

The `401` responses carry `WWW-Authenticate: Bearer`.

## 2. Error body shape

Every error response across the HTTP surface is RFC 9457
`application/problem+json` (issue #371, central module
`api/src/mc_server_dashboard_api/http_problem.py`). One body shape — the client
branches on exactly one contract:

```json
{
  "type": "urn:mcsd:error:<reason>",
  "title": "<HTTP status phrase>",
  "status": 401,
  "reason": "<reason>"
}
```

- `type` is a stable, non-resolvable `urn:mcsd:error:<reason>` URN; the machine
  code is its terminal segment **and** is surfaced verbatim as the `reason`
  extension member, so clients switch on `reason` without parsing the URI.
- A `422` validation failure adds an `errors` extension member (the per-field
  list) and uses `reason: "validation_error"`.
- A `403` permission denial (the membership permission gate) keeps the stable
  `reason: "forbidden"` and adds a `permission` extension member naming the
  required permission code (e.g. `"permission": "server:start"`), so the Web UI
  can name the missing permission in its denial toast (WEBUI_SPEC.md Section 7.4,
  issue #425). This exposes only which permission a known endpoint requires —
  static catalog data (WEBUI_SPEC.md Section 2.2) — not resource existence.

Reason codes the `/auth/*` endpoints emit:

| Status | `reason` | When |
|---|---|---|
| `401` | `invalid_credentials` | login failure, every refresh failure (missing / unknown / expired / revoked / reused token), **and** every session-restore failure (missing / unknown / expired / revoked cookie) — the router raises one uniform `401` for all of them, so the reason does not distinguish login from refresh from restore |
| `422` | `validation_error` | a malformed request body (e.g. login with a blank `username`/`password`, or a present-but-empty `refresh_token`) |

Note the refresh failure path deliberately reuses the `invalid_credentials`
reason rather than a token-specific code: the contract leaks no signal that would
distinguish *why* a refresh was rejected.

The same problem+json shape carries the password-policy `422` reason codes
emitted outside `/auth/*` by the user-management endpoints (registration
`POST /users`, password change `PUT /users/me/password`, admin user creation
`POST /admin/users`). Those codes — `too_short`, `too_long`,
`too_long_for_bcrypt`, `insufficient_complexity`, `common_password`,
`contains_user_info`, `simple_pattern` — are enumerated in
[`SECURITY.md`](SECURITY.md) Section 1.

## 3. Token transport: body vs cookie

The refresh token rides two transports (issue #363): the JSON body that
worker / CLI clients use, and an httpOnly cookie for the Web UI session
([`WEBUI_SPEC.md`](../ui/WEBUI_SPEC.md) Section 7.1). The body always carries the
refresh token even for cookie clients, so the body-based contract is unchanged
(non-breaking).

**Cookie attributes** (set on login):

```
Set-Cookie: <name>=<token>; HttpOnly; Secure; SameSite=Strict; Path=/api/auth; Max-Age=<refresh TTL>
```

The cookie name (`mcd_refresh` by default) and the `Secure` flag are operator
knobs (`auth.token.refresh_cookie_name`, `auth.token.refresh_cookie_secure`;
[`CONFIGURATION.md`](CONFIGURATION.md) Section 5.3). `Max-Age` tracks
`refresh_ttl_seconds`. `Path=/api/auth` and `SameSite=Strict` are fixed in the
router — the security posture, not knobs.

**Precedence and Set-Cookie emission** are two separate rules:

- **Body-token-wins precedence.** On refresh and logout, the body token is used
  when present; the cookie is a fallback for when the body carries none. If both
  are present, the body token is used.
- **Cookie emission follows cookie *presence*, not which token was used**
  (issue #372). Refresh re-sets (rotates) the cookie, and logout clears it,
  **only when the request itself carried the cookie**. A body-only request
  therefore leaves the response headers byte-for-byte unchanged — no rotated or
  clearing `Set-Cookie` that a non-browser client never asked for. A request that
  carries *both* a body token and the cookie uses the body token **and** still
  rotates the cookie the browser sent (otherwise that browser would be left with
  a stale cookie). Login is the exception: it always sets the cookie, because it
  is the entry point that grants it.

## 4. Refresh rotation and the reuse grace window

Each successful refresh **rotates**: the presented token is revoked and a fresh
access + refresh pair is issued in one transaction. Re-presenting an
already-rotated token is ambiguous — a legitimate concurrent refresh (two SPA
tabs, or a client retrying a refresh whose response was lost after the server
committed the rotation), or a replay of a leaked secret. A short **reuse grace
window** disambiguates (issue #369, `auth.token.refresh_reuse_grace_seconds`,
default **60s**):

| Presented token | Within grace window | Outside window / any time |
|---|---|---|
| rotated predecessor (revoked by rotation) | `200` + fresh pair, **family intact** | `401`, **whole family revoked** + `auth:refresh_reuse` DENIED audit event |
| family- or logout-revoked token | `401`, whole family revoked + DENIED audit event | same |
| unknown / expired token | `401` (no family action, not audited) | same |

Only a *rotation*-revoked predecessor is ever graced. A token revoked by a family
revoke (the theft response, or password change / deactivate / delete) or by
logout is never graced — re-presenting it stays on the theft path regardless of
how recent the revocation is. The grace-window predecessor is **not** re-revoked,
so repeated reuse cannot roll the window forward and keep a leaked token alive.

**`/auth/session` does not rotate; it leaves the rotation/reuse-detection
mechanism on `/auth/refresh` unchanged, but it does remove one *incidental*
theft signal** (issue #512). Restore validates the cookie and mints an access
token without revoking or re-issuing the refresh token, so it can never create a
torn rotation in the first place — which is exactly why the Web UI bootstrap uses
it instead of `/auth/refresh`. Rotation and reuse-detection remain entirely on
`/auth/refresh`, the periodic in-session path: against an *active* victim a
stolen refresh token is still invalidated the moment the legitimate holder's next
*refresh* rotates it (which revokes the stolen cookie), and re-presenting a
rotated token to `/auth/refresh` still trips the family revoke above. The gap is
the *idle* victim: a thief who replays the cookie **exclusively** against
`/auth/session` never collides with a rotation, so it raises no reuse signal and
can quietly mint access tokens until the refresh TTL expires.

The hardening follow-up (issue #530) closes that *detection* gap by making the
signal **explicit** rather than incidental: every successful restore now emits an
`auth:session_restore` SUCCESS audit row attributed to the session's user (see
Section 6). The eviction model is unchanged — restore still never revokes — but
the gap is no longer *invisible*: a thief minting access tokens against an idle
victim leaves a per-family restore trail an operator can review, and an anomalous
burst (or restores while the legitimate user believes they are logged out) is now
observable. This is proportionate to the minor severity (the cookie is httpOnly,
so the threat boundary is host/network compromise); a `last_used_at` column was
**not** added — the `refresh_token` row has no such column and adding one would
mean a new migration for a single-signal gain that the audit row already
delivers — and no restore-lifetime cap was introduced. Restore
deliberately does **not** trip reuse-detection: it has no rotation to
disambiguate, so a revoked/rotated cookie is simply an invalid token there (plain
`401`, no family action). Its read-only, no-rotation shape means a stolen cookie
replayed against `/auth/session` yields nothing the access-token TTL does not
already bound; it does not warrant rate-limiting beyond what `/auth/refresh`
carries.

### Guidance for the Web UI session layer

The httpOnly cookie is attached to refresh requests automatically, so concurrent
refreshes are easy to trigger. Within the grace window they are safe:

- **Concurrent tab refreshes** and **lost-response retries** within the window
  each get a fresh, valid pair and leave the family intact — they will not log the
  user out everywhere.
- **Beyond the window, serialize.** Two refreshes spaced further apart than
  `refresh_reuse_grace_seconds` that both present the same predecessor are treated
  as theft and revoke the whole family. Use a single-flight refresh mutex (one
  in-flight refresh per session; queued callers await its result) so a tab never
  replays a long-stale predecessor. This is the single-flight refresh the session
  lifecycle already mandates ([`WEBUI_SPEC.md`](../ui/WEBUI_SPEC.md) Section 7.1).

## 5. CSRF posture

Baseline: `SameSite=Strict` + `Path=/api/auth` on the refresh cookie (issues
#363, #365). `SameSite=Strict` keeps the browser from attaching the cookie to
cross-site requests; `Path=/api/auth` confines it to the auth endpoints. Refresh
returns the rotated tokens in the response body and performs no state change on
behalf of an ambient session, so it is not a useful CSRF target. The residual
surface is logout-by-forced-request, whose only effect is to end the victim's own
session. A stricter posture — require a custom `X-Requested-With` header that
cross-origin callers cannot set without a CORS preflight — is recorded in the
router docstring as an optional future upgrade, not built now.

## 6. Audit events

The endpoints record audit events (FR-AUD-1) for forensics, independent of the
status code returned to the client:

| Endpoint / path | Operation | Outcome | Actor |
|---|---|---|---|
| login success | `auth:login` | `SUCCESS` | the authenticated user |
| login failure | `auth:login` | `DENIED` | none (enumeration defence; the username / IP record lives in the `login_attempt` table) |
| refresh success | `auth:refresh` | `SUCCESS` | — |
| refresh reuse (family revoked) | `auth:refresh_reuse` | `DENIED` | the affected user (target: user) |
| session restore success | `auth:session_restore` | `SUCCESS` | the session's user (target: user) |
| logout (token presented) | `auth:logout` | `SUCCESS` | — |
| revoke one session (hit) | `auth:session_revoke` | `SUCCESS` | the caller (target: user) |
| revoke all other sessions | `auth:session_revoke` | `SUCCESS` | the caller (target: user) |

A `DELETE /users/me/sessions/{id}` that misses (unknown / malformed id, or one
owned by another user — all `404`) records **no** row: it changed nothing and is
not a security signal. Listing (`GET /users/me/sessions`) is a read and is not
audited.

A plain unknown / expired refresh token is **not** audited — it is not a
token-theft signal, so auditing it would be noise. `/auth/session` follows the
same rule on failure: a missing, unknown, or revoked cookie stays a silent `401`
with no row (no enumeration signal). A *successful* restore, however, **is**
audited (`auth:session_restore`, issue #530): restore never rotates, so it lacks
the incidental reuse signal `/auth/refresh` carries, and this explicit per-family
SUCCESS row is its replacement — it lets operators see session-restore activity
against an idle victim's cookie even though restore never revokes the family
(Section 4).

## 7. Session management endpoints

A refresh token is a persisted **session** (`refresh_token` row). These endpoints
let the authenticated caller see and revoke their own sessions (issue #387). They
are on `/api/users/me/sessions`, so — unlike `/auth/*` — they authenticate by the
**access token** (`Authorization: Bearer <access token>`), the same as the rest of
`/users/me`. Code:
`api/src/mc_server_dashboard_api/identity/api/users.py`.

| Endpoint | Success | Body | Failure |
|---|---|---|---|
| `GET /api/users/me/sessions` | `200` | JSON array of the caller's active sessions | — (empty array if none) |
| `DELETE /api/users/me/sessions/{id}` | `204` | empty | `404` `session_not_found` (unknown / malformed id, **or** owned by another user) |
| `DELETE /api/users/me/sessions` | `204` | empty | — |

**List** returns only **active** (non-revoked, non-expired) sessions of the
caller, newest-first. Each item is safe metadata only — the row id (an opaque
session id used to address a revoke) plus `created_at` and `expires_at`:

```json
[
  { "id": "<uuid>", "created_at": "<ISO 8601>", "expires_at": "<ISO 8601>" }
]
```

The raw refresh-token secret and its stored hash are **never** exposed. There is
no client-hint field because no such metadata is stored on the row (the proposal
allowed one only if already stored; it is not).

**Revoke one** (`DELETE /users/me/sessions/{id}`) revokes a single session the
caller owns, stamping `revoked_reason = 'user_revoked'`. The operation is scoped
to the caller's user id, so an id that is unknown, malformed, **or** owned by
another user all return the same `404 session_not_found` — never `403` — so the
endpoint leaks neither the session's existence nor its owner. A revoked session
can no longer refresh (a `user_revoked` token is never graced in the reuse window;
Section 4).

**Revoke all others** (`DELETE /users/me/sessions`) is everywhere-else logout. It
keeps the caller's **current** session alive and revokes the rest, also stamping
`revoked_reason = 'user_revoked'`. The current session is identified by the
refresh token the caller presents in an optional JSON body
(`{ "refresh_token": "<secret>" }`): the row whose hash matches is spared. Because
these endpoints authenticate by access token and the refresh cookie is confined to
`/api/auth` (Section 3), a browser request reaches this endpoint with **no**
refresh token; in that case the current session cannot be identified, so **all**
the caller's active sessions are revoked. This is the safe choice — it never
revokes another user's sessions, and a presented token is the only trustworthy way
to know which row is "current". A Web UI calling this should send its current
refresh token in the body to stay logged in on the device it is using.

## 8. Related documents

- [`SECURITY.md`](SECURITY.md) — login's brute-force / lockout behaviour and the
  username-enumeration posture behind the uniform `401`.
- [`CONFIGURATION.md`](CONFIGURATION.md) Section 5.3 — token TTLs, the reuse grace
  window, and the cookie name / `Secure` knobs.
- [`WEBUI_SPEC.md`](../ui/WEBUI_SPEC.md) Section 7.1 (auth/session lifecycle) and
  Section 7.4 (the problem+json error layer) — how the Web UI consumes this
  contract.
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the `TokenService` Port behind token
  issuance / verification.
