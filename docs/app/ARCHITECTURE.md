# Architecture

> Status: **Design** · Audience: contributors to `api/`, `worker/`, `proto/`
>
> This document defines the v2 architecture: the Hexagonal (Ports & Adapters)
> layering, the `api/` / `worker/` / `proto/` module boundaries, the catalog of
> domain Ports, and the design decisions for the architecture-level items left
> open in [`REQUIREMENTS.md`](../REQUIREMENTS.md) Section 9.1. It refines, but
> does not contradict, the requirements; where the two disagree, the
> requirements win and this document is wrong.

## Table of Contents

1. [Goals & posture](#1-goals--posture)
2. [Hexagonal layering](#2-hexagonal-layering)
3. [Monorepo module boundaries](#3-monorepo-module-boundaries)
4. [Two planes between API and Worker](#4-two-planes-between-api-and-worker)
5. [Domain Ports catalog](#5-domain-ports-catalog)
6. [Naming conventions](#6-naming-conventions)
7. [Architecture decisions (Section 9.1)](#7-architecture-decisions-section-91)
8. [Related documents](#8-related-documents)

---

## 1. Goals & posture

The system is two cooperating services in one repository:

- **`api/`** (Python) — the authoritative service. Owns identity, Communities,
  authorization, the server lifecycle records, the pluggable `Storage`, the
  Worker registry, and both ends of the API↔Worker channel.
- **`worker/`** (Go) — a stateless, replaceable agent that actually runs
  Minecraft via a pluggable `ExecutionDriver`. Holds no authoritative data.
- **`proto/`** (buf) — the shared protobuf/gRPC contract for the control plane,
  consumed by both sides.

Target scale is small (REQUIREMENTS.md NFR-SCALE-1): a few dozen Communities,
tens of concurrent servers, a single API instance. The architecture is biased
toward **simplicity first**, while keeping each external technology behind a
**Port** so the M1 implementation can be replaced without touching business
logic (NFR-PORT-1). Small scale is allowed to show up in the *implementation* of
a Port, never in the *shape* of the Port (REQUIREMENTS.md Section 1.1).

---

## 2. Hexagonal layering

Both services follow Hexagonal (Ports & Adapters): a pure domain core, use cases
that depend only on domain Ports, adapters that implement those Ports, and
wiring assembled at the edge. The proven layering rules below are adapted from
the legacy
[`docs/app/ARCHITECTURE.md`](https://github.com/mmiura-2351/mc-server-dashboard-api/blob/master/docs/app/ARCHITECTURE.md);
the legacy system was a single host, so its single-process specifics are dropped
and the rules are applied independently inside each of `api/` and `worker/`.

### 2.1 Layers

```
  ┌──────────────────────────────────────────────────────────────┐
  │  edge / wiring                                                 │
  │  HTTP routers (api) · gRPC handlers · process main · DI setup  │
  │  the ONLY place adapters are bound to Ports                    │
  └───────────────┬──────────────────────────────┬────────────────┘
                  │ depends on                    │ binds
                  ▼                               ▼
  ┌───────────────────────────────┐   ┌────────────────────────────┐
  │  application (use cases)       │   │  adapters                  │
  │  one verb per use case;        │   │  concrete tech behind a    │
  │  receives Ports as args        │   │  Port (DB, gRPC, FS, …)    │
  └───────────────┬───────────────┘   └──────────────┬─────────────┘
                  │ depends on                        │ implements
                  ▼                                   ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  domain (pure core)                                            │
  │  entities · value objects · domain errors · Port interfaces    │
  │  no framework, no I/O, no external library                     │
  └──────────────────────────────────────────────────────────────┘
```

| Layer | May contain / depend on | Must not |
|---|---|---|
| `domain` | entities, value objects, domain errors, Port interfaces, pure functions; standard library only | import any framework, driver, HTTP/gRPC client, or other layer |
| `application` | use cases that depend on `domain` (Ports + types) only | import `adapters`, the edge, or any framework |
| `adapters` | concrete implementations of `domain` Ports; any framework/library | be imported by `domain` or `application` |
| edge (`api` routers, gRPC handlers, `main`) | `application`, and `adapters` **only in the wiring file** | put business logic in routers/handlers |

### 2.2 Dependency direction

```
   edge  →  application  →  domain  ←  adapters
```

The arrow always points inward to `domain`. `domain` depends on nothing else in
the project; `application` depends only on `domain`; `adapters` depend on
`domain` (for the Port interfaces they implement) and on external libraries;
the edge depends on `application` and binds concrete adapters to Ports in one
place. Dependency inversion is the mechanism: a use case receives the Ports it
needs as constructor/struct arguments and never instantiates a concrete adapter
itself.

These rules are mechanically checkable and should be enforced by import-direction
contracts in CI (e.g. import-linter on the Python side, an equivalent guard on
the Go side). The exact tooling and how to run it locally are in
[`../dev/DEVELOPMENT.md`](../dev/DEVELOPMENT.md) Section 5.

### 2.3 Why this applies to both services

`api/` is the obvious Hexagonal target (persistence, auth, transport are all
swappable technologies). `worker/` benefits equally: its domain core is "the
desired/observed lifecycle of a local server instance", its key Port is
`ExecutionDriver`, and its adapters are the host-process and container drivers.
Keeping the Worker Hexagonal is what lets a future Kubernetes driver
(REQUIREMENTS.md FR-EXE-4) drop in without touching Worker business logic, and
lets the Worker's lifecycle logic be unit-tested with a fake driver
(NFR-TEST-1).

---

## 3. Monorepo module boundaries

```
repo/
├── proto/        # buf: protobuf + gRPC control-plane contract (shared)
├── api/          # Python: authoritative service (Hexagonal, per-domain)
└── worker/       # Go: stateless execution agent (Hexagonal)
```

### 3.1 What lives where

| Module | Language / tool | Owns |
|---|---|---|
| `proto/` | buf (protobuf) | the typed control-plane contract: the bidi-stream service, command and event messages, capability advertisement. No logic. |
| `api/` | Python | identity & auth, Communities/membership, authorization, server lifecycle records, the `Storage` Port + adapters, Worker registry & placement, both planes' API-side ends, audit, version/JAR resolution. The authoritative state. |
| `worker/` | Go | the gRPC stream client to the API, the `ExecutionDriver` Port + host-process/container adapters, local scratch working-dir management, Java-runtime selection, hydrate/snapshot transfer client, RCON, log/metric/heartbeat emission. No authoritative state. |

### 3.2 Dependency direction between modules

```
   api/ ──generates client from──▶ proto/ ◀──generates server-side stubs── worker/
                       (shared contract; no api/ ↔ worker/ direct dependency)
```

- `api/` and `worker/` **both depend on `proto/`** for generated types and
  service stubs. `proto/` depends on neither.
- `api/` and `worker/` have **no direct code dependency on each other**. Every
  interaction crosses the wire and is described entirely by `proto/` (control
  plane) or the API-terminated HTTP data-plane endpoint (Section 4).
- Because the contract is shared, a control-plane change is a single change set
  that updates `proto/`, `api/`, and `worker/` together; never merge a contract
  change that leaves one side uncompiled or unimplemented
  ([`../dev/CONTRIBUTING.md`](../dev/CONTRIBUTING.md) Section 5).

### 3.3 Authority direction at runtime

The API is authoritative; the Worker is subordinate. The API holds **desired
state** and issues commands; the Worker reports **observed state** and never
makes authoritative decisions (REQUIREMENTS.md FR-SRV-3, FR-SRV-4). A Worker can
be replaced at any time with no data loss beyond the snapshot RPO
(REQUIREMENTS.md FR-WRK-4, NFR-AVAIL-1).

---

## 4. Two planes between API and Worker

Both planes are **API-terminated** (REQUIREMENTS.md Section 5.2).

- **Control plane** — a Worker-initiated, persistent **gRPC bidirectional
  stream** (REQUIREMENTS.md Section 5.1). One stream multiplexes API→Worker
  commands (start/stop/restart, RCON, hydrate/snapshot triggers, file-access
  requests — see Section 7.2) and Worker→API events (status, log lines, metrics,
  heartbeat). The typed contract lives in `proto/`; concrete messages are
  defined in issue #2, not here.
- **Data plane** — bulk hydrate/snapshot transfer over an **API-terminated HTTP
  endpoint**, kept off the control stream so large transfers never block command
  traffic. The Worker is storage-backend-agnostic and talks only to the API; the
  API is the sole component that touches the pluggable `Storage`
  (REQUIREMENTS.md FR-DATA-3). Snapshot atomicity (FR-DATA-6) is specified in
  STORAGE.md (issue #17), not here.

The control plane is a Port on the API side (`ControlPlane`, Section 5) and a
client on the Worker side; this keeps the API's business logic independent of
gRPC.

---

## 5. Domain Ports catalog

Every external technology sits behind a Port (NFR-PORT-1). The table lists the
Ports implied by REQUIREMENTS.md, the side that **defines and depends on** each
Port, and the M1 adapter(s). "Side" is where the Port's *interface* lives; an
adapter on the other side of the wire may fulfil it (e.g. the API's
`ControlPlane` reaches Worker behaviour by sending commands over the stream).

### 5.1 API-side Ports

| Port | Purpose (req. ref) | M1 adapter(s) |
|---|---|---|
| `Storage` | Authoritative world/JAR/backup store; hydrate/snapshot source of truth (FR-DATA-1, FR-DATA-2) | fs / remote-fs / object, config-selected. Contract in STORAGE.md (#17) |
| `PermissionChecker` | `can(user, operation, resource)` decision (FR-AUTHZ-1, NFR-SEC-2) | role + resource-grant evaluator |
| `TokenService` | Issue/verify short-lived access & long-lived refresh tokens (FR-AUTH-2) | JWT-or-equivalent adapter |
| `PasswordHasher` | Hash/verify passwords with per-user salt (FR-AUTH-3) | bcrypt/argon2 adapter |
| `LoginAttemptStore` | Brute-force/lockout runtime state: record attempts, count per-username/per-IP failures over sliding windows, hold the per-account lockout + back-off (FR-AUTH-4). Decision in SECURITY.md Section 3 | DB-backed adapter (`login_attempt` + `account_lockout` tables) |
| `ControlPlane` | Send commands to a Worker, receive its events; track liveness (FR-SRV-5, FR-WRK-2, Section 6.13) | gRPC bidi-stream server (`proto/`) |
| `WorkerRegistry` | Connected Workers, capabilities, liveness, placement input (FR-WRK-1, FR-WRK-2, FR-WRK-3) | in-memory registry fed by the stream |
| `VersionCatalog` / JAR source | List MC versions/types, resolve & fetch the JAR (FR-VER-1, FR-VER-2) | external-manifest client with retry + cache fallback |
| `RealTimeEvents` | Relay status/log/metric events to subscribed clients (FR-MON-1, FR-MON-2, FR-MON-4) | WebSocket/SSE publisher |
| `AuditWriter` | Fire-after-commit, must-not-raise audit recording (FR-AUD-1, FR-AUD-2) | DB-backed writer |
| repositories (`<Entity>Repository`) + `UnitOfWork` | Persist core entities (Appendix B) transactionally | DB adapter. Model in DATABASE.md (#15) |
| `Clock` | Time, for tokens/schedules/snapshots | system-clock adapter |

`Storage`, `RealTimeEvents`, the JAR source, persistence, and `ControlPlane`
have their own design docs (Section 8); only their *interface placement* is fixed
here.

### 5.2 Worker-side Ports

| Port | Purpose (req. ref) | M1 adapter(s) |
|---|---|---|
| `ExecutionDriver` | Realize logical start/stop/restart for a backend (FR-EXE-1, FR-EXE-2, FR-EXE-4) | host-process driver, container (Docker) driver |
| `JavaRuntimeSelector` | Pick the Java runtime for a server's MC version (FR-EXE-5) | local-installs selector (legacy JAVA_COMPATIBILITY mapping) |
| `WorkingDir` | Manage the local scratch working set per server; path-traversal-safe file access (FR-DATA-4, FR-FILE-4) | local-filesystem adapter |
| `DataTransfer` | Pull (hydrate) / push (snapshot) the working set via the API HTTP data-plane (FR-DATA-3, FR-DATA-4) | HTTP client to the API |
| `ServerControl` (RCON) | `save-all`, commands, graceful stop on the running process (FR-SRV-5, Section 6.9) | RCON client |
| `APIClient` (control-plane) | Maintain the bidi stream; emit events; accept commands | gRPC stream client (`proto/`) |

The Worker side deliberately has **no `Storage` Port**: it never sees the
authoritative backend (FR-DATA-3). It reaches authoritative data only through
`DataTransfer` to the API.

---

## 6. Naming conventions

Reused from the legacy system where sensible (REQUIREMENTS.md Section 8). On the
Python (`api/`) side these are binding; the Go (`worker/`) side adopts the
spirit (idiomatic Go names) but keeps the same Port/adapter/permission concepts.

| Item | Convention | Example |
|---|---|---|
| Port (interface) | `<Noun>`, no `I`/`Abstract` prefix | `Storage`, `PermissionChecker`, `Clock` |
| Adapter | `<Tech><Port>` | `ObjectStorage`, `JwtTokenService`, `DockerExecutionDriver` |
| Use case | present-tense verb, no suffix | `CreateServer`, `RestoreBackup` |
| Permission code | `<resource>:<action>` | `server:start`, `backup:restore` |
| Request schema (api) | `<UseCase>Request` | `CreateServerRequest` |
| Response schema (api) | `<Entity>Response` | `ServerResponse` |

The per-domain directory layout inside `api/` (a `domain/ application/ adapters/
api/` quadrant per bounded context, with wiring isolated to a single
dependencies file) follows the legacy layout and is detailed in
[`../dev/DEVELOPMENT.md`](../dev/DEVELOPMENT.md) Section 4 as the Python domain
code lands.

---

## 7. Architecture decisions (Section 9.1)

These resolve the architecture-level items in REQUIREMENTS.md Section 9.1. Each
records the decision, the alternatives considered, and the rationale. They are
design decisions and do not change the requirements.

### 7.1 Execution backend is fixed for a server's lifetime (FR-EXE-3)

**Decision.** The execution backend (host-process vs container) is chosen at
server creation and is **immutable for the server's lifetime** in M1. Changing
backend means deleting and recreating the server (its world data can be carried
over via backup/restore through `Storage`).

**Alternatives considered.**
1. *Mutable backend via a config edit* — allow switching on a stopped server.
2. *Mutable via internal relocation* — snapshot under the old backend, hydrate
   under the new one, reusing the FR-WRK-6 relocation machinery.

**Rationale.** The backend is recorded on the `Server` entity (Appendix B) and
feeds placement (the required `ExecutionDriver` is a Worker-capability filter,
FR-WRK-3). Keeping it immutable removes a state-transition class and the edge
cases of a half-migrated server, for a use case (alternative 1/2) that is rare at
this scale and already achievable via recreate. The data model still *stores*
the backend as a field, so a future milestone can lift this to a supported
operation (built on the existing relocation path) without a schema change — the
constraint is a policy, not a structural limit.

### 7.2 Worker-side file access rides the control plane (Section 6.9)

**Decision.** Read-through reads and live edits of a **running** server's files
(REQUIREMENTS.md Section 6.9) are performed by the API issuing **file-access
commands over the existing gRPC control plane** to the owning Worker, which acts
on its live local working set and returns the result. There is **no separate
inbound file service on the Worker**. The concrete request/response message
shapes are defined in issue #2 (`proto/`); only the architectural shape is fixed
here.

**Alternatives considered.**
1. *Direct API→Worker file HTTP/RPC endpoint* — a second inbound surface on the
   Worker.
2. *Route file reads through the data plane* (treat each read as a mini-hydrate).

**Rationale.** Workers are Worker-initiated with **no inbound exposure**
(REQUIREMENTS.md Section 5.1); alternative 1 would break that and re-introduce
the inbound-port problem the connection model exists to avoid. The control plane
already multiplexes per-server commands to the owning Worker and carries small,
latency-sensitive messages — exactly the profile of a file read/edit. The data
plane (alternative 2) is for bulk working-set transfer and is the wrong
granularity for editing a single `server.properties`. Bounds: file access is
interactive and small; bulk movement stays on the data plane. Path-traversal
protection is enforced on the Worker side as well as in the `Storage` adapter
(FR-FILE-4), realized by the Worker's `WorkingDir` Port (Section 5.2).

### 7.3 JAR source on the API, Java runtime on the Worker (Section 6.12, FR-EXE-5)

**Decision.** Ownership splits along the API/Worker authority line:

- **Version listing and JAR retrieval are API-side.** The API owns the
  `VersionCatalog`/JAR-source Port: it lists supported versions/types, resolves
  the downloadable JAR via external manifests (with retry + cache fallback), and
  persists the JAR through `Storage` for reuse across servers (FR-VER-1,
  FR-VER-2, FR-VER-3). The JAR reaches a Worker as part of the normal hydrate
  (data plane) — the Worker never fetches JARs from the internet.
- **Java runtime selection is Worker-side.** The Worker (its `ExecutionDriver` /
  `JavaRuntimeSelector` Port) chooses the correct locally-installed Java runtime
  for the server's MC version at launch (FR-EXE-5). The API does not know or
  care which `java` binary runs.

**Alternatives considered.**
1. *Worker fetches JARs directly* — Worker talks to external sources.
2. *API dictates the Java runtime* — the API computes and sends a Java version.

**Rationale.** Alternative 1 contradicts the storage-agnostic, no-inbound,
replaceable Worker (FR-DATA-3) and would duplicate retry/cache logic per Worker;
centralizing JAR retrieval on the API gives one cache and one egress point.
Alternative 2 leaks a host detail (which Java versions are installed *on a
specific Worker*) into the authoritative service; the installed runtimes are a
property of the Worker host, so the choice belongs where that knowledge lives —
the Worker — consistent with REQUIREMENTS.md FR-EXE-5 ("this selection is the
driver's/Worker's concern, not the API's"). The legacy
[JAVA_COMPATIBILITY.md](https://github.com/mmiura-2351/mc-server-dashboard-api/blob/master/docs/app/JAVA_COMPATIBILITY.md)
version-to-Java mapping is a working reference for the selector.

### 7.4 API framework stack: FastAPI + async SQLAlchemy + Alembic

**Decision.** The `api/` service is built on **FastAPI** (ASGI, served by
Uvicorn) with **async SQLAlchemy 2.x** over **asyncpg** as the persistence
adapter behind the `<Entity>Repository` / `UnitOfWork` Ports (Section 5.1), and
**Alembic** for schema migrations. Configuration is loaded with
**pydantic-settings** at the edge (CONFIGURATION.md Section 1).

**Alternatives considered.**
1. *Litestar / Starlette-only / Flask* instead of FastAPI.
2. *A non-SQLAlchemy data layer* (raw asyncpg, an async ORM such as Tortoise, or
   SQLModel) instead of async SQLAlchemy + Alembic.

**Rationale.** FastAPI is the proven default: the legacy system shipped on it,
it gives request/response validation and OpenAPI for free, and its dependency
injection is a natural fit for binding Ports at the edge (Section 2.1) — keeping
routers thin. The alternatives in (1) are viable but offer no advantage that
justifies diverging from a stack the team already operates; simplicity-first
favors the known quantity. For (2), async SQLAlchemy is the mature async ORM
with a first-class migration tool (Alembic) and the session/transaction
primitives the `UnitOfWork` Port needs; raw asyncpg would re-implement that
machinery, and the smaller async ORMs trade ecosystem maturity for little gain.
asyncpg is the high-performance PostgreSQL driver SQLAlchemy's async engine
targets (DATABASE.md Section 1). All four sit *behind* Ports or at the edge, so
this is an adapter/edge choice that does not reach `domain` or `application`
(Section 2.2) and remains replaceable (NFR-PORT-1).

---

## 8. Related documents

This document links these and does not duplicate their content.

| Doc | Covers |
|---|---|
| [`../REQUIREMENTS.md`](../REQUIREMENTS.md) | What v2 must do; the source of truth for scope |
| [`DATABASE.md`](DATABASE.md) | Persistence model for the core entities |
| [`CONFIGURATION.md`](CONFIGURATION.md) | Runtime configuration & adapter selection |
| [`STORAGE.md`](STORAGE.md) | `Storage` adapter contracts & atomic snapshot publish |
| [`CONTROL_PLANE.md`](CONTROL_PLANE.md) | Concrete control-plane messages |
