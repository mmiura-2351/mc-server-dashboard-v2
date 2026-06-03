# Database

> Status: **Design** · Audience: contributors to `api/`
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
> a retained snapshot; the archive bytes are in `Storage`. Migration tooling (how
> the schema is versioned and applied) lands with epic #3 and is **out of scope**
> here. Runtime configuration is in `CONFIGURATION.md` (issue #16).

## Table of Contents

1. [Persistence technology](#1-persistence-technology)
2. [Conventions](#2-conventions)
3. [Entity relationship overview](#3-entity-relationship-overview)
4. [Authentication & identity](#4-authentication--identity)
5. [Communities, membership & roles](#5-communities-membership--roles)
6. [Authorization grants](#6-authorization-grants)
7. [Servers & workers](#7-servers--workers)
8. [Backups & file history](#8-backups--file-history)
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

The concrete migration toolchain (Alembic or equivalent) is **not** decided here;
it lands with epic #3.

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
  migration tooling (epic #3).
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
   ┌───┴────┐ 1      N ┌──────────┐ N      1 ┌──────────┐
   │community│────────▶│  server  │─────────▶│  worker  │ (assigned, nullable)
   └────────┘          └────┬─────┘          └──────────┘
                            │ 1
                ┌───────────┼────────────────┐
                │ N         │ N
          ┌─────┴─────┐ ┌───┴──────────────┐
          │  backup   │ │ file_edit_history │
          └───────────┘ └───────────────────┘
```

Reading aids:

- **Membership is many-to-many** (FR-MEM-2): `membership` is the join between
  `user` and `community`. Roles held within a Community attach to the
  *membership*, not the user, via the `membership_role` join — so the same user
  holds different roles in different Communities (FR-AUTHZ-4).
- **Roles are Community-scoped** (FR-AUTHZ-4): a `role` belongs to exactly one
  `community`; the same name in two Communities is two independent rows.
- **A `server` belongs to one `community`** (FR-SRV-3) and is optionally assigned
  to one `worker` (FR-WRK-3, nullable — unassigned when stopped/unplaced).
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
| `is_platform_admin` | bool | the admin axis (FR-AUTH-6, FR-AUTHZ-5); default false |
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

Constraints: `UNIQUE(token_hash)`. Index on `(user_id)` for "revoke all sessions"
and a partial index on `expires_at` for expiry sweeps. A token is valid iff
`revoked_at IS NULL AND expires_at > now()`.

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
| `server_type` | text | `vanilla` / `paper` / `forge` / … (CHECK enum) |
| `execution_backend` | text | `host_process` / `container` (CHECK enum) |
| `config` | jsonb | server configuration blob (properties, JVM args, snapshot-interval override per FR-DATA-7) |
| `desired_state` | text | what the operator wants: `running` / `stopped` (CHECK enum) |
| `observed_state` | text | last state reported by the Worker: `starting` / `running` / `stopping` / `stopped` / `restarting` / `crashed` / `unknown` (CHECK enum) |
| `observed_at` | timestamptz nullable | when `observed_state` was last updated |
| `assigned_worker_id` | uuid FK → `worker.id` nullable | `ON DELETE SET NULL` (FR-WRK-4) |
| `created_at` / `updated_at` | timestamptz | |

Constraints: `UNIQUE(community_id, name)`. Index on `(assigned_worker_id)` for
"all servers on Worker X" (used on Worker disconnect, FR-WRK-4).

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

**`execution_backend` immutability.** The backend is stored as a plain column but
is **immutable for the server's lifetime** in M1 (FR-EXE-3,
[`ARCHITECTURE.md`](ARCHITECTURE.md) Section 7.1). The constraint is enforced as a
policy in the update use case, not by the schema — keeping it a normal column
means a future milestone can lift the policy to a supported relocation operation
without a schema change.

**`assigned_worker_id` nullability.** A server is not permanently pinned to a
Worker (FR-WRK-6). It is null when stopped/unplaced, set on placement (FR-WRK-3),
and cleared (`ON DELETE SET NULL`) if its Worker disconnects/decommissions so the
server can be re-placed after hydrate (FR-WRK-4). The server row survives the
Worker; the Worker holds no authoritative state.

### `worker`

A registered execution host (FR-WRK-1): capabilities and liveness. Liveness is
tracked over the live control stream (FR-WRK-2) and is fundamentally **runtime
state**, not durable data; this table is the durable registration record, with a
last-seen cache for observability.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `name` | text | unique; operator-facing identifier |
| `capabilities` | jsonb | advertised drivers + resources (FR-WRK-1), placement input (FR-WRK-3) |
| `last_seen_at` | timestamptz nullable | last heartbeat (FR-WRK-2); a cache of live liveness |
| `created_at` / `updated_at` | timestamptz | |

Constraints: `UNIQUE(name)`.

> The authoritative liveness/least-loaded view used by greedy placement
> (FR-WRK-3) lives in the in-memory `WorkerRegistry`
> ([`ARCHITECTURE.md`](ARCHITECTURE.md) Section 5.1), fed by the stream. This
> table persists registration so a Worker is known across API restarts;
> `last_seen_at` is a best-effort durable echo, not the placement source of truth.

---

## 8. Backups & file history

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
| `size_bytes` | bigint nullable | recorded archive size |
| `source` | text | `manual` / `scheduled` / `event` (CHECK enum) |
| `created_at` | timestamptz | |

Constraints: index on `(server_id, created_at)` for listing a server's backups
newest-first. Deleting a backup row must also delete the archive in `Storage`;
that two-store cleanup is a use-case concern (orchestrated in `application`), not a
DB cascade.

> **Scheduled backups (FR-BAK-3)** need a schedule (cron-like) and execution
> history. The schedule belongs to the server (it can live in `server.config` as
> a per-server setting), and each run produces a `backup` row with
> `source = scheduled` — which *is* the execution history (a listing of scheduled
> rows with their `created_at`). A separate `backup_schedule` table is only needed
> if multiple named schedules per server are required; M1 does not.

### `file_edit_history`

Versioned file changes for rollback (FR-FILE-3, Appendix B). Each edit retains the
prior version so a file can be rolled back to any retained version.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `server_id` | uuid FK → `server.id` | `ON DELETE CASCADE` |
| `path` | text | server-relative file path (path-traversal-safe, FR-FILE-4) |
| `version` | int | monotonically increasing per `(server_id, path)` |
| `content_ref` | text | locator of the retained content in `Storage` |
| `edited_by` | uuid FK → `user.id` nullable | `ON DELETE SET NULL` (keep history if user gone) |
| `created_at` | timestamptz | |

Constraints: `UNIQUE(server_id, path, version)`; index on `(server_id, path,
version DESC)` for "latest version" and rollback listing.

> Retained file contents are bytes and so belong behind `Storage` (#17), not
> inline in the DB; this table is the **version index** that points at them.
> Small text files could alternatively be stored inline, but keeping all artifact
> bytes in one place (`Storage`) keeps the DB metadata-only, consistent with the
> document's scope.

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
| [`STORAGE.md`](STORAGE.md) *(planned, #17)* | The `Storage` Port: world/JAR/backup-archive bytes that this model references but does not store |
| [`CONFIGURATION.md`](CONFIGURATION.md) *(planned, #16)* | Runtime configuration & adapter selection (incl. which persistence adapter is bound) |
