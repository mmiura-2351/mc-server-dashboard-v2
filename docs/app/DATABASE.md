# Database

> Status: **Implemented** · Audience: contributors to `api/`
>
> This document defines the persistence model for the core entities sketched in
> [`REQUIREMENTS.md`](../REQUIREMENTS.md) Appendix B: the tables, their keys and
> relationships, the desired-state / observed-state split on `Server`, and the
> persistence-technology decision for M1. It refines, but does not contradict,
> the requirements and [`ARCHITECTURE.md`](ARCHITECTURE.md); where they disagree,
> the requirements win and this document is wrong.
>
> **Scope.** This document covers **metadata only** — the relational records the
> API owns. Bulk artifacts (world data, server JARs, backup archives) are **not**
> stored here; they live behind the `Storage` Port and are specified in
> [`STORAGE.md`](STORAGE.md) (issue #17). A `Backup` row here is the *metadata* of
> a retained snapshot; the archive bytes are in `Storage`. The schema is versioned
> and applied by **Alembic** (`api/migrations/`); this document specifies the
> logical model, not the per-migration DDL. Runtime configuration is in
> `CONFIGURATION.md` (issue #16).

## Table of Contents

1. [Persistence technology](#1-persistence-technology)
2. [Conventions](#2-conventions)
3. [Entity relationship overview](#3-entity-relationship-overview)
4. [Authentication & identity](#4-authentication--identity)
5. [Communities, membership & roles](#5-communities-membership--roles)
6. [Authorization grants](#6-authorization-grants)
7. [Servers & workers](#7-servers--workers)
8. [Backups, file history, sessions, resource packs & schedules](#8-backups-file-history-sessions-resource-packs--schedules)
9. [Audit log](#9-audit-log)
10. [Cascade behavior on member removal](#10-cascade-behavior-on-member-removal)
11. [Related documents](#11-related-documents)

---

## 1. Persistence technology

The core entities are persisted behind a **persistence Port** (NFR-PORT-1): the
`<Entity>Repository` interfaces plus a `UnitOfWork` for transactional grouping,
as cataloged in [`ARCHITECTURE.md`](ARCHITECTURE.md) Section 5.1. Business logic
depends only on those Port interfaces; the concrete database is a single adapter
bound at the edge. The choice below is therefore an **adapter** choice — it can
be replaced without touching `domain` or `application` code.

**Decision.** M1 uses **PostgreSQL** as the relational store behind the
persistence Port.

**Alternatives considered.**

1. *SQLite* — a single embedded file, zero operational footprint. The natural
   minimum for a single-instance service (NFR-SCALE-1).
2. *A document/NoSQL store* — schemaless flexibility for the evolving model.

**Rationale.** The data is strongly relational: many-to-many membership,
Community-scoped roles, per-membership role assignments, and resource grants are
all join-shaped, and the model leans on foreign keys, uniqueness constraints, and
cascade rules (Section 10) to keep itself consistent. A relational engine
expresses these directly, which rules out alternative 2 — the "flexibility" of a
document store would be re-implemented as application-side referential integrity,
the opposite of simplicity-first.

Between PostgreSQL and SQLite (alternative 1) the deciding factors are
concurrency and feature fit, not scale. The API is a single instance
(NFR-SCALE-1), but it serves concurrent requests and a long-lived Worker control
stream that writes observed state (FR-SRV-4) while user requests mutate desired
state; SQLite's single-writer lock makes that contention awkward. PostgreSQL also
gives first-class `timestamptz`, native `uuid`, partial/expression indexes (used
for the refresh-token and audit-query paths), and `ON DELETE` cascade semantics
the model relies on. The operational cost is one managed process, acceptable at
this scale and already implied by a server-side deployment. Because everything is
behind the persistence Port, a deployment that genuinely wants a single file can
provide a SQLite adapter later without a domain change — the Port shape does not
assume small scale (REQUIREMENTS.md Section 1.1).

The concrete migration toolchain is **Alembic**, with the migrations under
`api/migrations/`.

---

## 2. Conventions

- **Primary keys** are `uuid` (application-generated), not auto-increment
  integers. UUIDs are stable across environments, do not leak counts, and let an
  entity be referenced (e.g. in an `AuditLog` target) without a round-trip.
- **Timestamps** are `timestamptz` (UTC). Every entity carries `created_at`;
  mutable entities also carry `updated_at`.
- **Soft vs hard delete.** M1 hard-deletes. The audit trail (Section 9) is the
  record of what happened; entities themselves are removed on delete, with
  cascades (Section 10) keeping the graph consistent. `AuditLog` rows are
  retained even when their referenced actors/targets are gone (Section 9).
- **Naming.** Tables are singular (`user`, `server`), snake_case columns. Foreign
  keys are `<entity>_id`. This is a logical model; exact DDL is owned by the
  Alembic migrations (`api/migrations/`).
- **Enums** are stored as short text with a `CHECK` constraint (or a native enum
  type) rather than opaque integers, so rows are readable and new values are a
  migration, not a code-coupled magic number.

---

## 3. Entity relationship overview

```
                    ┌───────────┐
                    │   user    │ (global identity)
                    └─────┬─────┘
                          │ 1
            ┌─────────────┼───────────────┬──────────────────┐
            │ N           │ N             │ N                │ N
      ┌─────┴──────┐  ┌───┴──────────┐ ┌─┴─────────────┐ ┌──┴──────────┐
      │ membership │  │ resource_    │ │ refresh_token │ │ audit_log   │
      │            │  │ grant        │ │               │ │ (actor_id)  │
      └──┬──────┬──┘  └──────┬───────┘ └───────────────┘ └─────────────┘
         │ N    │ 1          │ N (target = a resource, e.g. server)
         │      │            │
    ┌────┴───┐  │       ┌────┴──────┐
    │community│◀─┘       (scoped to a server / community resource)
    └──┬─────┘  ▲ N
       │ 1      │
       │ N      │ 1
   ┌───┴────┐   │            ┌────────────────────────┐
   │  role  │   │            │ membership_role         │  (M:N join:
   └───┬────┘   │            │ (membership_id, role_id)│   roles per membership)
       │ N ─────┴────────────┴────────────────────────┘
       │
   ┌───┴────┐ 1      N ┌──────────┐    server.assigned_worker_id: nullable uuid,
   │community│────────▶│  server  │    a soft reference to the in-memory fleet
   └────────┘          └────┬─────┘    registry (no `worker` table at M1, Section 7)
                            │ 1
                ┌───────────┼────────────────┬────────────────────────┐
                │ N         │ N              │ 0..1                   │ N
          ┌─────┴─────┐ ┌───┴────────────┐ ┌┴──────────────────────┐ ┌┴──────────┐
          │  backup   │ │ game_session   │ │ server_resource_pack_ │ │ (versions │
          └───────────┘ └────────────────┘ │ assignments           │ │  in       │
                                           └───────────┬───────────┘ │  Storage) │
                                                       │ FK          └───────────┘
                                              ┌────────┴────────┐
                                              │ resource_packs  │ (global, not community-scoped)
                                              └─────────────────┘
```

Reading aids:

- **Membership is many-to-many** (FR-MEM-2): `membership` is the join between
  `user` and `community`. Roles held within a Community attach to the
  *membership*, not the user, via the `membership_role` join — so the same user
  holds different roles in different Communities (FR-AUTHZ-4).
- **Roles are Community-scoped** (FR-AUTHZ-4): a `role` belongs to exactly one
  `community`; the same name in two Communities is two independent rows.
- **A `server` belongs to one `community`** (FR-SRV-3) and is optionally assigned
  to one Worker (FR-WRK-3, nullable — unassigned when stopped/unplaced). At M1 the
  assignment is a plain `assigned_worker_id` uuid with no FK; there is no durable
  `worker` table (Section 7).
- **`resource_grant`** ties a `user` to a specific resource with an extra
  permission set (FR-AUTHZ-2); in M1 the resource is a server (or a
  community-level resource).

---

## 4. Authentication & identity

### `user`

A global identity (FR-AUTH-5): not owned by any Community, so one account can join
many. The platform-administrator capability is a flag on this same record
(FR-AUTH-6, decision #8) — there is no separate admin table.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `username` | text | unique; case-insensitive uniqueness recommended |
| `email` | text | unique |
| `password_hash` | text | bcrypt/argon2 output incl. per-user salt (FR-AUTH-3) |
| `is_platform_admin` | bool | the admin axis (FR-AUTH-6, FR-AUTHZ-5); default false, **except** the first user created on an empty table, which is auto-granted true at registration to bootstrap the platform admin (issue #909) — no manual DB step |
| `active` | bool | account lifecycle flag (issue #278); default true. A deactivated account keeps its row but cannot authenticate: login refuses it with the uniform 401, and an outstanding access token is rejected on its next request |
| `created_at` / `updated_at` | timestamptz | |

Constraints: `UNIQUE(username)`, `UNIQUE(email)`.

> The brute-force / lockout state from FR-AUTH-4 (per-username and per-IP failure
> counters over sliding windows, lockout with back-off) is **auth-hardening
> runtime state**, not a core entity, and is intentionally omitted from this core
> model. Its home is decided in [`SECURITY.md`](SECURITY.md) Section 3: dedicated
> DB-backed tables behind a Port, kept separate from this graph.

### `refresh_token`

Persisted long-lived session tokens (FR-AUTH-2); invalidated on logout
(FR-AUTH-3). Access tokens are short-lived and **not** persisted. Issued and
verified through the `TokenService` Port; this table is only the server-side
revocation/expiry record.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `user_id` | uuid FK → `user.id` | `ON DELETE CASCADE` |
| `token_hash` | text | the token is stored **hashed**, never in plaintext |
| `issued_at` | timestamptz | |
| `expires_at` | timestamptz | |
| `revoked_at` | timestamptz nullable | set on logout; non-null ⇒ invalid |
| `revoked_reason` | text nullable | why revoked (`rotated` / `family` / `logout` / `user_revoked` / `superseded`); null exactly when `revoked_at` is null. **No CHECK constraint** — unlike other enum-like columns (Section 2), this column is a plain `text` validated in application code; the migration (0014) added it as a bare `String` column. The null-exactly-when pairing is an application invariant, not a DB constraint |

Constraints: `UNIQUE(token_hash)`. Index on `(user_id)` for "revoke all sessions"
and a partial index on `expires_at` for expiry sweeps. A token is valid iff
`revoked_at IS NULL AND expires_at > now()`.

`revoked_reason` records the *cause* so the refresh-token reuse grace window
(issue #369) can grace only a `rotated` predecessor (a legitimate concurrent
refresh / lost-response retry): a `family`- or `logout`-revoked token is never
re-issued, so an attacker cannot escape a family revoke by re-presenting a
just-revoked successor inside the window.

---

## 5. Communities, membership & roles

### `community`

The isolation/ownership unit (FR-COMM-1). Created only by a platform administrator
(FR-COMM-2, decision #1).

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `name` | text | unique |
| `max_servers` | int nullable | **optional quota, unused in M1** (decision #9) |
| `max_members` | int nullable | **optional quota, unused in M1** (decision #9) |
| `created_at` / `updated_at` | timestamptz | |

The two `max_*` columns are the room-for-quotas left by decision #9: nullable,
unread by M1 business logic. They exist so a future milestone can enforce limits
without a schema change. M1 never writes or checks them.

### `membership`

The many-to-many join between `user` and `community` (FR-MEM-2). The unit a member
is added to (FR-MEM-1) and removed from (FR-MEM-3).

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | surrogate key, so joins/grants reference it cleanly |
| `user_id` | uuid FK → `user.id` | `ON DELETE CASCADE` |
| `community_id` | uuid FK → `community.id` | `ON DELETE CASCADE` |
| `created_at` | timestamptz | when the user joined |

Constraints: `UNIQUE(user_id, community_id)` — a user is a member of a Community
at most once.

### `role`

A Community-scoped named permission set (FR-COMM-4, FR-AUTHZ-4). Seeded with at
least an **Owner** role granting all permissions on Community creation; owners may
define more.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `community_id` | uuid FK → `community.id` | `ON DELETE CASCADE` |
| `name` | text | unique **within** the Community |
| `permissions` | text[] | set of `<resource>:<action>` codes (Appendix A) |
| `is_preset` | bool | seeded preset (e.g. Owner) vs owner-defined; default false |
| `created_at` / `updated_at` | timestamptz | |

Constraints: `UNIQUE(community_id, name)` — names are unique per Community, not
globally (FR-AUTHZ-4).

> `permissions` is a set of operation codes from
> [`REQUIREMENTS.md`](../REQUIREMENTS.md) Appendix A, stored as a text array. At
> this scale a permissions-codes array on the role is simpler than a
> `role_permission` join table and is queried as a whole when computing the
> effective set (FR-AUTHZ-2); the array is validated against the authoritative
> catalog in `domain`. A join table is the alternative if per-permission querying
> is ever needed — not in M1.

### `membership_role`

The join that assigns roles to a membership (Appendix B: "Membership — (user,
community) with role assignments"). A membership may hold several roles.

| Column | Type | Notes |
|---|---|---|
| `membership_id` | uuid FK → `membership.id` | `ON DELETE CASCADE` |
| `role_id` | uuid FK → `role.id` | `ON DELETE CASCADE` |

Primary key: composite `(membership_id, role_id)`. A role and the membership it is
assigned to are always in the same Community; this is an application invariant
(both FK to rows under one `community_id`), not enforceable by a single FK.

---

## 6. Authorization grants

### `resource_grant`

A permission scoped to a specific resource, granted to a specific member
(FR-AUTHZ-2). The effective permission set is `(role permissions in the
resource's Community) ∪ (resource grants to that member)`; this table is the
second term.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `user_id` | uuid FK → `user.id` | the granted member; `ON DELETE CASCADE` |
| `community_id` | uuid FK → `community.id` | the resource's Community; `ON DELETE CASCADE` |
| `resource_type` | text | e.g. `server` (CHECK-constrained enum) |
| `resource_id` | uuid | id of the specific resource (e.g. a `server.id`) |
| `permissions` | text[] | `<resource>:<action>` codes granted on that resource |
| `created_at` / `updated_at` | timestamptz | |

Constraints: `UNIQUE(user_id, resource_type, resource_id)` — one grant row per
member per resource (its `permissions` set is amended in place).

> The grant is keyed by `user_id`, not `membership_id`, because the grant is
> conceptually "to a user, on a resource that lives in a Community". To keep the
> FR-MEM-3 invariant (removing a member revokes that Community's grants), grants
> also carry `community_id` and are deleted when the membership is removed — see
> Section 10. `resource_id` is a soft reference (no DB-level FK) because
> `resource_type` is polymorphic; referential cleanup for the M1 resource type
> (server) is handled in the delete use case and by the membership-removal cascade.
> Because there is no FK on `resource_id`, deleting a single server does not
> remove its grants automatically: the server-delete use case must also delete the
> `resource_grant` rows for `(resource_type='server', resource_id=<server.id>)` in
> the same `UnitOfWork` transaction (Section 10), so no dangling grant rows remain.

---

## 7. Servers & workers

### `server`

The authoritative record of a Minecraft server (FR-SRV-3): identity, Community,
config, **desired** state, last-known **observed** state, execution backend, and
assigned Worker.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `community_id` | uuid FK → `community.id` | `ON DELETE CASCADE` |
| `name` | text | unique within the Community |
| `mc_edition` | text | e.g. `java` |
| `mc_version` | text | e.g. `1.21.1` (FR-SRV-1) |
| `server_type` | text | `vanilla` / `paper` / `fabric` / `forge` (CHECK enum). All are resolvable by the version catalog (forge resolves to the installer JAR — the worker runs `--installServer` on first start) |
| `config` | jsonb | server configuration blob (properties, JVM args, plus the reserved keys catalogued below) |
| `game_port` | integer nullable | the Minecraft game port (issue #243), assigned at create from the configured range (CONFIGURATION.md Section 5.8) and **unique deployment-wide**. Nullable: legacy/imported rows predating port tracking carry none, and Postgres treats `NULL`s as distinct so they never collide |
| `bedrock_port` | integer nullable | the server's public Bedrock **UDP** port (issue #1541), allocated from the dedicated UDP window (CONFIGURATION.md Section 5.8) when Geyser is detected among the server's plugins and released (`NULL`) on Geyser uninstall; a server delete drops the row and with it the port. **Unique deployment-wide.** Non-`NULL` *is* the Bedrock-enabled state — there is no separate boolean |
| `slug` | text | the relay hostname prefix (RELAY.md Section 3, issue #955), e.g. `amber-falcon-42`. Auto-generated at create as `<word>-<word>-<NN>` and **unique deployment-wide** (the hostname namespace is global). Renameable via the server update PATCH; released slugs are immediately reusable (owner decision, RELAY.md Section 17). Validated as a lowercase DNS label (`^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$`) with a reserved-word list (`www`, `api`, `relay`, …). Pre-existing rows are backfilled by migration 0016. |
| `backup_retention` | jsonb nullable | the scheduled-backup retention policy (issue #1841, migration 0032): `{"keep_last": N}` (keep the newest N, N ≥ 1) **or** `{"daily": D, "weekly": W, "monthly": M}` (keep the newest scheduled backup per UTC calendar day / ISO week / calendar month over those windows; each ≥ 0, at least one > 0). `NULL` means no retention — scheduled backups accumulate unbounded. Applies **only** to `source = scheduled` backup rows (Section 8); manual / uploaded / event rows are never auto-pruned. Validated in the domain (`RetentionPolicy`), not by a CHECK — the free-form `config` posture |
| `desired_state` | text | what the operator wants: `running` / `stopped` (CHECK enum) |
| `observed_state` | text | last state reported by the Worker: `starting` / `running` / `stopping` / `stopped` / `restarting` / `crashed` / `unknown` (CHECK enum) |
| `observed_at` | timestamptz nullable | when `observed_state` was last updated |
| `assigned_worker_id` | uuid nullable | the assigned Worker (FR-WRK-3); **no FK at M1** — there is no `worker` table (Section 7). A soft reference to the in-memory fleet registry; cleared by the API on Worker disconnect (FR-WRK-4) |
| `created_at` / `updated_at` | timestamptz | |

Constraints: `UNIQUE(community_id, name)`, `UNIQUE(game_port)` (deployment-wide,
`NULL`s allowed and non-colliding; issue #243), `UNIQUE(bedrock_port)`
(deployment-wide, `NULL`s allowed and non-colliding; issue #1541), `UNIQUE(slug)`
(deployment-wide, NOT NULL; issue #955). Index on `(assigned_worker_id)` for "all
servers on Worker X" (used on Worker disconnect, FR-WRK-4).

**Reserved `config` keys.** Alongside the free-form server properties and JVM
args, the `config` blob carries a small set of **reserved keys** with fixed
meaning. This table is the canonical list: a new reserved key must be registered
here when it is added, so the blob does not accumulate undocumented keys.

| Key | Unit / type | Set by | Feature / issue |
|---|---|---|---|
| `resolved_jar_sha256` | string (JAR content hash) | **system** — written by the start use case when a JAR is resolved; hidden from the config-overrides editor (issue #701) and never operator-settable | issue #118 |
| `resolved_jar_source` | string (source fingerprint) | **system** — written by the start use case alongside `resolved_jar_sha256`; records the catalog's published digest or URL so a subsequent start can detect upstream updates without re-downloading (issue #1676). Format: `"{algorithm}:{hash}"` (e.g. `sha1:ab…`, `sha256:cd…`) or `"url:{download_url}"` (Fabric) | issue #1676 |
| `snapshot_interval_seconds` | integer seconds | **operator** — per-server snapshot-cadence override (FR-DATA-7), clamped up to the configured floor | issue #107 |
| `memory_limit_mb` | integer mebibytes (MiB) | **operator** — per-server memory limit; absent means no limit (the JVM heap stays at its default). The worker derives the JVM heap from it; the `container` driver also enforces it as a *hard* ceiling (see [`CONFIGURATION.md`](CONFIGURATION.md) Section 6.3) | issue #705 |
| `cpu_millis` | integer millicores (1000 = one core) | **operator** — per-server CPU allocation; absent means no allocation (the driver's default share). A **soft, rough relative share** (owner decision), not a hard cap; the `container` driver translates it to a relative weight `CPUShares` that bites only under contention (see [`CONFIGURATION.md`](CONFIGURATION.md) Section 6.3) | issue #722 |

The operator-settable keys are validated on write (a bad value is `422`), and
every edit — a name, port, or any config key — is gated by `server:update`. The
system-written `resolved_jar_sha256` is not editable through the config-overrides
surface. `backup_interval_hours` was retired at the general-scheduler cutover
(issue #1840): it is no longer a reserved key, and a create/update still carrying
it is a `422` (`retired_config_key`) — the backup cadence is now a first-class
`backup` schedule (Section 8).

**Desired / observed split (FR-SRV-3, FR-SRV-4).** The two state columns are the
heart of the model. `desired_state` is the **source of truth for intent**, mutated
only by API operations (start/stop). `observed_state` + `observed_at` are written
**only** by the control-plane event handler from Worker reports (FR-SRV-4); they
are a cache of reality, never an authority. Divergence between them is normal and
expected (a server can be `desired=running, observed=crashed`); reconciliation
(re-issuing commands, marking servers on a dead Worker) reads both. This mirrors
[`ARCHITECTURE.md`](ARCHITECTURE.md) Section 3.3 (API holds desired state, Worker
reports observed state). The reportable values (`starting` / `running` /
`stopping` / `stopped` / `restarting` / `crashed`) mirror the control-plane
`ServerState` enum ([`CONTROL_PLANE.md`](CONTROL_PLANE.md)); `unknown` is
**API-inferred** — set by the API when the owning Worker disconnects and never
reported by a Worker, which is why the proto `ServerState` enum has no `UNKNOWN`
value.

**`assigned_worker_id` nullability and the missing FK.** A server is not
permanently pinned to a Worker (FR-WRK-6). The column is null when stopped/unplaced,
set on placement (FR-WRK-3), and cleared by the API if its Worker
disconnects/decommissions so the server can be re-placed after hydrate (FR-WRK-4).
The server row survives the Worker; the Worker holds no authoritative state. At M1
this is a **plain nullable uuid with no foreign key**: there is no durable `worker`
table to reference (Section 7), so clearing on disconnect is done by the API
against the in-memory fleet registry, not by a DB `ON DELETE SET NULL`. If a
`worker` table is added by a later migration, the FK (with `ON DELETE SET NULL`)
can be introduced alongside it without changing this column's semantics.

### `worker` — deferred beyond M1 (no table)

**Decision: M1 has no durable `worker` table.** Worker registration,
capabilities, and liveness live **only** in the in-memory `WorkerRegistry`
([`ARCHITECTURE.md`](ARCHITECTURE.md) Section 5.1), fed by the control stream.

*Rationale.* Workers are stateless and **re-register on every connect**
(FR-WRK-4), so the registry is rebuilt from live connections rather than read
from the DB on startup. Liveness (FR-WRK-2) is inherently runtime state, not
durable data. At M1 scale — a single API instance (NFR-SCALE-1) — a durable
worker table adds nothing the registry does not already provide, while creating a
DB ↔ registry sync liability (stale rows, write-through on every heartbeat). The
control plane already ships this way: the registry is in-memory (PRs #83/#86) and
`server.assigned_worker_id` is a plain nullable uuid with no FK (migration 0005,
PR #91).

*Future shape.* If a later milestone needs registrations to survive API restarts
or wants cross-instance visibility, a `worker` table can be added by a follow-up
migration **without breaking changes** — the `server.assigned_worker_id` column
already exists and the FK (`ON DELETE SET NULL`) is layered on at that time. The
deferred shape would be, roughly:

> | Column | Type | Notes |
> |---|---|---|
> | `id` | uuid PK | |
> | `name` | text | `UNIQUE`; operator-facing identifier |
> | `capabilities` | jsonb | advertised drivers + resources (FR-WRK-1), placement input (FR-WRK-3) |
> | `last_seen_at` | timestamptz nullable | last heartbeat (FR-WRK-2); a durable echo of live liveness, not the placement source of truth |
> | `created_at` / `updated_at` | timestamptz | |
>
> This sketch is **not** part of the M1 schema; it documents the intended future
> table only.

### Player groups (issue #276)

Reusable, Community-scoped player lists (OP / whitelist) attached to many servers
and synced to a server's `ops.json` / `whitelist.json`. Three normalized tables
(matching the relational model of the rest of this document, Section 2). The
group tooling lives in a `groups` slice **inside the servers bounded context** —
player groups are server-content tooling, not membership/authz, so the Community
context stays pure authorization. The Community-level permission codes guarding
the endpoints are `group:read` / `group:manage` (Appendix A).

#### `player_group`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `community_id` | uuid FK → `community.id` | `ON DELETE CASCADE` |
| `name` | text | unique within `(community_id, kind)` |
| `kind` | text | `op` / `whitelist` (CHECK enum); immutable for the group's lifetime (it selects the target file) |

Constraints: `UNIQUE(community_id, kind, name)` — a group name is unique per
Community per kind, so `op`/`admins` and `whitelist`/`admins` coexist.

#### `group_player`

A player row under a group (the player set). Membership is keyed by
`player_uuid` (the upsert key): adding an existing uuid updates its username.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `group_id` | uuid FK → `player_group.id` | `ON DELETE CASCADE` |
| `player_uuid` | uuid | the Minecraft account uuid (the file's stable key) |
| `username` | text | display name written into the file |

Constraints: `UNIQUE(group_id, player_uuid)` — one row per player per group.

#### `server_group`

The many-to-many attachment join between groups and servers.

| Column | Type | Notes |
|---|---|---|
| `group_id` | uuid FK → `player_group.id` | `ON DELETE CASCADE` |
| `server_id` | uuid FK → `server.id` | `ON DELETE CASCADE` |

Primary key: composite `(group_id, server_id)`. Both FKs cascade, so deleting a
group or a server tidies its attachments automatically. Index
`ix_server_group_server_id` on `server_id` covers the reverse lookup direction
(`list_groups_for_server`).

**File-sync posture (issue #276, the smallest honest M2 choice).** On any change
that affects an attached server's authoritative player file — attach, detach, or a
player add/remove on an attached group — the API regenerates that server's
`ops.json` (kind `op`; entries `{uuid, name, level, bypassesPlayerLimit}`, level
defaulting to 4) or `whitelist.json` (kind `whitelist`; entries `{uuid, name}`)
through the existing at-rest file write seam (versioned). The file is the
**union-merge** of every attached group of that kind, ordered by uuid so it is
byte-stable diff-to-diff. **Only at-rest servers are written**; a running or
otherwise unsettled server is left pending and ships the updated authoritative
copy on its next natural hydrate (hydrate always carries the authoritative working
set). Pushing live changes to a running server via the Worker (EditFile + RCON
reload) is deferred.

---

## 8. Backups, file history, sessions, resource packs & schedules

### `backup`

Retained-snapshot **metadata** for a server (FR-BAK-1, Appendix B). A backup is
effectively a retained snapshot and does **not** depend on a specific Worker
(FR-BAK-2). The archive bytes live behind the `Storage` Port (STORAGE.md, #17);
this row only points at them.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `server_id` | uuid FK → `server.id` | `ON DELETE CASCADE` |
| `storage_ref` | text | locator of the archive in `Storage` (opaque to the DB) |
| `size_bytes` | bigint nullable | recorded archive size; set at create/upload (issue #281). Legacy rows predating this stay NULL and are reported as "unknown" in statistics — an honest gap, not a wrong total |
| `source` | text | `manual` / `scheduled` / `event` / `uploaded` (CHECK enum). `uploaded` is an off-host archive brought in via the upload endpoint (issue #281; migration 0013 widened the CHECK) |
| `health` | text | `healthy` / `quarantined` / `unknown` (CHECK enum, issue #742; migration 0015). Structural health of the archived contents. A backup created through the integrity-gated create path (#749) is `healthy` by construction; legacy rows and `uploaded` archives (which bypass that gate) are `unknown` until the one-shot sweep (#744) classifies them; a row a check found corrupt is `quarantined`. NOT NULL, defaults `unknown` |
| `created_by` | uuid nullable | the user who triggered the backup; **soft reference** (no FK) so the row survives the actor's deletion (Section 9) |
| `created_at` | timestamptz | |

Constraints: index on `(server_id, created_at)` for listing a server's backups
newest-first. Deleting a backup row must also delete the archive in `Storage`;
that two-store cleanup is a use-case concern (orchestrated in `application`), not a
DB cascade.

Backups are downloadable and uploadable (issue #281): download streams the
archive in its native `tar.gz` form (no recompression); upload validates the
archive (it must open as a gzip tar with traversal-safe entries, bounded by the
shared upload cap) before storing it as a `source = uploaded` row, restorable
through the normal restore flow. Per-server (`backup:read`) and platform-admin
statistics endpoints aggregate count / total known bytes / unknown-size count /
newest+oldest from these rows.

> **Scheduled backups (FR-BAK-3)** need a schedule (cron-like) and execution
> history. The schedule belongs to the server (it can live in `server.config` as
> a per-server setting), and each run produces a `backup` row with
> `source = scheduled` — which *is* the execution history (a listing of scheduled
> rows with their `created_at`). A separate `backup_schedule` table is only needed
> if multiple named schedules per server are required; M1 does not.

`source = scheduled` rows are additionally subject to the server's
`backup_retention` policy (Section 7, issue #1841): after each successful
scheduled backup run — and on every policy write — the rows the policy no
longer keeps are pruned through the same archive-first/row-last delete path as
a manual delete, each deletion audited as `backup:delete` with no actor.
Manual / uploaded / event rows are never auto-pruned.

### `file_edit_history` — Storage-layer only (no DB table)

Versioned file changes for rollback (FR-FILE-3, Appendix B). Each edit retains the
prior version so a file can be rolled back to any retained version.

**Implementation note.** This entity has **no database table**. The original
design above described a relational version index; the actual implementation
stores prior file versions entirely in the `Storage` layer as immutable blobs
under `versions/<relative-file-path>/<version-id>` inside each server's
authoritative namespace (STORAGE.md Section 5). Version listing and rollback
are `Storage` Port operations (`list_file_versions`, `read_file_version`,
`rollback_file` — STORAGE.md Section 3.5), not database queries. The per-file
retention count is bounded by `storage.version_retention` (CONFIGURATION.md
Section 5.2). There is no `file_edit_history` table in the migration set.

### Game sessions (issue #957)

Relay-originated player session records (RELAY.md Section 8, Section 14). One row
per accepted **login** session recorded by the relay; status pings are not
recorded. Populated via `ReportSessions` (RELAY.md Section 6). `username` /
`player_uuid` are the values *claimed* in Login Start — pre-authentication (with
`online-mode` on, a session of meaningful duration implies a verified identity;
the column is labelled "claimed identity" in the UI).

#### `game_session`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | Relay-minted session id (the `ReportSessions` idempotency key) |
| `server_id` | uuid FK → `server.id` nullable | `ON DELETE CASCADE`; indexed with `started_at` for newest-first listing. Nullable so a `SessionEnd` arriving before its `SessionStart` (an out-of-order batch retry) can create a placeholder row |
| `hostname` | text nullable | The slug actually used at join (slugs are renameable; this is the historical value) |
| `player_ip` | inet nullable | The player's source address as seen by the relay |
| `username` | text nullable | Claimed in Login Start (RELAY.md Section 8) |
| `player_uuid` | uuid nullable | Claimed; present on protocols that send it |
| `started_at` | timestamptz nullable | |
| `ended_at` | timestamptz nullable | NULL = still open (or relay crashed; healed on `Register`) |

Index: `(server_id, started_at)` for the newest-first paginated listing.
Rows cascade away when their server is deleted. A retention prune loop
(`relay.session_retention_days`, default 90) deletes rows older than the window.

Access is gated by the `session:read` permission (RELAY.md Section 8, Appendix A);
player IPs are PII so the data is role-restricted.

### Resource packs (issue #1175)

Global resource pack library (not community-scoped) and per-server assignment. Two
tables added by migration 0018.

#### `resource_packs`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `filename` | text | original upload filename |
| `display_name` | text | operator-chosen display name |
| `description` | text nullable | optional description |
| `sha1_hash` | text(40) | SHA-1 digest (used by the Minecraft client for resource pack verification) |
| `sha256_hash` | text(64) | SHA-256 digest |
| `size_bytes` | bigint | archive size |
| `uploaded_by` | uuid | **soft reference** (no FK) — the row survives the actor's deletion |
| `created_at` / `updated_at` | timestamptz | |

#### `server_resource_pack_assignments`

At most one resource pack per server (the primary key is `server_id`). Index
`ix_srv_rp_assignments_resource_pack_id` on `resource_pack_id` covers the
reverse lookup (`list_assignments_for_pack`).

| Column | Type | Notes |
|---|---|---|
| `server_id` | uuid PK, FK → `server.id` | `ON DELETE CASCADE` |
| `resource_pack_id` | uuid FK → `resource_packs.id` | |
| `require_resource_pack` | bool | whether the client must accept the pack; default false |
| `resource_pack_prompt` | text nullable | optional prompt message shown to the player |
| `assigned_by` | uuid | **soft reference** (no FK) |
| `created_at` / `updated_at` | timestamptz | |

### Schedules (epic #649)

The general scheduler: per-server recurring actions and their execution
history (migration 0029). A schedule fires on a **cron expression XOR a fixed
interval**, evaluated in a per-schedule IANA timezone (cron) or as absolute
elapsed time with a deterministic per-schedule jitter (interval). The runner
(#1838) polls `next_run_at` over enabled schedules. The legacy FR-BAK-3
`backup_interval_hours` config cadence is retired into `action = backup`
schedules (issue #1840): migration 0031 converts each server that carries the key
into an equivalent enabled interval `backup` schedule (named "Scheduled backup",
`next_run_at` staggered within ~1 hour so the runner picks it up soon without a
fleet-wide thundering herd) and strips the key. A failed scheduled backup no
longer retries every tick — it gets the runner's bounded retry (one, ~30 minutes)
plus a notification, then waits for the next occurrence.

#### `schedule`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `server_id` | uuid FK → `server.id` | `ON DELETE CASCADE`; lookups ride the `UNIQUE(server_id, name)` index (leading column) |
| `name` | text | operator-chosen; `UNIQUE(server_id, name)` |
| `action` | text | `command` / `start` / `stop` / `restart` / `backup` (CHECK enum) |
| `payload` | jsonb | per-action payload: `{"command": <line>}` for `command`; optional `{"warnings": [{offset_minutes, message}, …]}` for `stop`/`restart` (≤ 5 steps, offsets positive, distinct, ≤ 120 min); `{}` otherwise |
| `cron` | text nullable | 5-field cron expression |
| `interval_seconds` | int nullable | fixed interval, minimum 60 s (domain-validated, matching cron's minute granularity); CHECK exactly one of `cron` / `interval_seconds` is non-NULL (`ck_schedule_cadence_xor`) |
| `timezone` | text | IANA zone the cron cadence is evaluated in; NOT NULL, defaults `UTC` |
| `enabled` | bool | NOT NULL |
| `next_run_at` | timestamptz nullable | the due instant the runner polls on; NULL exactly while disabled. Partial index `ix_schedule_next_run_at` on `(next_run_at) WHERE enabled` |
| `last_run_at` | timestamptz nullable | |
| `created_by` | uuid nullable | **soft reference** (no FK) so the row survives the actor's deletion (Section 9) |
| `created_at` / `updated_at` | timestamptz | |

#### `schedule_run`

One row per fired occurrence, written after it completes. History is capped at
50 rows per schedule by the runner's prune (#1838), not by the schema.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `schedule_id` | uuid FK → `schedule.id` | `ON DELETE CASCADE`; indexed with `started_at` for the newest-first history listing |
| `started_at` / `finished_at` | timestamptz | |
| `outcome` | text | `success` / `failure` / `skipped` (CHECK enum); `skipped` records a fired occurrence whose precondition was unmet (e.g. stop while not running) |
| `detail` | text nullable | sanitized outcome note (a failure category, a skip reason) — never a raw worker/OS message |

---

## 9. Audit log

### `audit_log`

The activity trail (FR-AUD-1): actor, Community, operation, target, outcome,
timestamp. Written **fire-after-commit, must-not-raise** (FR-AUD-2) by the
`AuditWriter` Port ([`ARCHITECTURE.md`](ARCHITECTURE.md) Section 5.1) — a row is
appended only after the business transaction commits, and a failed audit write
never rolls back or raises into the operation. Append-only.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `actor_id` | uuid nullable | the acting user; **soft reference** (no FK) so the row survives the actor's deletion |
| `community_id` | uuid nullable | the scope, for member-scoped queries (FR-AUD-3); soft reference |
| `operation` | text | the `<resource>:<action>` code (Appendix A) |
| `target_type` | text nullable | e.g. `server` |
| `target_id` | uuid nullable | the affected resource id; soft reference |
| `outcome` | text | `success` / `denied` / `error` (CHECK enum) |
| `created_at` | timestamptz | the event time |

**No foreign keys on purpose.** `actor_id`, `community_id`, and `target_id` are
deliberately *not* FKs: an audit trail must outlive the entities it describes
(deleting a user or Community must not erase the record of what they did, and must
not be blocked by audit rows). The IDs are stored as plain values for forensic
reference. This is the one place the model abandons referential integrity, by
design.

Indexes: `(community_id, created_at)` for member-scoped, Community-bounded queries
(FR-AUD-3); `(actor_id, created_at)` for "what did this user do"; `(created_at)`
for the platform-admin global view.

---

## 10. Cascade behavior on member removal

FR-MEM-3 is the sharpest consistency requirement: **removing a member revokes
that Community's roles and resource grants for the user** — and *only* that
Community's, leaving the user's other memberships untouched.

Removing a member deletes the user's `membership` row for that Community. The
following must then be gone for that `(user, community)` pair, and nothing else:

| What | How it is removed |
|---|---|
| Role assignments in this Community | `membership_role` rows `ON DELETE CASCADE` from the deleted `membership` |
| Resource grants in this Community | `resource_grant` rows for `(user_id, community_id)` — deleted by the remove-member **use case** (they FK `user_id`, not `membership_id`) |
| The membership itself | the `membership` row is the delete target |

What must **not** be touched:

- The `user` (global; may belong to other Communities — FR-AUTH-5).
- The user's memberships, roles, or grants in **other** Communities.
- `role` rows themselves (a role is the Community's, shared by members; deleting a
  member removes their *assignment*, not the role definition).
- `audit_log` rows referencing the user (soft references; Section 9).

Distinct from member removal, **deleting a whole Community** cascades to its
`membership`, `role`, `membership_role`, `resource_grant`, `server` (and thence
`backup`, `file_edit_history`) rows via `ON DELETE CASCADE`, while `audit_log`
keeps its soft-referenced history.

Also distinct, **deleting a single server** (without deleting its Community) must
sweep the `resource_grant` rows that point at it. Since `resource_id` is a soft
reference (no FK; Section 6), this is not automatic: the server-delete use case
deletes the `resource_grant` rows for `(resource_type='server', resource_id=<server.id>)`
in the same `UnitOfWork` transaction as the `server` row — the same pattern as the
member-removal grant cleanup — so no dangling polymorphic grant rows remain.

The two grant-cleanup paths above (cascade vs use-case) exist because
`resource_grant` is keyed by `user_id` for natural querying (Section 6); the
remove-member use case is responsible for deleting the matching grants in the same
`UnitOfWork` transaction as the membership deletion, so the FR-MEM-3 invariant
holds atomically.

---

## 11. Related documents

| Doc | Covers |
|---|---|
| [`../REQUIREMENTS.md`](../REQUIREMENTS.md) | What v2 must do; entity sketch (Appendix B), permission catalog (Appendix A), resolved decisions (Section 9) |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Hexagonal layering, the persistence Port (`<Entity>Repository` + `UnitOfWork`), the desired/observed authority split |
| [`STORAGE.md`](STORAGE.md) | The `Storage` Port: world/JAR/backup-archive bytes that this model references but does not store |
| [`CONFIGURATION.md`](CONFIGURATION.md) | Runtime configuration & adapter selection (incl. which persistence adapter is bound) |
