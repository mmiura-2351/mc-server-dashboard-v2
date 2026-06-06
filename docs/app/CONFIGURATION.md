# Configuration

> Status: **Design** · Audience: contributors and operators of `api/` and
> `worker/`
>
> This document defines the **runtime configuration surface** of v2 and the
> **config-driven adapter selection** mechanism. It refines, but does not
> contradict, [`../REQUIREMENTS.md`](../REQUIREMENTS.md) and
> [`ARCHITECTURE.md`](ARCHITECTURE.md); where they disagree, the requirements
> win and this document is wrong.
>
> Scope is the *configuration contract* — which keys exist, what they select,
> and their defaults — not how configuration is loaded. The loader lands with
> the implementation (epic #3+). Key names below are the agreed surface; minor
> renames during implementation are acceptable as long as the grouping and
> selection semantics hold.

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
- **Two independent processes.** `api/` (Python) and `worker/` (Go) are
  configured independently; each has its own configuration. `proto/` is a build
  artifact and has no runtime configuration.

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
- **Config file** — a single file per service (e.g. `api.toml` /
  `worker.toml`; the concrete format is fixed when the loader lands). Holds the
  non-secret bulk of configuration and is the recommended place for
  adapter-selection and tuning keys.
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
group. The exact prefix is confirmed when the loader lands.

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
  service does not fall back to an insecure default.

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
| `ExecutionDriver` (FR-EXE-2) | `worker.drivers` | subset of `host-process` / `container` | worker |

Notes:

- The Worker's `ExecutionDriver` selection is a **set**, not one value: a Worker
  advertises *which* drivers it offers (Section 6), and the API's greedy
  placement filters Workers by the driver a server needs (REQUIREMENTS.md
  FR-WRK-3). Selecting `host-process` only, `container` only, or both is a
  per-Worker configuration choice.
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
| `control.enabled` | `true` | | Whether the API hosts the control-plane gRPC server in this process (REQUIREMENTS.md Section 5.1). When `true`, `control.worker_credential` is required (fail-fast). |
| `control.worker_credential` | *required when enabled* | secret | Shared credential a Worker must present (`authorization: Bearer <credential>` metadata) to authenticate its stream (REQUIREMENTS.md NFR-SEC-1); the API-side counterpart of the Worker's `api.credential` (Section 6.1). |
| `control.tls.cert_file` | *required²* | | Path to the control-channel TLS server certificate the gRPC listener presents (REQUIREMENTS.md NFR-SEC-1). |
| `control.tls.key_file` | *required²* | secret | Path to the control-channel TLS private key. |
| `control.tls.insecure` | `false` | | When `true`, bind the control-plane gRPC listener in plaintext (no TLS). Local/dev only; the API logs a `WARN` at startup. Required to opt out of TLS when no cert/key pair is set. |
| `control.tls.client_ca_file` | — | | **Deferred (M1).** Reserved for client-certificate (mTLS) verification of the Worker. Unused today — the shared `control.worker_credential` authenticates the Worker (NFR-SEC-1), so M1 ships server-side TLS only. Documented to keep the config shape forward-compatible. |
| `control.heartbeat_timeout_seconds` | `30` | | Liveness window: a Worker missing heartbeats past this is marked disconnected (REQUIREMENTS.md FR-WRK-2). Must be positive. |
| `control.command_timeout_seconds` | `30` | | Deadline for a dispatched `ApiCommand` to be answered by a `CommandResult`; an unanswered command is treated as a failure (CONTROL_PLANE.md Section 4.2). Must be positive. |

² `control.tls.cert_file` and `control.tls.key_file` are required **together,
unless** `control.tls.insecure=true`. With neither the cert/key pair nor
`insecure=true` set — or with only one of cert/key set — `create_app` fails fast
at startup; with `control.tls.insecure=true` the listener binds plaintext
(local/dev only) and logs a `WARN`. Production must set the cert/key pair (or
terminate TLS at a reverse proxy and run the listener `insecure=true` behind it
— see CONTROL_PLANE.md Section 2). This mirrors the Worker's `api.tls.*`
required-unless-insecure rule (Section 6.1): the Worker verifies this
certificate against its `api.tls.ca_file`.

> **Upgrade impact (existing dev setups).** This is a behavior change: a dev
> process with `control.enabled=true` that previously bound plaintext now fails
> fast at startup unless it sets `control.tls.insecure=true` (or supplies a
> cert/key pair). Add `control.tls.insecure=true` to local/dev configs.

### 5.2 Persistence and Storage adapter

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `database.url` | *required* | secret | Connection string for the persistence adapter (may embed credentials). Model owned by DATABASE.md (#15). |
| `storage.backend` | `fs` | | Selector for the `Storage` Port (Section 4): `fs` / `remote-fs` / `object`. |
| `storage.fs.root` | `./data` | | Root directory when `storage.backend = fs`. |
| `storage.version_retention` | `10` | | Maximum per-file prior versions retained for rollback; the oldest beyond this count are pruned (STORAGE.md Section 5). Must be non-negative; `0` retains no prior versions. |
| `storage.remote_fs.*` | — | partly | Mount/endpoint settings when `storage.backend = remote-fs`; secret members masked. Detail in STORAGE.md (#17). |
| `storage.object.endpoint` | — | | Object-store endpoint when `storage.backend = object`. |
| `storage.object.bucket` | — | | Object-store bucket/container. |
| `storage.object.access_key` | — | secret | Object-store access key. |
| `storage.object.secret_key` | — | secret | Object-store secret key. |

Only the keys for the selected `storage.backend` are read; the rest are ignored
(Section 4). The authoritative per-backend key list and the atomic-snapshot
publish behaviour (REQUIREMENTS.md FR-DATA-6) live in STORAGE.md (#17).

### 5.3 Authentication: tokens and password hashing

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `auth.token.algorithm` | `HS256` | | Signing algorithm of the `TokenService` JWT adapter (REQUIREMENTS.md FR-AUTH-2). One of `HS256` / `RS256` (case-sensitive); any other value fails fast at load. A parameter of the adapter, not an adapter selector (Section 4). |
| `auth.token.signing_key` | *required* | secret | Signing key/secret for access & refresh tokens (REQUIREMENTS.md FR-AUTH-2). For an asymmetric algorithm this is the private key (path or value). Under `HS256` the key is shared-secret entropy and **must be at least 32 bytes** (the 256-bit digest length); a shorter key fails fast at load. |
| `auth.token.access_ttl_seconds` | `900` | | Short-lived access-token lifetime. Must be positive and **strictly less than** `refresh_ttl_seconds` (an access token may not outlive the refresh token); a non-conforming pair fails fast at load. |
| `auth.token.refresh_ttl_seconds` | `1209600` | | Long-lived refresh-token lifetime (14 days). Must be positive. Also the `Max-Age` of the refresh cookie below. |
| `auth.token.refresh_cookie_name` | `mcd_refresh` | | Name of the httpOnly refresh-token cookie set on `POST /auth/login` for the Web UI session (issue #363, WEBUI_SPEC.md Section 7.1). The cookie is always `HttpOnly; SameSite=Strict; Path=/auth`; `Max-Age` tracks `refresh_ttl_seconds`. Body-based clients (worker/CLI) ignore it. Must be non-empty. |
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

The API drives scheduled backups (REQUIREMENTS.md FR-BAK-3). The per-server
schedule itself is a server-config key edited through the server-configuration
API (`backup_interval_hours`, see Section 8); this only tunes how often the
background scheduler wakes to check which servers are due.

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `backup.schedule_tick_seconds` | `300` | | Loop resolution of the scheduled-backup scheduler: how often it wakes to check which servers are due. Coarse, since backup cadence is measured in hours. Must be positive. |

### 5.6 Divergence reconciler

The API runs a background reconciler that re-dispatches durable-but-unsent
lifecycle intent so a desired/observed divergence converges (REQUIREMENTS.md
FR-SRV-3/4). Two windows the in-line lifecycle path leaves open are closed here:
a start/stop committed just before a crash (never dispatched) and a
compensation-failure orphan (`desired=running` with no assigned Worker). The loop
is gated on the control plane like the snapshot/backup schedulers — with no Worker
channel there is nothing to re-dispatch.

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `reconciler.interval_seconds` | `60` | | Loop resolution: how often the reconciler scans for diverged servers. Must be positive. |
| `reconciler.grace_seconds` | `120` | | How long a divergence must persist (measured from the last Worker report) before it is acted on, so the normal in-flight lifecycle path has time to converge first. Must be positive. |
| `reconciler.backoff_base_seconds` | `30` | | Base of the per-server exponential backoff after a failed re-dispatch; the wait doubles per consecutive failure. Must be positive. |
| `reconciler.backoff_max_seconds` | `3600` | | Cap on the per-server backoff wait. Must be positive and `>=` `backoff_base_seconds`. |

### 5.7 JAR-pool garbage collection

The API runs a background reference-counted GC that reclaims pooled server JARs
no live server row references (STORAGE.md Section 3.2). It is gated on the control
plane like the snapshot/backup/reconciler loops, and a platform admin can also
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

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `ports.range_start` | `25565` | | Lowest assignable game port (inclusive). Must be `1..65535`. |
| `ports.range_end` | `25664` | | Highest assignable game port (inclusive). Must be `1..65535` and `>=` `range_start`. |

### 5.9 Observability

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `log.level` | `info` | | Log verbosity. |
| `log.format` | `json` | | Structured-log format; `json` keeps logs machine-parseable (REQUIREMENTS.md NFR-OBS-1). |

---

## 6. Worker configuration

The Worker is stateless and replaceable (REQUIREMENTS.md FR-WRK-4); its
configuration tells it **where the API is**, **how to authenticate**, **what it
can run**, and **where its scratch space is**.

### 6.1 API connection and authentication

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `api.grpc_endpoint` | *required* | | Address of the API control-plane gRPC server the Worker dials to open its persistent stream (REQUIREMENTS.md Section 5.1). |
| `api.data_plane_url` | *required* | | Base URL of the API's HTTP data-plane endpoint for hydrate/snapshot transfer (REQUIREMENTS.md FR-DATA-3). May be discovered from the API at registration; this key is the fallback/override. |
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
| `worker.drivers` | `host-process` | | `ExecutionDriver` set this Worker offers (Section 4): any subset of `host-process` / `container`. Advertised as a capability. |
| `worker.max_servers` | `0` | | Free-capacity hint for placement; `0` means "no advertised cap" at this scale. |

The concrete capability message on the wire is defined in `proto/` (#2); this
section fixes only what the operator configures.

### 6.3 Execution and scratch

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `worker.scratch_dir` | *required* | | Local scratch directory where the Worker hydrates a server's working set and runs it (REQUIREMENTS.md FR-DATA-4); the `WorkingDir` Port's root (ARCHITECTURE.md Section 5.2). |
| `worker.java.runtimes` | *(empty)* | | Map of Java **major version** to the `java` binary path for it; the `JavaRuntimeSelector` picks the entry matching a server's Minecraft version (REQUIREMENTS.md FR-EXE-5, ARCHITECTURE.md Section 7.3). See below. |
| `driver.container.docker_host` | *(daemon default)* | | Docker daemon endpoint when the `container` driver is enabled. Only a `unix://` socket is supported in M1; empty uses the daemon's default socket. |
| `driver.container.images` | *(empty)* | | Map of Java **major version** to the base container image providing that JRE; the `container` driver picks the image matching a server's Minecraft version by the same bracket logic as `worker.java.runtimes`. **Required** when `worker.drivers` advertises `container`. See below. |
| `driver.container.game_bind_ip` | `127.0.0.1` | | Host interface the `container` driver publishes each server's **game** port on. The default is loopback-only; set `0.0.0.0` to accept players from outside the host (the firewall then governs exposure). Must be a valid IP address. RCON always stays on loopback regardless of this value. |
| `driver.container.network` | *(empty)* | | User-defined Docker network the `container` driver attaches each MC container to. Empty (default) keeps the historical behavior: containers run on the default bridge and RCON is published to the host loopback. When set — the containerized-worker topology — the driver attaches MC containers to this network, **drops** the host RCON publication, and dials RCON at the container's name over the network (the network's container-name DNS resolves it). The game-port publication is unchanged either way. **Must be a *user-defined* network** (`docker network create …`): the default `bridge` has no container-name DNS, so the RCON dial would silently fail. The value is not validated against the daemon at config load. |
| `java.install_dir` | *(auto-discover)* | | Directory of installed Java runtimes for future auto-discovery of `worker.java.runtimes`; not yet implemented. |

The `worker.java.runtimes` map keys are Java major versions; values are absolute
paths to the matching `java` binary. The Worker maps a server's Minecraft version
to a required Java major (legacy
[JAVA_COMPATIBILITY.md](https://github.com/mmiura-2351/mc-server-dashboard-api/blob/master/docs/app/JAVA_COMPATIBILITY.md)
reference) and launches that runtime; a version with no configured runtime fails
the launch.

```toml
[worker.java.runtimes]
8  = "/usr/lib/jvm/temurin-8/bin/java"
17 = "/usr/lib/jvm/temurin-17/bin/java"
21 = "/usr/lib/jvm/temurin-21/bin/java"
```

The environment-variable form is a comma-separated `major=path` list:
`MCD_WORKER_WORKER_JAVA_RUNTIMES="17=/jvm/17/bin/java,21=/jvm/21/bin/java"`.

The `container` driver mirrors this: `driver.container.images` maps a Java major
version to a base image that provides that JRE (the server JAR is bind-mounted
from the scratch dir and run with the image's `java`). The version→major mapping
is shared with `worker.java.runtimes`, so a server runs on the same Java major
whether it executes as a host process or in a container. A version with no
configured image fails the launch.

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
API observes the resulting state via the Worker's reconnect/status). Resource
limits (CPU/memory quotas) are deferred to M2+ (REQUIREMENTS.md Section 2.2).

### 6.4 Observability

| Key | Default | Secret | Meaning |
|---|---|---|---|
| `log.level` | `info` | | Log verbosity. |
| `log.format` | `json` | | Structured-log format (REQUIREMENTS.md NFR-OBS-1). |
| `worker.metrics_interval_seconds` | `15` | | Cadence at which the Worker samples each running server and emits a `Metrics` event (REQUIREMENTS.md FR-MON-3). `0` keeps the built-in default. |

The Worker captures each running server's console output and streams it to the
API as `LogLine` events (FR-MON-2), and samples basic per-server runtime metrics
(CPU and resident memory; CPU in thousandths of a core) on the
`worker.metrics_interval_seconds` cadence (FR-MON-3). The host-process driver
reads metrics from `/proc/<pid>` (Linux only); the container driver reads the
Docker Engine stats endpoint. When a metric source is unavailable (a non-Linux
host, an unreachable daemon, an exited process) the Worker emits an *up-only*
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

| Key | Default | Meaning |
|---|---|---|
| `auth.password.min_length` | `12` | Minimum password length (characters). Must be positive and **not exceed** `max_length`; a min above the max fails fast at load. |
| `auth.password.max_length` | `128` | Maximum length (bcrypt 72-byte cap plus a DoS guard). Must be positive. |
| `auth.password.require_complexity` | `true` | Enforce the complexity-or-length rule: at least 3 of {upper, lower, digit, symbol} **or** at least 16 characters. |
| `auth.password.check_common_list` | `true` | Reject passwords on a common-password blocklist (legacy baseline: SecLists xato-net top-10,000). |
| `auth.password.forbid_user_info` | `true` | Reject a password containing the username or the email local-part. |
| `auth.password.forbid_simple_patterns` | `true` | Reject 4+ repeated characters or 4+ sequential alphabet/keyboard/numeric runs. |

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
| `auth.registration.open` | `true` | Master switch for self-registration. `false` returns `403` from `POST /users`; admin-created accounts via the admin surface are unaffected. |
| `auth.registration.ip_limit_enabled` | `true` | Whether the per-IP registration cap is enforced. |
| `auth.registration.ip_threshold` | `5` | Registrations per source IP within the window before further attempts get `429`. Must be at least 1. |
| `auth.registration.ip_window_seconds` | `3600` | Sliding window for the per-IP registration count. Must be positive. |

The cap counts by source IP, so legitimate registrants sharing one egress IP (a
NAT or corporate gateway) draw from the same window: the `ip_threshold + 1`-th in a
window gets `429` even when each is genuine. This matches the login per-IP posture
(Section 7.2); raise `ip_threshold` or widen who is trusted (Section 7.3) where a
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

Scheduled backups follow the same per-server pattern: each server may carry a
`backup_interval_hours` schedule on its `Server` config blob (DATABASE.md
Section 8), edited through the server-configuration API rather than this static
file. It must be a positive integer; the scheduler wakes every
`backup.schedule_tick_seconds` (Section 5.5) to back up servers whose schedule
is due.

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
