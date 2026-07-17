# Configuration

> Status: **Implemented** · Audience: contributors and operators of `api/` and
> `worker/`
>
> This document defines the **runtime configuration surface** of v2 and the
> **config-driven adapter selection** mechanism. It refines, but does not
> contradict, [`../REQUIREMENTS.md`](../REQUIREMENTS.md) and
> [`ARCHITECTURE.md`](ARCHITECTURE.md); where they disagree, the requirements
> win and this document is wrong.
>
> Scope is the *configuration contract* — which keys exist, what they select,
> and their defaults — not how configuration is loaded. The loaders are shipped
> and fail fast on a bad config (Section 2); the key names below are the settled
> surface.

## Table of Contents

1. [Principles](#1-principles)
2. [Sources and precedence](#2-sources-and-precedence)
3. [Secrets](#3-secrets)
4. [Config-driven adapter selection](#4-config-driven-adapter-selection)
5. [API configuration](#5-api-configuration)
6. [Worker configuration](#6-worker-configuration)
7. [Authentication hardening](#7-authentication-hardening)
8. [Snapshot cadence](#8-snapshot-cadence)
9. [Related documents](#9-related-documents)

---

## 1. Principles

- **Wiring at the edge.** Configuration is read **only** at the edge / wiring
  layer (ARCHITECTURE.md Section 2.1) — the process `main`, DI setup, and the
  routers/handlers. It selects and constructs adapters, then injects Ports into
  use cases. Domain and application layers never read configuration; they
  receive already-wired Ports (ARCHITECTURE.md Section 2.2).
- **Config-driven adapter selection.** Every swappable technology sits behind a
  Port (REQUIREMENTS.md NFR-PORT-1); which adapter fulfils a Port is a
  configuration choice, not a code change (REQUIREMENTS.md FR-DATA-2). See
  Section 4.
- **Secrets via configuration/environment.** No secret is ever hard-coded
  (REQUIREMENTS.md NFR-SEC-3). See Section 3.
- **Proportionate surface.** Target scale is small (REQUIREMENTS.md
  NFR-SCALE-1); the configuration surface is the minimum needed to select
  adapters and tune the documented behaviours, not an exhaustive knob for every
  internal constant.
- **Three runtime processes.** `api/` (Python), `worker/` (Go), and `relay/`
  (Go) are configured independently; each has its own configuration. `webui/`
  (TypeScript) is a build-time component with no runtime configuration of its
  own. `proto/` is a build artifact and has no runtime configuration.

---

## 2. Sources and precedence

Both services read configuration from two sources, with environment variables
overriding the file:

```
   defaults (in code)  <  config file  <  environment variables
   (lowest precedence)                    (highest precedence)
```

- **Defaults** are the values in this document; a key omitted everywhere takes
  its default. A key with no default (marked *required*) must be supplied.
- **Config file** — a single **TOML** file per service (e.g. `api.toml` /
  `worker.toml`). Holds the non-secret bulk of configuration and is the
  recommended place for adapter-selection and tuning keys.
- **Environment variables** — highest precedence; override any file value. The
  intended channel for **secrets** (Section 3) and for per-deployment overrides
  (container/orchestrator injection). Names are the UPPERCASE keys in the tables
  below.

A startup configuration error (a required key missing, an unknown adapter name,
a malformed value) is **fatal**: the service fails fast at boot rather than
starting in a half-configured state.

The tables below give the **logical key name**. The environment-variable form
is the key prefixed per service (`MCD_API_` for `api/`, `MCD_WORKER_` for
`worker/`) to avoid collisions; the file form nests the same key under its
group.

---

## 3. Secrets

Secrets are read from configuration/environment and **never** hard-coded or
committed (REQUIREMENTS.md NFR-SEC-3). Every key that carries a credential, key,
or token is marked **secret** in the tables below.

- Secrets are supplied via **environment variables** (Section 2) or a
  file-system path to the secret material (e.g. a TLS key file), never inline in
  a committed config file.
- Secret values are **masked** wherever configuration is logged or echoed, in
  line with the structured-logging masking rule (REQUIREMENTS.md NFR-OBS-1).
- A missing **required** secret is a fatal startup error (Section 2); the
  service does not fall back to an insecure default. A **blank** value (an empty
  or whitespace-only string — the natural result of a blank `${VAR}` compose
  interpolation) is treated as missing, so it fails fast the same way rather than
  booting with an empty key/credential.

The M1 secrets are: the API token-signing key (Section 5), the Worker's
credential for authenticating to the API, and the TLS material for the
control channel (Sections 5 and 6).

---

## 4. Config-driven adapter selection

Selection follows one pattern: a **selector key** names the adapter for a Port,
and **adapter-specific keys** configure the chosen adapter. The wiring layer
reads the selector, constructs that adapter, and binds it to the Port
(ARCHITECTURE.md Section 2.1). An unknown adapter name is a fatal startup error
(Section 2). Keys for non-selected adapters are ignored.

| Port (ref.) | Selector key | Adapter choices (M1) | Side |
|---|---|---|---|
| `Storage` (FR-DATA-2) | `storage.backend` | `fs` / `remote-fs` / `object` | api |
| `PasswordHasher` (FR-AUTH-3) | `auth.password.hash` | `argon2` / `bcrypt` | api |
| `ExecutionDriver` (FR-EXE-2) | `worker.drivers` | `container` (only shipped driver) | worker |

Notes:

- The `ExecutionDriver` Port is **pluggable** by design (FR-EXE-2): the seam
  admits future backends (e.g. a Kubernetes driver). `container` is the only
  driver shipped today.
- The Worker's `ExecutionDriver` selection is a **set**, not one value: a Worker
  advertises *which* drivers it offers (Section 6), and the API's greedy
  placement filters Workers by the driver a server needs (REQUIREMENTS.md
  FR-WRK-3).
- The `Storage` adapter contract and the per-backend keys' full semantics are
  owned by STORAGE.md (#17); this document fixes only the **selector** and the
  shape of the adapter-specific groups (Section 5.2).
- Other Ports in ARCHITECTURE.md Section 5 have a **single M1 adapter** and so
  need no selector key (e.g. `TokenService`, `PermissionChecker`,
  `WorkerRegistry`, `Clock`); they are added here only if a future milestone
  introduces a choice. `TokenService` is a single "JWT-or-equivalent" adapter
  (ARCHITECTURE.md Section 5.1, FR-AUTH-2); its `auth.token.algorithm` is an
  adapter parameter (Section 5.3), not an adapter selector.

---

## 5. API configuration

Grouped by concern. **Secret** marks credentials/keys (Section 3); *required*
marks keys with no default.

### 5.1 Server and transport

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `server.host` | `0.0.0.0` | | Bind address for the HTTP API (REST + data-plane endpoint). |
| `server.http_port` | `8000` | | Port for the HTTP API. Must be 0..65535; `0` binds an OS-assigned ephemeral port. |
| `server.grpc_port` | `50051` | | Port the control-plane gRPC server listens on for Worker-initiated streams (REQUIREMENTS.md Section 5.1). Must be 0..65535; `0` binds an OS-assigned ephemeral port. |
| `server.public_base_url` | *required for hydrate/snapshot* | | Externally reachable base URL of the API's data-plane HTTP endpoint, handed to Workers for hydrate/snapshot transfer (REQUIREMENTS.md Section 5.2, STORAGE.md Section 8). Optional in code so a process that never dispatches a transfer need not set it; a lifecycle command that needs it fails fast when it is unset. |
| `server.data_plane_base_url` | `server.public_base_url` | | Base URL handed to Workers for hydrate/snapshot transfers instead of `server.public_base_url` (issue #1549). Defaults to `server.public_base_url` so a split deployment with no edge proxy sees no change. Set this on a deployment whose public URL routes through a body-size-capped edge (e.g. Cloudflare Tunnel, ~100 MB, see `docs/dev/DEPLOYMENT.md` Section 8) to an internal address instead, so a co-located Worker's working-set upload bypasses the edge rather than hairpinning through it and 413ing. |
| `control.enabled` | `true` | | Whether the API hosts the control-plane gRPC server in this process (REQUIREMENTS.md Section 5.1). When `true`, `control.worker_credential` is required (fail-fast). |
| `control.worker_credential` | *required when enabled* | secret | Shared credential a Worker must present (`authorization: Bearer <credential>` metadata) to authenticate its stream (REQUIREMENTS.md NFR-SEC-1); the API-side counterpart of the Worker's `api.credential` (Section 6.1). |
| `control.tls.cert_file` | *required²* | | Path to the control-channel TLS server certificate the gRPC listener presents (REQUIREMENTS.md NFR-SEC-1). |
| `control.tls.key_file` | *required²* | secret | Path to the control-channel TLS private key. |
| `control.tls.insecure` | `false` | | When `true`, bind the control-plane gRPC listener in plaintext (no TLS). Local/dev only; the API logs a `WARN` at startup. Required to opt out of TLS when no cert/key pair is set. |
| `control.tls.client_ca_file` | — | | **Deferred (M1).** Reserved for client-certificate (mTLS) verification of the Worker. Unused today — the shared `control.worker_credential` authenticates the Worker (NFR-SEC-1), so M1 ships server-side TLS only. Documented to keep the config shape forward-compatible. |
| `control.heartbeat_timeout_seconds` | `30` | | Liveness window: a Worker missing heartbeats past this is marked disconnected (REQUIREMENTS.md FR-WRK-2). Must be positive. |
| `control.command_timeout_seconds` | `30` | | Deadline for a dispatched `ApiCommand` to be answered by a `CommandResult`; an unanswered command is treated as a failure (CONTROL_PLANE.md Section 4.2). Must be positive. See the reconciler grace floor below.³ |
| `control.hydrate_timeout_seconds` | `600` | | Separate, longer deadline for the **hydrate** phase of a start (issue #822): pulling a large world's working set routinely outlasts `command_timeout_seconds`, so the hydrate trigger gets its own budget instead of forcing the global command timeout up (which governs every other command and widens the duplicate-start window). Only the start's hydrate dispatch uses it; all other commands stay on `command_timeout_seconds`. Must be positive. See the reconciler grace floor below.³ |
| `control.snapshot_timeout_seconds` | `600` | | Separate, longer deadline for the **final snapshot** a graceful stop captures (issue #847). The stop holds the server's assignment until this snapshot settles (closing the stop→re-place generation race), so the dispatch must span a full working-set upload (minutes for a large world); under `command_timeout_seconds` it would time out and release the assignment mid-upload, reopening the race. Only the stop's final-snapshot dispatch uses it; all other commands stay on `command_timeout_seconds`. Must be positive. A snapshot **timeout** holds the assignment (not just a cancel), recovered by the stale-stop arm, so it co-bounds the reconciler grace floor below via `grace_seconds > snapshot_timeout_seconds` — at stock values dominated by the duplicate-start term, but binding when raised above `hydrate_timeout_seconds + command_timeout_seconds`.³ |
| `control.stop_timeout_seconds` | `600` | | Separate, longer deadline for the **stop** command's worker round-trip (issue #930). A graceful stop does an in-container save (RCON `save-all`) and the worker's docker-stop escalation (RCON wait → SIGTERM grace → SIGKILL), which on a slow/CPU-starved host or a large world routinely outlasts `command_timeout_seconds`; under it the dispatch times out and the API returns **503** for a stop the worker actually completes, wedging the row at `(stopped, stopped, assigned)` until the reconciler's stale-stop arm clears it and silently losing the stop-leg final snapshot. Only the stop dispatch uses it; all other commands stay on `command_timeout_seconds`. Must be positive. Kept below `reconciler.grace_seconds` so the reconciler never replays a stop still legitimately in flight — it is the THIRD term of the grace floor below.³ |

² `control.tls.cert_file` and `control.tls.key_file` are required **together,
unless** `control.tls.insecure=true`. With neither the cert/key pair nor
`insecure=true` set — or with only one of cert/key set — `create_app` fails fast
at startup; with `control.tls.insecure=true` the listener binds plaintext
(local/dev only) and logs a `WARN`. Production must set the cert/key pair (or
terminate TLS at a reverse proxy and run the listener `insecure=true` behind it
— see CONTROL_PLANE.md Section 2). This mirrors the Worker's `api.tls.*`
required-unless-insecure rule (Section 6.1): the Worker verifies this
certificate against its `api.tls.ca_file`.

³ **Reconciler grace floor (duplicate-start + stop-hold safety, issue #774/#812/#822/#847/#930).** Keep
`reconciler.grace_seconds > max(control.hydrate_timeout_seconds + control.command_timeout_seconds, control.snapshot_timeout_seconds, control.stop_timeout_seconds)`.
The reconciler must wait out the longest a started server's FIRST dispatch
round-trip can still be in flight before it re-dispatches; a start's dispatch is
hydrate-then-start, so that round-trip is bounded by the hydrate budget plus the
start command deadline. Below the floor, a slow start crashed/timed-out mid-flight
can still be converging on its assigned Worker when the reconciler's orphan path
re-places it elsewhere and starts a **second** live instance. `create_app` logs a
`WARN` (not a hard failure) when the floor is violated. The stock default
(`grace_seconds=660`) already exceeds the stock floor (`max(600 + 30, 600) = 630`),
so no warning fires out of the box; lower `grace_seconds` (or raise the timeouts)
only when adjusting the reconciler for non-default budgets.

The stop-side `control.snapshot_timeout_seconds` (issue #847) is the SECOND term of
the floor. That budget bounds the stop's held final snapshot, recovered by the
reconciler's stale-stop arm; the arm carries its own safety constraint
`grace_seconds > control.snapshot_timeout_seconds` (so the arm never clears a
still-healthy snapshot hold mid-upload — which would reopen the stop→re-place race
the hold exists to close, now that a final-snapshot **timeout** also leans on the
arm rather than releasing the assignment). At stock values the duplicate-start
bound (630) dominates the snapshot bound (600), but an operator who raises
`snapshot_timeout_seconds` above `hydrate_timeout_seconds + command_timeout_seconds`
makes the snapshot bound binding — hence the `max(...)`, which enforces both.

The stop command's own `control.stop_timeout_seconds` (issue #930) is the THIRD
term. It bounds the FIRST dispatch of a stale stop the reconciler replays
(`redispatch_stop` on an `observed=running` row): the row stays diverged while that
dispatch is in flight, so grace must exceed it or the reconciler re-selects and
re-dispatches the same stop before the first settles. At stock values it sits under
the duplicate-start bound (630), but an operator who raises it must keep grace above
it.

> **Upgrade impact (existing dev setups).** This is a behavior change: a dev
> process with `control.enabled=true` that previously bound plaintext now fails
> fast at startup unless it sets `control.tls.insecure=true` (or supplies a
> cert/key pair). Add `control.tls.insecure=true` to local/dev configs.

### 5.2 Persistence and Storage adapter

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `database.url` | *required* | secret | Connection string for the persistence adapter (may embed credentials). Model owned by DATABASE.md (#15). |
| `database.pool_size` | `5` | | Number of permanent connections SQLAlchemy keeps open. Must be positive. Mirrors SQLAlchemy's own default so existing deployments see no change until tuned. |
| `database.max_overflow` | `10` | | Maximum extra connections SQLAlchemy opens above `pool_size` under burst load, returned to the pool when idle. Must be non-negative; `0` disables overflow. Mirrors SQLAlchemy's own default. |
| `storage.backend` | `fs` | | Selector for the `Storage` Port (Section 4): `fs` / `remote-fs` / `object`. |
| `storage.fs.root` | `./data` | | Root directory for the `fs` and `remote-fs` backends. For `fs` this is a local path; for `remote-fs` point it at the POSIX mount path (the `fs` adapter is reused over the mount — STORAGE.md Section 7.2). |
| `storage.version_retention` | `10` | | Maximum per-file prior versions retained for rollback; the oldest beyond this count are pruned (STORAGE.md Section 5). Must be non-negative; `0` retains no prior versions. |
| `storage.object.endpoint` | — | | Object-store endpoint when `storage.backend = object`. |
| `storage.object.bucket` | — | | Object-store bucket/container. |
| `storage.object.access_key` | — | secret | Object-store access key. |
| `storage.object.secret_key` | — | secret | Object-store secret key. |

Only the keys for the selected `storage.backend` are read; the rest are ignored
(Section 4). The authoritative per-backend key list and the atomic-snapshot
publish behaviour (REQUIREMENTS.md FR-DATA-6) live in STORAGE.md (#17).

**Connection-pool sizing and the lifecycle lock (#827/#876/#884).** Every at-rest-gated
operation (restore, delete, the file mutations, update, create-backup) and every
`StartServer` flip holds a *per-server* advisory lock on a **dedicated** pool
connection for the operation's duration — a long gated op (a minutes-long
restore/delete pack) thus pins one connection on top of the connection its own
unit-of-work uses. The lock's acquire is *bounded*: a waiter that cannot take the
lock within a few seconds gives up with a `409 server_busy` rather than block (and
pin a slot) indefinitely, so contention cannot deadlock the pool — but both the
*holder* and each *bounded waiter* each pin one dedicated connection for their
duration. Size the pool with that headroom in mind:

- Each **held** LifecycleLock pins **one** connection for the duration of the
  gated operation (the lock acquire + the op itself).
- Each **bounded waiter** (~5 s timeout) pins **one** connection while it waits.
- Normal request traffic uses additional connections concurrently.

A reasonable sizing formula: `pool_size + max_overflow ≥ (expected concurrent gated ops) + (expected peak waiters) + (normal concurrent request traffic)`. The shipped defaults (`pool_size=5`, `max_overflow=10`) give a ceiling of 15, adequate for a deployment with a handful of simultaneous gated operations. Raise `pool_size` (permanent connections) first; `max_overflow` adds burst headroom at the cost of connection churn.

### 5.3 Authentication: tokens and password hashing

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `auth.token.algorithm` | `HS256` | | Signing algorithm of the `TokenService` JWT adapter (REQUIREMENTS.md FR-AUTH-2). One of `HS256` / `RS256` (case-sensitive); any other value fails fast at load. A parameter of the adapter, not an adapter selector (Section 4). |
| `auth.token.signing_key` | *required* | secret | Signing key/secret for access & refresh tokens (REQUIREMENTS.md FR-AUTH-2). For an asymmetric algorithm this is the private key (path or value). Under `HS256` the key is shared-secret entropy and **must be at least 32 bytes** (the 256-bit digest length); a shorter key fails fast at load. |
| `auth.token.access_ttl_seconds` | `900` | | Short-lived access-token lifetime. Must be positive and **strictly less than** `refresh_ttl_seconds` (an access token may not outlive the refresh token); a non-conforming pair fails fast at load. |
| `auth.token.refresh_ttl_seconds` | `1209600` | | Long-lived refresh-token lifetime (14 days). Must be positive. Also the `Max-Age` of the refresh cookie below. |
| `auth.token.refresh_reuse_grace_seconds` | `60` | | Grace window after a refresh token is rotated within which re-presenting the **predecessor** is treated as a legitimate concurrent refresh (two SPA tabs, or a retry of a refresh whose response was lost) rather than theft (issue #369): a fresh pair is issued and the token family is kept. Outside the window the reuse still revokes the whole family (DENIED audit event). Must be positive. Larger values tolerate more clock skew / slower retries at the cost of a longer replay window for a leaked predecessor secret. |
| `auth.token.refresh_cookie_name` | `mcd_refresh` | | Name of the `HttpOnly` refresh-token cookie: `/api/auth/login` always sets it (the sole refresh-token transport from login), `/api/auth/session` reads it without re-setting it, and both `/api/auth/refresh` (rotates) and `/api/auth/logout` (clears) emit `Set-Cookie` only when the request carried it (AUTH_API.md Section 3). Must be non-empty. |
| `auth.token.refresh_cookie_secure` | `true` | | Sets the `Secure` flag on the refresh cookie (HTTPS-only). Turn it **off** only for plain-HTTP localhost development so the browser will store the cookie. |
| `auth.password.hash` | `argon2` | | `PasswordHasher` selector (Section 4): `argon2` / `bcrypt`. |

Password **policy** (strength, brute-force, proxy trust) is configured
separately in Section 7.

### 5.4 Snapshot cadence

The API drives snapshot scheduling (REQUIREMENTS.md FR-DATA-7); see Section 8
for the full cadence model.

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `snapshot.default_interval_seconds` | `3600` | | Global default periodic snapshot interval applied to every running server. Must be positive. |
| `snapshot.min_interval_seconds` | `300` | | Lower bound a per-server override may not go below (guards against snapshot thrash). Must be positive and **not exceed** `default_interval_seconds`; a floor above the default fails fast at load. |

### 5.5 Backup cadence

Scheduled backups (REQUIREMENTS.md FR-BAK-3) are no longer a standalone loop.
The legacy `backup_interval_hours` config-key cadence and its dedicated
`backup.schedule_tick_seconds` tick are **retired** (issue #1840): backups are
now a first-class `backup` schedule on the general scheduler, so its
`schedule.tick_seconds` (Section 5.14) is the only cadence knob. This section has
no keys.

**Migration note:** remove any `[backup]` section (`backup.schedule_tick_seconds`)
from the config file when upgrading — an unknown TOML key fails the load
(Section 2 fail-fast) and the API will not boot. A stale `MCD_API_BACKUP__*`
environment variable is silently ignored.

### 5.6 Divergence reconciler

The API runs a background reconciler that re-dispatches durable-but-unsent
lifecycle intent so a desired/observed divergence converges (REQUIREMENTS.md
FR-SRV-3/4). Two windows the in-line lifecycle path leaves open are closed here:
a start/stop committed just before a crash (never dispatched) and a
compensation-failure orphan (`desired=running` with no assigned Worker). The loop
is gated on the control plane like the snapshot scheduler — with no Worker
channel there is nothing to re-dispatch.

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `reconciler.interval_seconds` | `60` | | Loop resolution: how often the reconciler scans for diverged servers. Must be positive. |
| `reconciler.grace_seconds` | `660` | | How long a divergence must persist (measured from the last Worker report) before it is acted on, so the normal in-flight lifecycle path has time to converge first. Must be positive and `> max(control.hydrate_timeout_seconds + control.command_timeout_seconds, control.snapshot_timeout_seconds, control.stop_timeout_seconds)` (the start round-trip budget, the held stop-snapshot budget, and the stop dispatch budget); below the floor the reconciler can re-dispatch a first start before it settles (risking a duplicate live instance), clear a still-healthy final-snapshot hold mid-upload (reopening the #847 race), or replay a stale stop before its first round-trip settles (#930) — a `WARN` fires on boot. The stock default (660) satisfies the stock floor (`max(600 + 30, 600, 600) = 630`). |
| `reconciler.held_start_grace_seconds` | `90` | | The SHORTER grace applied only to a `redispatch_start` whose assigned Worker is connected AND already holds a fresh-enough working set, so the start skips hydrate and is command-only (issue #999). The long hydrate-based `grace_seconds` is pure dead waiting there, and the cross-worker duplicate-live-instance race cannot occur on a re-dispatch to the same already-connected Worker (its double-start guard rejects a second live start). All other paths keep the full `grace_seconds`. Must be positive and `<= grace_seconds`; a `WARN` fires on boot when violated. |
| `reconciler.backoff_base_seconds` | `30` | | Base of the per-server exponential backoff after a failed re-dispatch; the wait doubles per consecutive failure. Must be positive. |
| `reconciler.backoff_max_seconds` | `3600` | | Cap on the per-server backoff wait. Also doubles as the slack past `next_eligible_at` that keeps crash-loop damping alive across a slow (modded) boot's `starting` window. Must be positive, `>=` `backoff_base_seconds`, and `>= 600` (a smaller slack lets a still-diverged server expire and reset its failure count, re-arming the boot-crash loop). |

### 5.7 JAR-pool garbage collection

The API runs a background reference-counted GC that reclaims pooled server JARs
no live server row references (STORAGE.md Section 3.2). It is gated on the control
plane like the snapshot/reconciler loops, and a platform admin can also
trigger a sweep on demand (`POST /versions/jar-pool/gc`).

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `jar_gc.interval_seconds` | `86400` | | Loop resolution: how often the GC wakes to sweep the JAR pool. The pool grows slowly (one entry per distinct resolved JAR), so a daily default is ample. Must be positive. |

### 5.8 Game ports

The API tracks each server's Minecraft game port (DATABASE.md Section 7,
`server.game_port`) and assigns one at create from this range — the lowest free
in-range port, unique deployment-wide — so two servers never collide on a port
(issue #243). An operator may instead supply an explicit `game_port` in the
create request; it is rejected with 422 when out of range and 409 when already
taken. A delete frees the server's port for reuse.

The API also tracks each server's public Bedrock **UDP** port
(`server.bedrock_port`, issue #1541), allocated from a dedicated window — an
independent namespace from the TCP game-port range (TCP and UDP ports do not
collide) — when Geyser is detected among the server's plugins and the Bedrock
deployment gate is on (`relay.enabled` + `relay.bedrock_enabled`, Section 5.13).
The default window starts at Bedrock's conventional port 19132 so the first
allocation lands there. Geyser uninstall and server delete free the port.

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `ports.range_start` | `25565` | | Lowest assignable game port (inclusive). Must be `1..65535`. |
| `ports.range_end` | `25664` | | Highest assignable game port (inclusive). Must be `1..65535` and `>=` `range_start`. |
| `ports.bedrock_range_start` | `19132` | | Lowest assignable Bedrock UDP port (inclusive). Must be `1..65535`. |
| `ports.bedrock_range_end` | `19231` | | Highest assignable Bedrock UDP port (inclusive). Must be `1..65535` and `>=` `bedrock_range_start`. |

### 5.9 Server memory limits

Operator-configurable memory-limit defaults and ceiling for per-server allocations
(issue #1069). Both default to unset (the hardcoded defaults apply). Surfaced to
clients via `GET /api/meta` so the create wizard can pre-fill and cap the value.

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `memory_limit.default_mb` | *unset* | | Application-wide default memory allocation (MiB) for new servers when the create request omits `memory_limit_mb`. `None` (unset) preserves the current behaviour (blank / driver default). Must be at least 512 MiB; must not exceed `max_mb` when both are set; must not exceed the built-in ceiling (1 TiB) when `max_mb` is unset. |
| `memory_limit.max_mb` | *unset* | | Operator-configurable ceiling that replaces the hardcoded 1 TiB max. `None` (unset) preserves the built-in ceiling. Must be at least 512 MiB. |

### 5.10 Observability

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `log.level` | `info` | | Log verbosity. |
| `log.format` | `json` | | Structured-log format; `json` keeps logs machine-parseable (REQUIREMENTS.md NFR-OBS-1). |

### 5.11 Web UI serving

The API can serve the built browser UI (`webui/dist`) from its own origin with an
SPA fallback — no reverse proxy, no CORS, no separate service (WEBUI_SPEC 7.7,
issues #386 and #490). The compose `api` image builds the SPA and sets this to
`/app/webui/dist`; in development the key is left unset (Vite serves the UI and
proxies the API).

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `webui.dist_dir` | *unset* | | Directory of the built SPA to serve at `/`. When set, the API mounts it after every router (so API routes and WS endpoints take precedence) and falls back to `index.html` for unmatched paths so deep links/reloads resolve. Must be an existing directory containing `index.html`; otherwise startup fails fast. When unset, nothing is mounted. |

### 5.12 Plugin-cache garbage collection

The API runs a background GC that reclaims cached plugin/mod blobs no
`server_plugin` row references (issue #1332). It is gated on the control plane
like the JAR-pool GC (Section 5.7).

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `plugin_cache_gc.interval_seconds` | `86400` | | Loop resolution: how often the GC wakes to sweep the plugin cache. The cache grows slowly (one entry per distinct plugin blob), so a daily default is ample. Must be positive. |

### 5.13 Game-ingress relay

The game-ingress relay (RELAY.md, epic #659) lets players join at
`<slug>.<base_domain>` with no port. It is **config-selectable and default off**
(RELAY.md Section 9): with `relay.enabled=false` (the default) single-host
operators keep the direct path with zero new setup. When enabled, the API serves
`RelayService` on the existing gRPC listener (`server.grpc_port`) alongside
`WorkerService`, exposes `join_hostname` on server responses, and (issue #957)
runs the session prune loop. `relay.credential` and `relay.base_domain` are both
required when enabled, enforced fail-fast at the edge (a blank value is treated as
missing, per the secret-blank rule above).

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `relay.enabled` | `false` | | Master switch: serve `RelayService`, expose `join_hostname`, and run the prune loop (RELAY.md Section 9). |
| `relay.credential` | *required when enabled* | secret | Shared credential the relay must present (`authorization: Bearer <credential>` metadata) to authenticate its gRPC calls (REQUIREMENTS.md NFR-SEC-1). A **separate** credential from `control.worker_credential` so relay and Worker credentials rotate independently (RELAY.md Section 6). |
| `relay.base_domain` | *required when enabled* | | Routing domain (e.g. `mc.example.com`); used to build `join_hostname` (`<slug>.<base_domain>`) and returned to the relay on `Register` (RELAY.md Sections 3, 6). |
| `relay.game_port` | `25565` | | The relay container's published game-listener host port (RELAY.md Section 13): the port players join on (fixed at 25565 to keep joins port-less). When the relay is enabled the allocator excludes any of these relay ports that fall inside the assignable game-port range so a server is never assigned a port the relay already holds on the host (issue #1002). Must be 1..65535. |
| `relay.tunnel_port` | `25665` | | The relay container's published tunnel-listener host port (RELAY.md Section 13): the Worker dial-back tunnel endpoint. When the relay is enabled the allocator excludes it like `relay.game_port`. Must be 1..65535. |
| `relay.bedrock_enabled` | `false` | | Bedrock ingress capability (issue #1541). With `relay.enabled`, a server whose plugin set carries Geyser -- via install, catalog install, or a ZIP import (issue #1551) -- allocates its `bedrock_port` from the dedicated UDP window (Section 5.8); server responses surface `bedrock_address` / `bedrock_port` only while at least one installed Geyser copy is *enabled* (issue #1555) -- the port stays allocated but the fields go null if every copy is disabled. `GET /api/meta` reports the combined gate as `bedrock_enabled`. Off: Geyser detection allocates nothing and the fields stay null. |
| `relay.bedrock_tunnel_port` | `25675` | | The relay container's published Bedrock tunnel (QUIC) **UDP** listener host port (epic #1540). When the Bedrock gate is on, the allocator excludes it from the assignable Bedrock window like `relay.game_port` / `relay.tunnel_port`. Must be 1..65535. |
| `relay.session_retention_days` | `90` | | `game_session` prune window in days (RELAY.md Section 8; consumed by issue #957). Must be positive. |

### 5.14 General scheduler

The API runs the general-scheduler runner (epic #649, issue #1838), which polls
the `schedule` table for due per-server actions (console command / start / stop /
restart / backup) and dispatches them through the existing lifecycle, command,
and backup use cases. Like the snapshot loop it is gated on the control plane
(`control.enabled`) — a scheduled action needs a Worker channel. This only tunes
the poll cadence; the schedules themselves are per-server rows.

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `schedule.tick_seconds` | `20` | | Loop resolution of the scheduler runner: how often it wakes to poll `next_run_at` over enabled schedules. Fine because a schedule can fire as often as every minute (the domain interval floor), and it must stay well under the runner's fixed 300 s late-run grace so an on-time occurrence is never judged stale. Must be positive and at most 300 (the grace itself): a coarser tick would render every non-backup occurrence perpetually stale, never executed. Player warnings on stop/restart schedules (issue #1839) key off this cadence too: their send grace is derived as max(60 s, `tick_seconds`), so warnings are reliable whenever the tick is at most 60. With a coarser tick a warning fires up to one tick late (always strictly before the action), and a warning offset smaller than the tick may be skipped entirely — logged, never silent. |

---

## 6. Worker configuration

The Worker is stateless and replaceable (REQUIREMENTS.md FR-WRK-4); its
configuration tells it **where the API is**, **how to authenticate**, **what it
can run**, and **where its scratch space is**.

### 6.1 API connection and authentication

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `api.grpc_endpoint` | *required* | | Address of the API control-plane gRPC server the Worker dials to open its persistent stream (REQUIREMENTS.md Section 5.1). |
| `api.credential` | *required* | secret | The Worker's credential for authenticating to the API (REQUIREMENTS.md NFR-SEC-1, FR-WRK-1). |
| `api.tls.ca_file` | *required¹* | | Path to the CA bundle used to verify the API's control-channel TLS (REQUIREMENTS.md NFR-SEC-1). |
| `api.tls.insecure` | `false` | | When `true`, dial the control channel in plaintext (no TLS). Local/dev only; the Worker logs a `WARN` at startup. Required to opt out of TLS when `api.tls.ca_file` is unset. |
| `api.tls.client_cert_file` | — | | **Deferred (M1).** Path to the Worker's client certificate, when the control channel uses mTLS. The API does not yet verify client certificates (it ships server-side TLS only; the `api.credential` authenticates the Worker), so this key is currently unused; it stays documented to keep the config shape forward-compatible. |
| `api.tls.client_key_file` | — | secret | **Deferred (M1).** Path to the Worker's client private key (mTLS). Unused until the API verifies client certificates (see `api.tls.client_cert_file`). |
| `worker.id` | *(auto)* | | Stable identifier the Worker registers under. **Must be a UUID**: the API persists a server's assigned worker as a UUID column, so registration rejects a non-UUID id with `INVALID_ARGUMENT` (and the Worker fails config load if an explicit `worker.id` is not a UUID). When unset, the Worker generates a UUIDv4 on first boot, persists it at `<worker.scratch_dir>/worker-id` (mode `0600`), and reuses it on later restarts so identity stays stable. **Upgrade impact:** a Worker that previously defaulted to its hostname gets a new UUID identity on first boot after upgrading, so the API sees a brand-new Worker and the old `assigned_worker_id` rows are orphaned (recovered via the disconnect/mark-unknown path; servers restart cleanly on hydrate). This is a one-time transition. |

¹ `api.tls.ca_file` is required **unless** `api.tls.insecure=true`. With neither
set, configuration validation fails fast at startup; with `api.tls.insecure=true`
the Worker dials plaintext (local/dev only). Production must set `api.tls.ca_file`.

### 6.2 Advertised capabilities

The Worker advertises its capabilities at registration (REQUIREMENTS.md
FR-WRK-1); these feed the API's greedy placement filter (FR-WRK-3).

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `worker.drivers` | *(required)* | | `ExecutionDriver` set this Worker offers (Section 4). `container` is the only shipped driver and it requires `driver.container.images`, so there is no zero-config default: the set must be supplied and must be non-empty. Advertised as a capability. |
| `worker.max_servers` | `0` | | Free-capacity hint for placement; `0` means "no advertised cap" at this scale. |

The concrete capability message on the wire is defined in `proto/` (#2); this
section fixes only what the operator configures.

### 6.3 Execution and scratch

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `worker.scratch_dir` | *required* | | Local scratch directory where the Worker hydrates a server's working set and runs it (REQUIREMENTS.md FR-DATA-4); the `WorkingDir` Port's root (ARCHITECTURE.md Section 5.2). |
| `driver.container.docker_host` | *(daemon default)* | | Docker daemon endpoint when the `container` driver is enabled. Only a `unix://` socket is supported in M1; empty uses the daemon's default socket. |
| `driver.container.images` | *(empty)* | | Map of Java **major version** to the base container image providing that JRE; the `container` driver picks the image matching a server's Minecraft version by the legacy version→major bracket logic (REQUIREMENTS.md FR-EXE-5, ARCHITECTURE.md Section 7.3). **Required** when `worker.drivers` advertises `container`. See below. |
| `driver.container.game_bind_ip` | `127.0.0.1` | | Host interface the `container` driver publishes each server's **game** port on. The default is loopback-only; set `0.0.0.0` to accept players from outside the host (the firewall then governs exposure). Must be a valid IP address. RCON always stays on loopback regardless of this value. |
| `driver.container.network` | *(empty)* | | User-defined Docker network the `container` driver attaches each MC container to. Empty (default) keeps the historical behavior: containers run on the default bridge and RCON is published to the host loopback. When set — the containerized-worker topology — the driver attaches MC containers to this network, **drops** the host RCON publication, and dials RCON at the container's name over the network (the network's container-name DNS resolves it). The game-port publication is unchanged either way. **Must be a *user-defined* network** (`docker network create …`): the default `bridge` has no container-name DNS, so the RCON dial would silently fail. The value is not validated against the daemon at config load. |

The `container` driver picks a base image per server: `driver.container.images`
maps a Java major version to a base image that provides that JRE (the server JAR
is bind-mounted from the scratch dir and run with the image's `java`). The Worker
maps a server's Minecraft version to a required Java major (legacy
[JAVA_COMPATIBILITY.md](https://github.com/mmiura-2351/mc-server-dashboard-api/blob/master/docs/app/JAVA_COMPATIBILITY.md)
reference) and selects the image for it; a version with no configured image fails
the launch.

```toml
[driver.container]
docker_host = "unix:///var/run/docker.sock"

[driver.container.images]
17 = "eclipse-temurin:17-jre"
21 = "eclipse-temurin:21-jre"
```

The environment-variable form is a comma-separated `major=image` list:
`MCD_WORKER_DRIVER_CONTAINER_IMAGES="17=eclipse-temurin:17-jre,21=eclipse-temurin:21-jre"`,
with `MCD_WORKER_DRIVER_CONTAINER_DOCKER_HOST` for the daemon endpoint.

The `container` driver names each container deterministically (`mcsd-<server_id>`)
and labels it with this Worker's id (`mcsd.worker.id`) and the server id
(`mcsd.server.id`). At startup it sweeps and force-removes every container
carrying its own worker-id label before launching any server, then removes a
container after the server exits. The sweep recovers from a crash that left a
server's container behind, but because it force-removes *running* containers too,
a graceful Worker restart while servers are up kills those live servers — this is
the deliberate M1 stateless-worker posture (no hydration of prior state yet; the
API observes the resulting state via the Worker's reconnect/status).

#### Per-server memory limit: per-driver guarantee

A server's `memory_limit_mb` (the operator-set per-server limit,
[`DATABASE.md`](DATABASE.md) Section 7) is carried end-to-end to the Worker, which
derives the JVM heap from it: `-Xms = -Xmx = limit − max(limit/5, 256 MiB)`,
reserving headroom for JVM off-heap/native overhead so the heap stays under the
limit (issue #706). **An unset limit leaves the heap at the JVM default** (the
pre-limit launch). The limit is also a **hard ceiling** the process cannot
exceed:

| Driver | Memory guarantee | Mechanism |
|---|---|---|
| `container` | **Hard ceiling — enforced.** The process cannot exceed the limit; the kernel OOM-kills it if it tries. | Docker `Memory` set to the limit, plus the derived `-Xmx` (added in issue #707). |

#### Per-server CPU allocation: per-driver guarantee

A server's `cpu_millis` (the operator-set per-server CPU allocation,
[`DATABASE.md`](DATABASE.md) Section 7) is carried end-to-end to the Worker as a
**soft relative share** — not a hard cap (owner decision). Unlike memory there is
no derived launch flag: CPU is enforced (if at all) by the driver, not the JVM
command line. **An unset allocation leaves the server at the driver's default
share.** What each driver does with it differs:

| Driver | CPU guarantee | Mechanism |
|---|---|---|
| `container` | **Soft relative share — enforced under contention only.** The allocation sets the server's CPU weight proportional to other containers; when the host is otherwise idle the server may burst above it. There is **no hard cap**. | Docker `CPUShares`, proportional to the allocation (issue #724). |

These guarantees cover memory and CPU; disk quotas remain deferred (epic
#704, REQUIREMENTS.md Section 2.2).

### 6.4 Observability

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `log.level` | `info` | | Log verbosity. |
| `log.format` | `json` | | Structured-log format (REQUIREMENTS.md NFR-OBS-1). |
| `worker.metrics_interval_seconds` | `15` | | Cadence at which the Worker samples each running server and emits a `Metrics` event (REQUIREMENTS.md FR-MON-3). `0` keeps the built-in default. |

The Worker captures each running server's console output and streams it to the
API as `LogLine` events (FR-MON-2), and samples basic per-server runtime metrics
(CPU and resident memory; CPU in thousandths of a core) on the
`worker.metrics_interval_seconds` cadence (FR-MON-3). The container driver reads
metrics from the Docker Engine stats endpoint. When a metric source is
unavailable (an unreachable daemon, an exited process) the Worker emits an
*up-only*
sample — the server id with zero stats — so the API still learns the server is
running.

Log streaming is **transient relay-only** at M1 (REQUIREMENTS.md Section 6.13):
the Worker streams lines as they are produced and does not persist them. To keep
log volume from backing up the control-plane stream, each server's capture uses a
bounded per-server buffer; under sustained backpressure the Worker drops the
oldest buffered line and emits a single marker line reporting how many lines were
dropped (consistent with the best-effort status-event posture, FR-MON-4). Lines
longer than 8 KiB are truncated with a marker.

---

## 7. Authentication hardening

These knobs implement REQUIREMENTS.md FR-AUTH-4 on the **API** side. The
bindings are the FR-AUTH-4 bullets (password strength, brute-force protection,
reverse-proxy trust). The defaults below are the proven baseline from the legacy
[SECURITY.md](https://github.com/mmiura-2351/mc-server-dashboard-api/blob/master/docs/app/SECURITY.md),
adopted as-is for M1; the legacy document remains a reference, not a binding
spec.

### 7.1 Password policy

Password strength is selected by a named preset rather than per-rule toggles
(issue #536). The preset fixes which strength rules fire and their thresholds;
the rejection reason codes are identical across presets (SECURITY.md Section 1),
so a Web UI built against them keeps working when the operator changes the
preset.

| Key | Default | Meaning |
|---|---|---|
| `auth.password.policy` | `middle` | Strength preset: `low` / `middle` / `high`. Any other value fails fast at load. See the preset table in [SECURITY.md](SECURITY.md) Section 1 for the rules each fires. Env: `MCD_API_AUTH__PASSWORD__POLICY`. |
| `auth.password.max_length` | `128` | Maximum length (bcrypt 72-byte cap plus a DoS guard); independent of the preset. Must be positive and **not below** the selected preset's minimum length; a max below the preset minimum fails fast at load. |

Preset thresholds (full table in [SECURITY.md](SECURITY.md) Section 1):

| Preset | Min length | Complexity-or-length | Common-list | User-info | Simple-pattern |
|---|---|---|---|---|---|
| `low` | 8 | off | on | on | off |
| `middle` *(default)* | 10 | 2 of 4 (or 16+ chars) | on | on | on |
| `high` | 12 | 3 of 4 (or 16+ chars) | on | on | on |

The default is `middle`. It changed from the historical fixed posture, which was
equivalent to `high`. Because the policy only validates *newly set* passwords,
existing hashes are unaffected; set `auth.password.policy=high` to keep the old
posture.

> **Breaking change (upgrading from per-rule config):** the previous per-rule
> keys were removed in favour of the preset above. If your config still sets any
> of `auth.password.min_length`, `auth.password.require_complexity`,
> `auth.password.check_common_list`, `auth.password.forbid_user_info`, or
> `auth.password.forbid_simple_patterns` (or their `MCD_API_AUTH__PASSWORD__*`
> env forms), **remove them** and select a preset instead. The `auth.password`
> section forbids unknown keys, so leaving any of them set fails fast with a
> startup validation error. To keep the old strictness, choose
> `auth.password.policy=high`, which reproduces the previous default posture.

### 7.2 Brute-force protection

Per-username and per-IP failure thresholds over sliding windows, lockout with
exponential back-off, and an artificial failure delay against timing-based
enumeration (REQUIREMENTS.md FR-AUTH-4).

| Key | Default | Meaning |
|---|---|---|
| `auth.brute_force.enabled` | `true` | Master switch for brute-force protection. |
| `auth.brute_force.username_threshold` | `5` | Failures per username before lockout. Must be at least 1. |
| `auth.brute_force.username_window_seconds` | `900` | Sliding window for the per-username count. Must be positive. |
| `auth.brute_force.ip_threshold` | `20` | Failures per source IP before throttling. Must be at least 1. |
| `auth.brute_force.ip_window_seconds` | `300` | Sliding window for the per-IP count. Must be positive. |
| `auth.brute_force.lockout_base_seconds` | `900` | Initial lockout duration; doubles on repeat (exponential back-off). Must be positive and **not exceed** `lockout_max_seconds`; a base above the cap fails fast at load. |
| `auth.brute_force.lockout_max_seconds` | `86400` | Cap on the backed-off lockout duration. Must be positive. |
| `auth.brute_force.delay_ms` | `200` | Artificial delay added on a failed attempt to deny timing enumeration. Must be non-negative; `0` explicitly disables the delay (forgoing the timing-uniformity guarantee). |
| `auth.brute_force.prune_interval_seconds` | `3600` | How often the background loop prunes `login_attempt` rows older than the longest window, independent of logins (SECURITY.md Section 3). Must be positive. |

### 7.3 Reverse-proxy trust

Forwarded client IPs are honored **only** from explicitly trusted proxy peers
(REQUIREMENTS.md FR-AUTH-4); the per-IP brute-force counter depends on a
trustworthy client IP.

| Key | Default | Meaning |
|---|---|---|
| `auth.proxy.trust_forwarded_headers` | `false` | Whether to read the forwarded-for header at all. |
| `auth.proxy.trusted_proxies` | *(empty)* | List of proxy peer IPs/CIDRs; the forwarded header is honored only when the immediate peer is on this list. |

### 7.4 Open-registration abuse controls

`POST /users` is unauthenticated open registration (REQUIREMENTS.md FR-AUTH-1).
These knobs cover the abuse surface FR-AUTH-4's authentication hardening does not
(issue #362): a master switch to close self-registration on an admin-provisioned
deployment, and a per-IP cap that reuses the FR-AUTH-4 per-IP sliding-window
counter and the Section 7.3 trusted-proxy client-IP resolution (no parallel
mechanism). The legacy SECURITY.md baseline prescribes no registration limits, so
the defaults below are taken consistent with the per-IP **login** throttle
(Section 7.2): registration is a rarer, more deliberate action than a login, so
the threshold is lower and the window wider.

| Key | Default | Meaning |
|---|---|---|
| `auth.registration.open` | `true` | Master switch for self-registration. `false` returns `403` from the unauthenticated `POST /users`; a platform admin can still provision accounts via `POST /admin/users` (issue #368), which is exempt from this switch and the per-IP cap below. **First-user bootstrap exception (issue #909):** on a fresh database with no users yet, the *first* `POST /users` registration is allowed regardless of this flag and is auto-granted platform admin — the only way to create the bootstrap admin without a config flip. The flag is enforced normally for every registration once any user exists. |
| `auth.registration.ip_limit_enabled` | `true` | Whether the per-IP registration cap is enforced. |
| `auth.registration.ip_threshold` | `5` | Accepted registrations per source IP within the window before further attempts get `429`. Must be at least 1. |
| `auth.registration.ip_window_seconds` | `3600` | Sliding window for the per-IP registration count. Must be positive. |

The cap counts by source IP, so legitimate registrants sharing one egress IP (a
NAT or corporate gateway) draw from the same window: once `ip_threshold` attempts
from that IP are accepted within the window, the next is `429`'d even when each is
genuine. Only attempts that pass the gate count toward the window; a throttled
(`429`) attempt is **not** recorded, so a stream of rejected retries does not
re-arm the window. The block lifts once the `ip_threshold`-th accepted attempt
ages out of the window — `ip_window_seconds` after it, not after the last `429`
(issue #370). Raise `ip_threshold` or widen who is trusted (Section 7.3) where a
deployment expects shared egress.

---

## 8. Snapshot cadence

Snapshot cadence is **periodic-with-per-server-override plus event-driven**
(REQUIREMENTS.md FR-DATA-7, Section 9 decision #4). Configuration covers the two
periodic dimensions; event-driven snapshots are behavioural, not configured.

- **Global default interval** — `snapshot.default_interval_seconds` (Section
  5.4) applies to every running server.
- **Per-server override** — each server may carry its own interval, stored on
  the `Server` entity (REQUIREMENTS.md Appendix B) and edited through the
  server-configuration API, **not** through this static configuration file. An
  override is clamped to at least `snapshot.min_interval_seconds`.
- **Event-driven snapshots** — taken on graceful stop and on-demand backup
  (REQUIREMENTS.md FR-DATA-7, Section 6.11) regardless of the interval; they have
  no configuration key.

The effective periodic interval for a running server is its per-server override
if set, otherwise the global default. The interval bounds the RPO: a crash may
lose up to one interval of changes (REQUIREMENTS.md FR-DATA-5).

Scheduled backups no longer follow this config-key pattern: the legacy
`backup_interval_hours` cadence is retired (issue #1840). A backup is now a
first-class `backup` schedule on the general scheduler (DATABASE.md Section 8),
polled at `schedule.tick_seconds` (Section 5.14) like every other schedule.

---

## 9. Related documents

| Doc | Covers |
|---|---|
| [`../REQUIREMENTS.md`](../REQUIREMENTS.md) | What v2 must do; the source of truth for scope. |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Hexagonal layering, module boundaries, the Ports catalog these keys select adapters for, and wiring at the edge. |
| [`DATABASE.md`](DATABASE.md) | Persistence model behind `database.url`. |
| [`STORAGE.md`](STORAGE.md) | `Storage` adapter contracts and the per-backend keys behind `storage.backend`. |
| [`CONTROL_PLANE.md`](CONTROL_PLANE.md) | The control-plane messages, including Worker capability advertisement. |
| [`../dev/CONTRIBUTING.md`](../dev/CONTRIBUTING.md) | The change workflow for editing these docs. |
