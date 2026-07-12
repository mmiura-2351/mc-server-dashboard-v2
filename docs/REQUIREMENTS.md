# Minecraft Server Dashboard v2 — Requirements

> Status: **Draft (open questions resolved)** · Phase: requirements definition · Date: 2026-06-03
>
> This is a greenfield rebuild of the Minecraft Server Dashboard. Backward
> compatibility with the legacy codebase (`mc-server-dashboard-api`) is
> explicitly abandoned; the legacy system is reference-only. This document
> defines *what* the system must do and the architectural constraints it must
> satisfy. It does not prescribe the final module layout — that is design work
> that follows from these requirements.
>
> **Versioning terms used in this document:**
> - **legacy** / **the legacy system** — the existing `mc-server-dashboard-api`
>   implementation being replaced. Never called "v1" here, to avoid ambiguity.
> - **v2** — this rebuild as a whole (the product name).
> - **M1** — the **first milestone** (initial shippable scope) of v2. Phrases
>   like "in M1" or "M1 implements …" describe that initial scope. Deferred work
>   targets later milestones (M2, M3, …).

## Table of Contents

1. [Background & Goals](#1-background--goals)
2. [Scope](#2-scope)
3. [Terminology](#3-terminology)
4. [Actors & Roles](#4-actors--roles)
5. [System Architecture Overview](#5-system-architecture-overview)
6. [Functional Requirements](#6-functional-requirements)
7. [Non-Functional Requirements](#7-non-functional-requirements)
8. [Architecture Principles](#8-architecture-principles)
9. [Resolved Design Decisions](#9-resolved-design-decisions)
- [Appendix A — Operation permission catalog](#appendix-a--operation-permission-catalog-draft)
- [Appendix B — Core entities](#appendix-b--core-entities-sketch)

---

## 1. Background & Goals

The legacy system runs the API server and every Minecraft server process on the
same machine. Minecraft processes are spawned as local subprocesses (double-fork
daemon model), and world data, JARs, and backups live on the API host's local
filesystem. Authorization is role-based with a fixed role set and no notion of
multi-tenancy.

This rebuild exists to remove those structural limits. The three drivers:

1. **Separate the API machine from the Minecraft execution machine.** Minecraft
   processing is delegated to **Workers** running on separate machines.
2. **Make the Minecraft execution method pluggable.** The way a server is run
   is abstracted behind a driver interface (`ExecutionDriver`), so new
   execution methods can be added without touching business logic. Container
   (Docker) is the only shipped driver.
3. **Rework authentication and authorization.** Move to a **Community**-based
   architecture (a logical multi-tenancy for small communities) with
   **per-operation permissions**.

### 1.1 Design posture

Target scale is **small** (single-digit to a few dozen Communities, on the order
of tens of concurrently running servers). The system is therefore designed for
**simplicity first**, while keeping the right abstractions so that larger scale
can be added later without a rewrite:

- No server-placement scheduler in M1 (simple Worker assignment is enough).
- No message broker in M1 (a direct Worker→API channel suffices).
- A single API instance is acceptable.
- The abstractions (`ExecutionDriver`, `Storage`, `PermissionChecker`, the
  control-plane channel) must not assume small scale in their *shape*, only in
  their M1 *implementation*.

### 1.2 Repository topology

The system ships as a **monorepo** containing `api/`, `worker/`, a shared
`proto/` package (the gRPC/protobuf control-plane contract), and `webui/` (the
Web UI, added with the UI milestone — see docs/ui/WEBUI_SPEC.md). A single repo
keeps the API and Worker in lock-step on the shared protocol so a contract
change and both sides land in one change set, and keeps the UI in lock-step
with the API surface it consumes.

---

## 2. Scope

### 2.1 In scope (M1)

- Community management and membership (many-to-many users ↔ Communities).
- Authentication, and authorization via custom roles + per-resource grants.
- Minecraft server lifecycle (create, configure, start, stop, restart, delete).
- One execution backend: **container (Docker)**.
- Worker registration, liveness, and server assignment.
- Pluggable authoritative storage (fs / remote-fs / object) on the API side.
- Runtime data staging between authoritative storage and stateless Workers.
- File management, backup management, version management.
- Real-time monitoring (status, logs).
- Audit logging.
- A platform-administrator role above all Communities.

### 2.2 In scope (M2)

M2's theme is **the use-case-enabling foundation**: every intended user use case
can be executed end-to-end through the API. The Web UI is a separate track that
consumes this API surface — it is not part of M2's pillars (which cover the
API + Worker) and lives in `webui/` in this monorepo (see Section 1.2; design
in docs/ui/WEBUI_SPEC.md).

M2 is organized as four pillars, each tracked by an epic:

- **Pillar A — account self-service and admin lifecycle** (epic #239): users
  manage their own account (password change, profile update, account deletion);
  platform administrators manage users via the API (list,
  deactivate/reactivate, delete, grant/revoke administrator) instead of direct
  SQL. Identity stays global (FR-AUTH-5); every mutation is audited.
- **Pillar B — server-operation use cases complete end-to-end** (epic #240):
  EULA acceptance flow, port management (free-port discovery and
  auto-assignment), force stop on the HTTP API, additional server types (fabric
  and forge are added), and
  sanitized start-failure categories.
- **Pillar C — data and content management** (epic #241): extended file
  operations, server ZIP import/export, backup upload/download (off-host), and
  OP / whitelist player groups synced to the server.
- **Pillar D — operations and long-run robustness** (epic #242): Prometheus
  metrics and a readiness endpoint, a system notification stream, JAR pool
  garbage collection, and version-catalog admin.

### 2.3 Explicitly dropped (not planned)

These are **dropped**, not deferred: they are not required to cover the user use
cases and are not on the roadmap. (Distinct from the M3+ deferred list in
Section 2.4, which is still planned.)

- Kubernetes execution backend. The `ExecutionDriver` abstraction stays
  k8s-compatible (FR-EXE-4), but there is no commitment to build a Kubernetes
  driver.
- Billing / metering.
- Email invitations / invite links (membership is manual add; FR-MEM-1).
- Self-service Community creation (Communities are admin-created; FR-COMM-2).
- Server live-migration without downtime (relocation is stop → snapshot →
  hydrate → start).
- Horizontal scaling of the API tier, message-broker transport.

### 2.4 Deferred to M3+ (still planned)

Out of scope for M2 but kept on the roadmap for a later milestone:

- Per-Community quotas / resource limits.
- Continuous delta replication of world data (snapshot-based sync until then).
- Worker-restart container adoption (#102).
- Fleet-level start idempotency (#182).
- Lane capacity (#169).

---

## 3. Terminology

| Term | Definition |
|---|---|
| **Community** | A set of users that owns and isolates a collection of resources (servers, data). The legacy system's "tenant"-style concept, named for community-scale use, not corporate use. The unit of resource isolation, user management, and visibility. |
| **Member** | A user who belongs to a Community. Membership is many-to-many. |
| **Platform administrator** | An operator-level role above all Communities. Manages Workers, global monitoring, and Community provisioning. Distinct from any Community role. |
| **Worker** | A stateless machine/agent that runs and manages Minecraft execution environments. Holds no authoritative data; replaceable at any time. |
| **ExecutionDriver** | The abstraction inside a Worker that knows *how* to run a server. The API issues logical commands; the driver realizes them. Container (Docker) is the only shipped driver. |
| **Storage** | The pluggable, API-side authoritative store for world data, JARs, and backups (fs / remote-fs / object). |
| **Control plane** | The lightweight, persistent command/event channel between API and Worker. |
| **Data plane** | The bulk transfer path (hydrate / snapshot) of world data, API-terminated. |
| **Hydrate** | Copying a server's working set from authoritative storage onto a Worker before launch. |
| **Snapshot** | Copying a server's working set from a Worker back to authoritative storage. |
| **Resource grant** | A permission scoped to a specific resource (e.g. a single server) given to a specific member. |
| **Port** | A technology-agnostic interface defined by the domain (Hexagonal / Ports & Adapters). Business logic depends on Ports, never on concrete technologies. E.g. `Storage`, `PermissionChecker`. See section 8. |
| **Adapter** | A concrete implementation of a Port for a specific technology (e.g. an object-storage adapter for the `Storage` Port). Selected by configuration at the edge. |
| **RPO** | Recovery Point Objective — the maximum amount of recent data change that may be lost on failure (here, bounded by the snapshot interval). |

---

## 4. Actors & Roles

1. **Platform administrator** — operates the deployment. Registers/decommissions
   Workers, monitors the fleet, provisions Communities. Not bound to a single
   Community.
2. **Community owner** — full authority within a Community; manages roles,
   members, and all resources. Seeded as a preset role on Community creation.
3. **Community member (custom roles)** — any user in a Community whose
   capabilities are defined by the Community's roles plus any resource grants.
4. **Unauthenticated client** — may only reach authentication endpoints.

A single user account may simultaneously be a member of multiple Communities,
holding different roles in each, and may also be a platform administrator.

---

## 5. System Architecture Overview

```
        ┌────────────────────────── API server (authoritative) ──────────────────────────┐
        │  Business logic · Authentication · Authorization · Communities                  │
        │  Storage Port ──▶ [ fs | remote-fs | object ] adapter (config-selected)          │
        │  Worker registry (tracks connected Workers)                                      │
        │  Control plane (commands) + Data plane (hydrate / snapshot transfer)             │
        └───────────────▲────────────────────────────────────────────┬────────────────────┘
                        │ Worker-initiated persistent channel (gRPC) │ API-mediated data
              commands ↓│↑ events (status / log / metrics)             │ transfer (world/jar/backup)
        ┌───────────────┴────────────────────────────────────────────▼────────────────────┐
        │  Worker (stateless / replaceable)                                                 │
        │  ExecutionDriver ──▶ [ container | (k8s-ready) ]                                 │
        │  Runs MC in a local scratch working dir; manages it (RCON, signals)               │
        │  Holds no authoritative data: hydrate on placement, snapshot on stop / interval   │
        └───────────────────────────────────────────────────────────────────────────────────┘
```

### 5.1 Connection model — Worker-initiated agent channel

- The Worker initiates and maintains a **persistent channel** to the API over a
  **gRPC bidirectional stream**. A single stream multiplexes **API→Worker
  commands** (start, stop, restart, RCON, hydrate/snapshot triggers) and
  **Worker→API events** (status changes, log lines, metrics, heartbeat). The
  typed protobuf contract lives in the shared `proto/` package (see 1.2).
- Rationale: Workers can be added/removed dynamically with no inbound exposure
  on the Worker side; liveness is detected by heartbeat; log/metric streaming
  flows naturally over the same channel. This matches the stateless,
  replaceable nature of Workers.
- Workers are operated by the same operator as the API (inside the trust
  boundary). Worker sandboxing is a **resource-isolation** concern, not a
  security boundary.

### 5.2 Two planes

- **Control plane**: lightweight, always-on, low-latency. Commands and events.
- **Data plane**: heavyweight, intermittent. Hydrate and snapshot transfers over
  an **API-terminated HTTP endpoint**, separate from the gRPC control stream so
  large transfers do not block control traffic. **API-mediated** (see 6.8): the
  Worker is storage-backend-agnostic; the API is the only component that talks to
  the pluggable Storage. Both planes remain API-terminated.

---

## 6. Functional Requirements

### 6.1 Identity & Authentication

- FR-AUTH-1: Users register and authenticate against the API.
- FR-AUTH-2: Authentication issues short-lived access tokens and long-lived
  refresh tokens (JWT or equivalent), behind a token-service abstraction so the
  format can evolve.
- FR-AUTH-3: Passwords are hashed (bcrypt/argon2) with per-user salt; refresh
  tokens are invalidated on logout.
- FR-AUTH-4: The API enforces configurable authentication-hardening controls:
  - **Password strength**: minimum length, a complexity-or-length rule, a
    common-password blocklist, and rejection of passwords containing the
    username/email.
  - **Brute-force protection**: per-username and per-IP failure thresholds over
    sliding windows, account lockout with exponential back-off, and an
    artificial delay on failures to deny timing-based enumeration.
  - **Reverse-proxy trust**: forwarded client IPs are honored only from
    explicitly trusted proxy peers.

  The proven baseline for the exact defaults and overrides is the legacy
  repository's
  [docs/app/SECURITY.md](https://github.com/mmiura-2351/mc-server-dashboard-api/blob/master/docs/app/SECURITY.md);
  M1 may adopt those values as-is. (Reference only — the bullets above are the
  binding requirement.)
- FR-AUTH-5: A user account is global (not owned by a Community) so the same
  identity can join multiple Communities.
- FR-AUTH-6: The platform-administrator role uses the **same identity system**
  as ordinary users, distinguished by an administrator role/flag. There is no
  separate admin authentication mechanism.
- FR-AUTH-7: **Admin bootstrap.** The first user created on a fresh deployment
  (when no user exists yet) automatically becomes a platform administrator, so a
  new install is usable without any out-of-band database mutation. The grant is
  keyed on *no users existing*, not *no admin existing*, so it cannot re-open once
  any user exists; concurrent first registrations produce exactly one admin; the
  auto-grant is audited. A fresh deployment with self-registration closed
  (FR-AUTH-1 open flag off) still permits this single bootstrap registration.

### 6.2 Communities

- FR-COMM-1: A Community is a named container that owns servers and their data.
- FR-COMM-2: Communities are created **only by a platform administrator**, who
  assigns the initial owner. Self-service creation is dropped (see Section 2.3).
- FR-COMM-3: Each Community is isolated: its resources are invisible to
  non-members (Layer-1 visibility; see 6.4).
- FR-COMM-4: On creation, a Community is seeded with preset roles (at minimum an
  **Owner** role granting all permissions).

### 6.3 Membership

- FR-MEM-1: Members are added by a Community owner/admin **manually adding an
  existing user account** to the Community. Invite links and email invitations
  are dropped (see Section 2.3).
- FR-MEM-2: Membership is many-to-many: a user may belong to multiple
  Communities and hold different roles in each.
- FR-MEM-3: A member can be removed; removal revokes that Community's roles and
  resource grants for the user.
- FR-MEM-4: A user's view (lists of servers, data, etc.) is always scoped to the
  Communities they are a member of.

### 6.4 Authorization

Authorization is two-layered:

- **Layer 1 — visibility / isolation (membership).** A user can perceive a
  Community's resources only if they are a member. Non-members get no existence
  signal (404/empty, not 403).
- **Layer 2 — operations (within a Community).** What a member may *do* is
  determined by:
  - **Custom roles**: each Community defines its own roles; a role is a set of
    operation permissions. Preset roles are seeded; owners may define more.
  - **Resource grants**: in addition to roles, a member may receive permissions
    scoped to a specific resource (e.g. start/stop server X only).

Requirements:

- FR-AUTHZ-1: The decision primitive is `can(user, operation, resource)`,
  exposed as a `PermissionChecker` Port. Business logic calls it and is unaware
  of how the answer is computed.
- FR-AUTHZ-2: The effective permission set for a member on a resource is
  `(role permissions held in the resource's Community) ∪ (resource grants to
  that member)`.
- FR-AUTHZ-3: Operations are identified by codes of the form
  `<resource>:<action>` (e.g. `server:start`, `server:delete`, `file:edit`,
  `backup:restore`, `member:add`, `role:manage`). The full catalog is an
  appendix (see Appendix A) and is authoritative.
- FR-AUTHZ-4: Roles are Community-scoped entities; the same role name in two
  Communities denotes two independent roles.
- FR-AUTHZ-5: The platform administrator role is a separate axis, evaluated
  outside any Community context, governing fleet/Community provisioning
  operations.
- FR-AUTHZ-6: Authorization must be enforced server-side on every operation;
  client-side scoping is convenience only.

### 6.5 Minecraft Server Lifecycle

- FR-SRV-1: A member with the right permission can create a server within a
  Community, specifying at least: Minecraft edition/version, server type
  (vanilla/Forge/Paper/etc. as supported), and the desired execution backend.
- FR-SRV-2: Supported lifecycle operations: start, stop, restart, delete, and
  configuration edit. Each maps to an operation code and is permission-gated.
- FR-SRV-3: The API holds the authoritative record of each server (identity,
  Community, config, desired state, last-known runtime state, assigned Worker).
- FR-SRV-4: Runtime state (running/stopped/starting/crashed) is reported by the
  Worker over the control plane; the API's record reflects it. The API's record
  is the source of truth for *desired* state; the Worker reports *observed*
  state.
- FR-SRV-5: Server commands (e.g. RCON) issued via the API are forwarded to the
  owning Worker over the control plane.

### 6.6 Execution Backends & ExecutionDriver

- FR-EXE-1: Execution method is abstracted as an `ExecutionDriver` interface
  inside the Worker. The API sends logical commands ("start this server"); the
  driver realizes them for its backend.
- FR-EXE-2: M1 ships one driver: **container (Docker)**. The `ExecutionDriver`
  abstraction stays pluggable (FR-EXE-1, FR-EXE-4).
- FR-EXE-3: Container is the only backend. The M1 baseline assumption is that
  the backend is fixed for a server's lifetime.
- FR-EXE-4: The interface must remain shaped so a Kubernetes driver could be
  added without interface changes. Building such a driver is not committed (see
  Section 2.3); this is a compatibility constraint on the abstraction, not a
  planned deliverable.
- FR-EXE-5: The Worker selects the correct Java runtime for a server based on its
  Minecraft version (multiple Java versions may be installed; the right one is
  chosen per server). This selection is the driver's/Worker's concern, not the
  API's. (The legacy repository's
  [docs/app/JAVA_COMPATIBILITY.md](https://github.com/mmiura-2351/mc-server-dashboard-api/blob/master/docs/app/JAVA_COMPATIBILITY.md)
  is a working reference for the version-to-Java mapping.)

### 6.7 Worker Management

- FR-WRK-1: A Worker authenticates and registers with the API on startup,
  advertising its capabilities (available drivers, resources).
- FR-WRK-2: The API maintains a registry of connected Workers and their
  liveness (heartbeat over the control plane).
- FR-WRK-3: When a server is started, the API assigns it to an eligible Worker.
  M1 placement is **resource-aware** but advisory (issue #710): filter Workers by
  capability (the required ExecutionDriver is available), then **memory-gate** and
  **CPU-rank** the survivors. Accounting is commit-based — it sums the committed
  `memory_limit_mb` / `cpu_millis` of each Worker's running-assigned servers
  against that Worker's advertised `HostResources`. A Worker whose committed
  memory plus the request cannot fit advertised memory minus a reserve
  (`max(1024 MiB, 10%)`) is **excluded**; if none fit, placement fails with
  `NoEligibleWorker`. CPU is a soft least-loaded tie-break on committed
  `cpu_millis` and never excludes. Memory hard-gates because it is OOM-enforced
  (issue #707); CPU soft-ranks because shares oversubscribe (issue #724). The
  placement function is isolated so a richer scheduler can replace it later
  without changing callers.
- FR-WRK-4: Workers are stateless and replaceable. If a Worker disconnects, its
  servers are marked accordingly; they can be (re)started on another eligible
  Worker after hydrate from authoritative storage.
- FR-WRK-5: A Worker can be drained/decommissioned: its servers are stopped
  (a final snapshot is taken) and may be relocated to another Worker.
- FR-WRK-6: A server is not permanently pinned to a Worker; relocation is
  supported via stop → snapshot → hydrate → start.

### 6.8 Data & Storage

- FR-DATA-1: Authoritative world data, server JARs, and backups are owned by the
  **API side**, stored via a `Storage` Port.
- FR-DATA-2: The Storage backend is pluggable and config-selected: filesystem,
  remote filesystem, or object storage. Switching backends is a configuration
  change, not a code change.
- FR-DATA-3: The Worker is **storage-backend-agnostic**. It never talks to the
  Storage backend directly; the API mediates all transfer (API-mediated data
  plane).
- FR-DATA-4: Runtime data lifecycle (snapshot-based sync):
  - **Start**: on placement onto a Worker that does not already hold a
    sufficiently fresh working set, the API drives hydrate of the server's
    working set from Storage to that Worker's local scratch; then the Worker
    launches MC. A same-worker restart skips hydrate and starts on the Worker's
    existing scratch (the live, newer working set) only when the Worker holds a
    generation at least as fresh as the authoritative store generation —
    otherwise it hydrates first, so a stale or absent scratch never silently
    boots out-of-date world data (issues #696, #763). Each published snapshot
    increments a monotonic generation counter; the Worker records the generation
    locally after each snapshot commit and reports it to the API at
    (re-)registration via `Register.held_servers`; the skip is gated on
    `held_generation >= store_generation`.
  - **Running**: MC writes to the Worker's local scratch; the authoritative copy
    is temporarily stale.
  - **Stop / interval**: the Worker's working set is snapshotted back to Storage.
  - **Relocation**: snapshot on the old Worker, hydrate on the new Worker.
- FR-DATA-5: The RPO (Recovery Point Objective) is bounded by the snapshot
  interval; a crash may lose up to one interval of changes. This is the accepted
  M1 trade-off. (Continuous delta sync is deferred; the sync strategy should be
  encapsulated so it can be upgraded.)
- FR-DATA-6: Snapshot/hydrate must be safe against partial transfer (atomic
  publish of a completed snapshot; never overwrite the authoritative copy with a
  partial one).
- FR-DATA-7: Snapshot cadence is **periodic with per-server override plus
  event-driven** snapshots. A configurable default interval applies to every
  running server; each server may override its interval; and snapshots are also
  taken on events (graceful stop, on-demand backup). This bounds the loss window
  even for servers that run a long time without a save.

### 6.9 File / Backup semantics by server state

Because the live truth of a running server lives on its Worker while the
authoritative copy on Storage is temporarily stale, file and backup operations
branch on server state. This policy is shared by 6.10 (File Management) and
6.11 (Backup Management):

| Operation | Stopped server | Running server |
|---|---|---|
| File read | Authoritative Storage copy | Read-through to the Worker's live working set |
| File edit | Authoritative Storage copy | Applied to the Worker's live working set (effect may require a restart). A file edit **creates** the target if it does not exist yet (create-through), the same as an at-rest edit — a valid relative path to a not-yet-existing file is never rejected as a bad path. |
| Backup | Archive directly from the authoritative Storage copy | On-demand snapshot (worker quiesces via save-off → save-all → settle-wait) → archive (no stop required) |
| Restore | Replace the authoritative working set | **Stop required** (hot replacement of a live working set is unsafe) |

### 6.10 File Management

- FR-FILE-1: Members with permission can browse, read, and edit a server's files
  through the API.
- FR-FILE-2: File operations follow the state-branching policy above: they act on
  the authoritative Storage copy when the server is stopped, and route to the
  Worker's live working set (over the control plane) when it is running.
- FR-FILE-3: Each file edit is versioned: the system retains prior versions and
  can roll a file back to any retained version.
- FR-FILE-4: Path-traversal protection is enforced inside the Storage adapter and
  on any Worker-side file access.

### 6.11 Backup Management

- FR-BAK-1: Members with permission can create, list, restore, and delete
  backups of a server.
- FR-BAK-2: Backups are produced per the 6.9 state policy: from the authoritative
  Storage copy when stopped, or via on-demand snapshot (worker quiesces via
  save-off → save-all → settle-wait) → archive when running. A backup is effectively a retained snapshot and does not depend
  on a specific Worker.
- FR-BAK-3: Scheduled backups are supported as a first-class `backup` schedule on
  the general scheduler (epic #649): a per-server `schedule` row with
  `action = backup` firing on a cron expression or a fixed interval (DATABASE.md
  Section 8), driven by the one scheduler runner. The legacy per-server
  `backup_interval_hours` config-key cadence is **retired** (owner decision D1,
  issue #1840): existing keys are migrated to equivalent enabled `backup`
  schedules and the key is stripped, so a create/update still carrying it is a
  `422` (`retired_config_key`). Execution history is the ordered set of `backup`
  rows with `source = scheduled`. A scheduled backup that fails no longer retries
  on every scheduler tick: it gets the runner's bounded retry (one, ~30 minutes
  later) plus an operator notification, then waits for the next occurrence.
- FR-BAK-4: Restore replaces a server's authoritative working set and **requires
  the server to be stopped** (per the 6.9 policy); hot-restore of a running
  server is not supported.

### 6.12 Version Management

- FR-VER-1: The system lists supported Minecraft versions and server types and
  resolves the appropriate downloadable JAR. The catalogued (resolvable) server
  types are **vanilla** (Mojang version manifest), **paper** (PaperMC API),
  **fabric** (meta.fabricmc.net — the generated server launcher JAR), and
  **forge** (the Forge Maven + promotions feed).

  **forge** resolves to the *installer* JAR
  (`maven.minecraftforge.net/.../forge-<v>-installer.jar`), shipped to the worker
  at the conventional `server.jar` relpath; the worker runs the supervised
  `--installServer` step on first start (the `forge-argsfile` launch mode, FR-EXE),
  then launches via the generated args file. The catalog lists the MC versions
  Forge has published, picking the recommended (fallback latest) Forge build per
  MC version from the promotions feed.
- FR-VER-2: JAR retrieval uses external APIs (official manifests, etc.) behind an
  adapter with retry and cache fallback. Vanilla (SHA-1), Paper (SHA-256), and
  Forge (SHA-1, from the installer's sibling `.sha1`) publish a checksum that is
  verified on download; the Fabric meta API publishes no digest for its generated
  launcher JAR, so that download is stored unverified but still content-addressed
  by its own SHA-256.
- FR-VER-3: Retrieved JARs are persisted through the Storage Port and reused
  across servers.

### 6.13 Real-time Monitoring

- FR-MON-1: Server status changes are pushed from Workers to the API over the
  control plane and made available to clients in real time.
- FR-MON-2: Server log output is streamed from the Worker to the API and relayed
  to subscribed clients.
- FR-MON-3: Basic runtime metrics (e.g. up/down, resource usage if available)
  are reported by Workers.
- FR-MON-4: Real-time delivery degrades gracefully: if the real-time transport
  is down, REST endpoints still function and clients simply miss live updates.

### 6.14 Audit Logging

- FR-AUD-1: Security- and state-relevant operations are recorded to an audit
  trail (actor, Community, operation, target, outcome, timestamp).
- FR-AUD-2: Audit writes must never block or fail the business operation: an
  event is recorded only after the business transaction commits, and a failure
  to write audit data must not raise into or roll back the operation
  (fire-after-commit, must-not-raise).
- FR-AUD-3: Audit logs are queryable by platform administrators and, scoped to
  their Community, by authorized members.

---

## 7. Non-Functional Requirements

- NFR-SCALE-1: Target scale is small — up to a few dozen Communities and on the
  order of tens of concurrent servers. A single API instance is acceptable.
- NFR-AVAIL-1: A Worker outage must not lose authoritative data beyond the
  snapshot RPO, and affected servers must be recoverable on another Worker.
- NFR-AVAIL-2: API-tier outage stops control operations but must not corrupt
  authoritative Storage.
- NFR-SEC-1: Workers authenticate to the API; the control channel is
  authenticated and encrypted (mTLS/TLS).
- NFR-SEC-2: All authorization decisions are enforced server-side via the
  `PermissionChecker` Port.
- NFR-SEC-3: Secrets are read from configuration/environment, never hard-coded.
- NFR-OBS-1: Logs are structured (machine-parseable), carry a correlation ID per
  request/operation so a flow can be traced end to end, and mask sensitive fields
  (credentials, tokens, secrets) before output.
- NFR-PORT-1: Every external technology (Storage, execution, transport,
  persistence, auth, permission checks) sits behind a Port so it can be swapped
  without touching business logic.
- NFR-TEST-1: Business logic (domain/application) is unit-testable with all Ports
  faked; adapters and the API boundary are integration-tested.

---

## 8. Architecture Principles

- Hexagonal (Ports & Adapters): a pure domain core, use cases depending only on
  domain Ports, adapters implementing Ports, wiring at the edge. (The legacy
  repository's
  [docs/app/ARCHITECTURE.md](https://github.com/mmiura-2351/mc-server-dashboard-api/blob/master/docs/app/ARCHITECTURE.md)
  is a proven reference for the layering rules.)
- Two planes (control / data) between API and Worker, both API-terminated.
- Stateless Workers; authoritative state on the API side.
- Config-driven adapter selection (Storage backend, execution driver).
- Naming conventions reused from the legacy system where sensible
  (`<resource>:<action>` permission codes, `<Tech><Port>` adapters,
  present-tense use-case names).

---

## 9. Resolved Design Decisions

The open questions from the first draft are resolved as follows:

| # | Question | Decision | Refs |
|---|---|---|---|
| 1 | Community provisioning flow | **Admin-only creation.** Only a platform administrator creates a Community and assigns its owner. Self-service dropped (Section 2.3). | FR-COMM-2 |
| 2 | Invitation mechanism | **Manual add.** A Community owner/admin adds an existing user account directly. Invite links / email dropped (Section 2.3). | FR-MEM-1 |
| 3 | File/backup semantics for running servers | **State-branching policy** (read/edit read-through to the Worker when running; backup via worker-quiesced snapshot→archive; restore requires stop). | 6.9, FR-FILE-2, FR-BAK-4 |
| 4 | Snapshot interval policy | **Periodic default + per-server override + event-driven** (graceful stop, on-demand backup). | FR-DATA-7 |
| 5 | Transport | **gRPC bidirectional stream** for the control plane; **API-terminated HTTP** for the bulk data plane. | 5.1, 5.2 |
| 6 | Worker placement (M1) | **Resource-aware, advisory**: capability filter, then commit-based memory gate (hard, OOM-enforced) and CPU least-loaded rank (soft, oversubscribed) against advertised `HostResources`; `NoEligibleWorker` if memory cannot fit. Placement isolated for a future scheduler. | FR-WRK-3, #710, #707, #724 |
| 7 | Repository topology | **Monorepo** (`api/`, `worker/`, shared `proto/`). | 1.2 |
| 8 | Platform-admin authentication | **Same identity system + administrator role/flag.** | FR-AUTH-6 |
| 9 | Quotas | **Deferred**; the Community data model leaves room for optional limit fields (unused in M1). | Appendix B |

### 9.1 Remaining for the design phase

These need design-level decisions but do not change the requirements:

- Whether and how a server's execution backend may change after creation
  (FR-EXE-3); the M1 baseline treats it as fixed for the server's lifetime.
- Concrete protobuf service/message definitions for the control plane.
- Worker-side file-access protocol details for read-through edits (6.9).
- Storage adapter contracts (fs / remote-fs / object) and the atomic snapshot
  publish mechanism (FR-DATA-6).
- Version/JAR source adapters and Java runtime selection per server type.
- The web UI (`webui/` in this monorepo) adaptation to the v2 API
  (docs/ui/WEBUI_SPEC.md).

---

## Appendix A — Operation permission catalog (draft)

Authoritative codes are `<resource>:<action>`. Initial catalog to refine:

| Domain | Codes |
|---|---|
| Server | `server:create`, `server:read`, `server:update`, `server:delete`, `server:start`, `server:stop`, `server:restart`, `server:command` |
| File | `file:read`, `file:edit`, `file:history`, `file:rollback` |
| Backup | `backup:create`, `backup:read`, `backup:restore`, `backup:delete`, `backup:schedule` |
| Member | `member:read`, `member:add`, `member:remove` |
| Role | `role:read`, `role:manage` |
| Grant | `grant:read`, `grant:manage` |
| Group | `group:read`, `group:manage` |
| Community | `community:read`, `community:update`, `community:delete` |
| Audit | `audit:read` (community-scoped; the audit trail query for authorized members, FR-AUD-3) |
| Session | `session:read` (community-scoped; the relay game-session moderation surface — player IPs are PII, RELAY.md Section 8) |
| Plugin | `plugin:read`, `plugin:manage` (community-scoped; plugin/mod content management, issue #1150) |
| Schedule | `schedule:read`, `schedule:manage` (per-server scheduler CRUD, epic #649; writes additionally require the scheduled action's own permission — `server:command` / `server:start` / `server:stop` / `server:restart` / `backup:schedule` — so `schedule:manage` alone cannot escalate) |
| Platform (admin axis) | `worker:manage`, `community:provision`, `platform:monitor` |

## Appendix B — Core entities (sketch)

- **User** — global identity (auth, credentials).
- **Community** — isolation/ownership unit. Leaves room for optional limit/quota
  fields (unused in M1).
- **Membership** — (user, community) with role assignments.
- **Role** — community-scoped named permission set.
- **ResourceGrant** — (user, resource, permissions) override.
- **Server** — community-scoped MC server (config, desired state, observed
  state, execution backend, assigned worker).
- **Worker** — registered execution host (capabilities, liveness).
- **Backup** — retained snapshot metadata for a server.
- **FileEditHistory** — versioned file changes for rollback.
- **AuditLog** — activity trail.
- **RefreshToken** — persisted session token.
