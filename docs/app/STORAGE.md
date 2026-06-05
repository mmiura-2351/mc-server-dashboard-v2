# Storage

> Status: **Design** · Audience: contributors to `api/`
>
> This document defines the API-side authoritative store: the `Storage` Port
> contract, the layout of authoritative data, the atomic snapshot-publish
> mechanism, file version retention, path-traversal protection, and the three
> adapter families (fs / remote-fs / object). It refines, but does not
> contradict, [`../REQUIREMENTS.md`](../REQUIREMENTS.md) (especially Sections
> 6.8–6.12, FR-DATA-1…7, FR-FILE-3, FR-FILE-4) and is consistent with the
> Ports catalog and layering in [`ARCHITECTURE.md`](ARCHITECTURE.md). Where this
> document and the requirements disagree, the requirements win and this document
> is wrong.

## Table of Contents

1. [Scope & posture](#1-scope--posture)
2. [Authoritative data layout](#2-authoritative-data-layout)
3. [The `Storage` Port contract](#3-the-storage-port-contract)
4. [Atomic publish](#4-atomic-publish)
5. [File version retention](#5-file-version-retention)
6. [Path-traversal protection](#6-path-traversal-protection)
7. [Adapter families](#7-adapter-families)
8. [Data plane (HTTP transfer)](#8-data-plane-http-transfer)
9. [Design decisions](#9-design-decisions)
10. [Related documents](#10-related-documents)

---

## 1. Scope & posture

The `Storage` Port is the API-side authoritative store for **world data, server
JARs, and backups** (REQUIREMENTS.md FR-DATA-1). It is the **only** component
that touches the pluggable backend; the Worker is storage-backend-agnostic and
reaches authoritative data exclusively through the API-mediated data plane
(FR-DATA-3). The backend is config-selected — filesystem, remote filesystem, or
object storage — and switching it is a configuration change, not a code change
(FR-DATA-2).

This document specifies **what the Port guarantees**, not how the bytes move
between API and Worker. The data-plane HTTP transfer endpoints (hydrate /
snapshot wire format, chunking, resumption) are epic #8 and out of scope here;
this document only fixes the API-side store contract that those endpoints plug
into. Where the data plane meets `Storage`, the boundary is named but not
specified (Sections 3.1, 4).

Target scale is small (NFR-SCALE-1): a few dozen Communities, tens of concurrent
servers, a single API instance. The contract is therefore deliberately thin —
enough operations to satisfy Sections 6.8–6.12, no speculative generality — while
keeping the *shape* backend-neutral so a larger backend can replace the M1 one
without touching business logic (REQUIREMENTS.md Section 1.1).

### 1.1 What lives behind the Port vs. above it

- **Blob layout** (the byte-level on-backend layout of working sets, JARs,
  backup archives, and file versions) is owned by this document.
- **Metadata** (the `Server`, `Backup`, `FileEditHistory` rows that index those
  blobs, schedules, audit) lives in the database and is owned by
  `DATABASE.md` (#15). `Storage` returns opaque keys/handles; the metadata
  layer records them. The Port never queries the database and the database never
  reaches into the backend.
- **Config key names** for selecting and tuning a backend are owned by
  `CONFIGURATION.md` (#16). This document defines **what** is selectable and
  tunable (the backend family, the snapshot/version-retention knobs); #16 names
  the keys.

---

## 2. Authoritative data layout

Authoritative data is namespaced **per Community, then per server**, mirroring
the isolation unit in REQUIREMENTS.md Section 6.2. JARs are **shared across all
Communities** because they are immutable, content-addressed artifacts reused
across servers (FR-VER-3) and contain no Community data.

```
<root>/
├── communities/
│   └── <community-id>/
│       └── servers/
│           └── <server-id>/
│               ├── current -> snapshots/<snapshot-id>/  # symlink to the live snapshot; the publish pointer (Section 4)
│               ├── snapshots/                # published working-set snapshots; current points at the live one
│               │   └── <snapshot-id>/        # the full working directory for one published state
│               │       ├── world/
│               │       ├── server.properties
│               │       └── ...
│               ├── incoming/                 # staging area for in-flight snapshot/restore transfers (Section 4)
│               │   ├── <transfer-id>/        # an in-flight snapshot being streamed in
│               │   └── restore-<backup-id>/  # a backup being extracted before publish (Section 4.1)
│               ├── versions/                 # retained per-file versions for rollback (Section 5)
│               │   └── <relative-file-path>/
│               │       ├── <version-id>      # an immutable prior content of that file
│               │       └── ...
│               └── backups/
│                   ├── <backup-id>.<archive-ext>  # a retained, self-contained snapshot archive
│                   └── ...
└── jars/                                     # shared, content-addressed server JARs (FR-VER-3)
    └── <sha256>.jar
```

Notes:

- `current` is a **symlink** to the live entry under `snapshots/`; it names the
  authoritative copy and is the atomic-publish pointer (Section 4.2).
  Dereferencing `current/` reaches the authoritative working set. While a server
  is running that copy is temporarily stale (FR-DATA-4); `Storage` does not know
  server state — the application layer decides when to read `current/` vs.
  read-through to the Worker per the Section 6.9 state-branching policy.
- `snapshots/<snapshot-id>/` holds published working-set states. Only the one
  targeted by the `current` symlink is authoritative; superseded snapshots are
  reclaimed after a publish (Section 4.3). Single-file edits (Section 4.4) mutate
  the live snapshot in place via a temp-sibling rename, atomic at file
  granularity; whole-working-set publishes mint a new snapshot and flip the
  pointer.
- `<community-id>` and `<server-id>` are opaque identifiers minted by the API
  (not user-supplied names), so the namespace cannot collide and is not subject
  to path-traversal from naming (Section 6).
- `incoming/<transfer-id>/` exists only during a snapshot transfer; it is the
  partial copy that must never become authoritative until the transfer is proven
  complete (Section 4).
- `backups/` holds whole-working-set archives. A backup is a retained snapshot
  that does **not** depend on a specific Worker (FR-BAK-2); restoring one
  republishes it into `current/`. The archive codec (`<archive-ext>`) is
  **adapter-internal** — callers hold only the opaque `BackupKey` and never the
  on-disk name; the M1 `fs` adapter uses gzip (`.tar.gz`), with zstd deferred.
- On object backends the tree above is a **key prefix scheme**, not real
  directories (Section 7.3); the same logical layout applies.

---

## 3. The `Storage` Port contract

The Port is defined in `api/`'s domain layer and depended on by use cases
(ARCHITECTURE.md Section 5.1). Every operation is scoped by an explicit
`(community_id, server_id)` (or, for JARs, unscoped) so the adapter can resolve
the namespace and enforce isolation; no operation accepts an absolute path.

Operations are grouped by the requirement area they serve. Signatures are
language-neutral sketches; the binding Python interface lands with the toolchain.

### 3.1 Working-set hydrate / snapshot

Serve the runtime data lifecycle (FR-DATA-4). These move the whole working set
between `current/` and the data plane; they do **not** transfer to the Worker
directly (that is the data-plane endpoint's job, epic #8). `Storage` provides the
authoritative-side stream and the atomic-publish handshake.

| Operation | Purpose | Notes |
|---|---|---|
| `open_hydrate_source(community_id, server_id) -> ReadStream` | Open a read stream over the current authoritative working set | The data plane reads from this to feed a Worker on start/relocation (hydrate). Reads `current/`. |
| `begin_snapshot(community_id, server_id) -> SnapshotHandle` | Start an incoming snapshot transfer | Allocates an isolated `incoming/<transfer-id>/` staging area. |
| `write_snapshot(handle, WriteStream)` | Stream the Worker's working set into staging | Writes only into staging, never `current/`. May be called incrementally. |
| `commit_snapshot(handle)` | Atomically publish the staged snapshot as the new authoritative copy | Atomic publish (Section 4). After return, `current/` reflects the complete transfer or the prior copy — never a partial. |
| `abort_snapshot(handle)` | Discard an incomplete/failed transfer | Deletes the staging area; `current/` is untouched. Also the cleanup path for crash recovery (Section 4.3). |

The hydrate/snapshot **wire transport** (how `ReadStream`/`WriteStream` bytes
cross the API↔Worker boundary) is the data plane (epic #8). `Storage` only
guarantees the authoritative-side semantics above.

### 3.2 JAR store / reuse

Serve version management (FR-VER-3). JARs are immutable and content-addressed by
their SHA-256, so an identical JAR is stored once and reused across servers and
Communities.

| Operation | Purpose | Notes |
|---|---|---|
| `put_jar(ReadStream) -> JarKey` | Store a JAR, returning its content key | Idempotent: storing the same bytes yields the same key and no duplicate. Key = `sha256`. |
| `has_jar(JarKey) -> bool` | Test presence before downloading from an external source | Lets the `VersionCatalog` adapter skip a redundant fetch. |
| `open_jar(JarKey) -> ReadStream` | Read a stored JAR | The data plane streams this to a Worker as part of hydrate. |

The API never deletes JARs implicitly in M1 (a JAR may be referenced by any
server). JAR garbage collection is a deferred concern (Section 8.5).

### 3.3 Backup archive create / list / restore / delete

Serve backup management (FR-BAK-1, FR-BAK-2, FR-BAK-4). A backup is a
self-contained archive of a working set; it does not depend on a Worker.

| Operation | Purpose | Notes |
|---|---|---|
| `create_backup_from_current(community_id, server_id) -> BackupKey` | Archive the authoritative `current/` into `backups/` | The **stopped-server** path (Section 6.9). For a **running** server the application first does `save-all` → on-demand snapshot (`commit_snapshot`) → then calls this; `Storage` only ever archives the authoritative copy. |
| `list_backups(community_id, server_id) -> [BackupKey]` | Enumerate a server's backups | Metadata (label, timestamp, size) lives in the DB (#15); this returns the keys. |
| `restore_backup(community_id, server_id, BackupKey)` | Atomically republish a backup into `current/` | Atomic publish (Section 4). Caller must ensure the server is **stopped** (FR-BAK-4); `Storage` enforces atomicity, the application enforces the stop precondition. |
| `delete_backup(community_id, server_id, BackupKey)` | Remove a backup archive | Idempotent. |

### 3.4 File read / edit on the authoritative copy

Serve file management for **stopped** servers (FR-FILE-1, FR-FILE-2). For a
running server, file ops route over the control plane to the Worker's live
working set and do **not** touch `Storage` (ARCHITECTURE.md Section 7.2); those
paths are not part of this Port.

| Operation | Purpose | Notes |
|---|---|---|
| `read_file(community_id, server_id, rel_path) -> bytes` | Read one file from `current/` | `rel_path` is validated against traversal (Section 6). |
| `list_dir(community_id, server_id, rel_path) -> [entry]` | Browse a directory in `current/` | Same path validation. |
| `write_file(community_id, server_id, rel_path, bytes)` | Edit one file in `current/`, retaining the prior version | Captures the previous content into `versions/` (Section 5) before overwriting. The per-file write is atomic (Section 4.4). |
| `delete_file(community_id, server_id, rel_path)` | Delete one file from `current/`, retaining the prior content | Captures the content into `versions/` (Section 5) **before** removing, so a delete is reversible by rollback exactly like an edit. Missing path → `NotFoundError`. |
| `delete_dir(community_id, server_id, rel_path)` | Recursively delete a directory subtree from `current/` | **No** per-file version capture: file versioning (Section 5) is the fine-grained single-file mechanism, whereas whole-subtree recovery is what backups (Section 3.3) exist for; capturing a version per member of a large subtree would be a storage-amplification bomb. Missing dir → `NotFoundError`. |
| `make_dir(community_id, server_id, rel_path)` | Create an (empty) directory in `current/` | Backend-dependent (see note). Idempotent. |

**Empty-directory limitation (`make_dir`).** fs / remote-fs materialize a real
empty directory, which rides the hydrate tar as a directory member (the tar is
built recursively, so empty dirs survive a snapshot round-trip). **Object storage
has no real directories** — a directory exists only as the shared key-prefix of
its files (Section 7.3), so an *empty* directory cannot be represented and
`make_dir` is a no-op there; the directory becomes observable once a file is
written under it. This is documented honestly rather than papered over with a
marker object that would pollute listings.

### 3.5 File version retention / rollback

Serve versioned edits (FR-FILE-3). See Section 5 for the retention scheme.

| Operation | Purpose | Notes |
|---|---|---|
| `list_file_versions(community_id, server_id, rel_path) -> [VersionId]` | List retained prior versions of a file | Ordered newest-first. Version metadata is indexed in the DB (#15). |
| `read_file_version(community_id, server_id, rel_path, VersionId) -> bytes` | Read a specific retained version | For preview/diff before rollback. |
| `rollback_file(community_id, server_id, rel_path, VersionId)` | Restore a file to a retained version | Implemented as a `write_file` of the old content, so the pre-rollback content is itself retained (rollback is reversible). |

---

## 4. Atomic publish

**Requirement (FR-DATA-6).** A snapshot or backup restore must never overwrite
the authoritative copy with a partial transfer. The authoritative `current/`
must, at every instant and after any crash, reflect either the *previous*
complete state or a *new* complete state — never a half-written mix.

### 4.1 The staging-then-publish protocol

Every operation that replaces `current/` (snapshot commit, backup restore)
follows the same two-phase shape:

1. **Stage.** Write the entire new working set into an isolated, non-authoritative
   location (`incoming/<transfer-id>/` for snapshots; `incoming/restore-<backup-id>/`
   for restores). `current/` is untouched throughout.
2. **Publish.** Once the staged copy is **proven complete**, move it into
   `snapshots/<snapshot-id>/` and make it authoritative by a single atomic
   pointer switch (Section 4.2). The pointer switch is the only step that changes
   what `current` resolves to. Only after it succeeds is the superseded snapshot
   reclaimed.

A transfer is "proven complete" before publish by the caller signalling
end-of-stream plus an integrity check (size/manifest match between what the
Worker sent and what landed in staging). The exact completeness signal is part of
the data-plane contract (epic #8); `Storage.commit_snapshot` is the gate that
refuses to publish without it.

An empty staging area is not a publishable transfer: a worker packing an empty
working set is a bug signal, never a valid snapshot. `Storage.commit_snapshot`
refuses it with `IncompleteTransferError`, which the snapshot endpoint surfaces
as `400 empty_snapshot` (Section 8).

### 4.2 What "single atomic step" means per backend family

The publish step's atomicity is realized differently per adapter family, but the
*observable guarantee* is identical (Section 7 tabulates it per adapter):

- **fs / remote-fs:** publish is a **`current` symlink flip**, mirroring the
  object backend's pointer design. The staged copy is first moved into
  `snapshots/<snapshot-id>/` (a fresh, never-before-published name, so this move
  never overwrites anything authoritative). Then a new symlink is created at a
  temporary name pointing at that snapshot and **atomically renamed over
  `current`** (`rename(2)` of one symlink onto another, within the server
  directory). Same-directory rename is atomic on POSIX filesystems, so `current`
  resolves to either the old snapshot or the new one at every instant — it is
  never absent and never partial. The superseded snapshot is deleted only after
  the flip returns.

  This is chosen over `renameat2(RENAME_EXCHANGE)` (Linux-only; commonly
  unsupported over NFS/SMB) and over deleting `current/` then renaming the new
  copy into place (which leaves a window where `current` does not exist — the
  exact defect being fixed). A symlink flip needs only atomic same-directory
  rename, which is already the guarantee the remote-fs adapter requires of its
  mount (Section 7.2), so it is the portable option across all path-based
  backends.
- **object:** there is no atomic multi-object rename, so the published state is
  named by a **single pointer object** (a small `current.json`-style manifest
  listing the object keys that make up the live working set). Publish writes all
  data objects under a fresh, unique prefix, then **atomically overwrites the one
  pointer object** to reference the new prefix. The pointer flip is a single
  object PUT, which object stores serve atomically (last-writer-wins,
  read-after-write). The prior prefix's objects are garbage-collected after the
  flip (Section 7.3).

For the fs / remote-fs backends, after the symlink rename the parent directory is
fsynced so the flip survives power loss (symmetric with the Section 4.4 single-file
fsync note); a flip that is lost despite being complete is otherwise bounded by the
FR-DATA-5 RPO.

### 4.3 Crash safety

| Crash point | fs / remote-fs | object |
|---|---|---|
| During **stage** (writing `incoming/`) | `current` still points at the old snapshot; orphan staging dir is cleaned by `abort_snapshot` or a startup sweep | pointer still references old prefix; orphan data objects under the un-referenced prefix are GC'd |
| After stage, before the staged copy is **moved into `snapshots/`** | `current` unchanged (old snapshot); the staged dir in `incoming/` is an orphan, cleaned on recovery | pointer still references old prefix; new prefix is orphaned and GC'd |
| After move into `snapshots/`, before the **symlink flip** | `current` still points at the old snapshot; the freshly moved `snapshots/<snapshot-id>/` is not yet referenced, so it is an orphan, cleaned by the sweep (it is unreferenced because no symlink targets it) | (no analogue — object stage writes directly under the new prefix) |
| During the **symlink flip** | same-directory rename is atomic — `current` resolves to either the old or the new snapshot; it is never absent and never a partial pointer | the pointer PUT is atomic — it either references the old or the new prefix |
| After the flip, before superseded-snapshot reclaim | `current` points at the new snapshot; the superseded `snapshots/<snapshot-id>/` is an orphan (unreferenced by `current`), cleaned on recovery | pointer references the new prefix; old prefix is an orphan, GC'd |

The invariant in every row: **`current` (or the object pointer) always resolves
to one complete snapshot — never absent, never partial.** Recovery is
idempotent: the startup sweep reclaims any `snapshots/<snapshot-id>/` not
targeted by `current` and any leftover `incoming/` staging dir, and re-running
`abort_snapshot` or the sweep is always safe.

### 4.4 Single-file writes

`write_file` (Section 3.4) also publishes atomically, by the same principle at
file granularity: write to a temp sibling, fsync, then atomically rename over the
target (fs / remote-fs) or PUT the new object then update the pointer (object).
This keeps a concurrent `read_file` from ever seeing a torn file. Capturing the
prior version into `versions/` (Section 5) happens **before** the overwrite, so a
crash mid-write leaves both the old `current/` content and the retained version
consistent.

A single-file `write_file` and a whole-working-set publish/restore are never
issued concurrently for the same server: they are serialized at the application
layer per the Section 6.9 state-branching policy and decision 8.2 (Storage file
edits happen only on a stopped server, while publish happens for a running
server's snapshot or during restore, which requires a stop). The Storage adapter
itself does not arbitrate concurrent publish and `write_file` on the same server;
the application layer is responsible for not issuing them concurrently.

---

## 5. File version retention

**Requirement (FR-FILE-3).** Each file edit is versioned; any retained version
can be restored.

**Decision — copy-on-write per file, count-bounded retention.** On every
`write_file` (and `rollback_file`), the **previous** content of that file is
copied into `versions/<rel-path>/<version-id>` before the new content is
published. `version-id` is a monotonic, opaque id; the DB (#15) indexes
`(server, rel_path, version-id, author, timestamp)`.

Retention is bounded by a **configurable per-file version count** (default and
key owned by #16); when the count is exceeded the oldest version of that file is
pruned. Versioning applies to the **authoritative copy only** — edits to a
running server's live working set go over the control plane (ARCHITECTURE.md
Section 7.2) and are captured as versions when that working set is next
snapshotted and the resulting file differs, not on each keystroke.

**Alternatives considered.**
1. *Whole-working-set versioning* (snapshot the entire `current/` per edit) —
   simple to reason about but stores enormous redundant data per single-file
   edit; rejected at this scale.
2. *Delta/diff chains per file* — storage-efficient but adds a reconstruction
   step and a corruption-propagation risk across the chain; rejected as
   premature optimization for small files like configs (NFR-SCALE-1).
3. *Time-windowed retention* instead of count-bounded — harder to reason about
   storage bounds; count is simpler and predictable. (A future milestone can add
   a time policy behind the same Port.)

**Rationale.** Full-content copies of individual files are tiny (configs,
properties, scripts), so copy-on-write is the simplest correct scheme and makes
rollback a plain read+write with no reconstruction. Backups (Section 3.3) already
cover whole-working-set recovery, so file versioning only needs to cover the
fine-grained file-edit case it exists for.

---

## 6. Path-traversal protection

**Requirement (FR-FILE-4).** Path-traversal protection is enforced inside the
`Storage` adapter (and, separately, on the Worker side for live working sets —
ARCHITECTURE.md Section 7.2, the Worker's `WorkingDir` Port).

Every operation that accepts a caller-supplied `rel_path` (Sections 3.4, 3.5)
enforces, inside the adapter, before any I/O:

- The path is treated as **relative to the server's `current/` root**; absolute
  paths are rejected.
- The path is **canonicalized** and must resolve to a location **inside** the
  server's namespace root. Any result escaping the root (via `..`, symlinks, or
  encoding tricks) is rejected with a domain error, not silently clamped.
- **Symlinks** within `current/` are not followed out of the root; a symlink that
  points outside the server root is rejected.
- The `(community_id, server_id)` scope is applied by the adapter from
  trusted, API-minted ids (Section 2), never from the `rel_path`, so cross-server
  or cross-Community access is structurally impossible.

This is enforced in the adapter (not the use case) so that **every** backend gets
the protection and a future backend cannot forget it; the rejection is a typed
domain error so the API surface can map it to a uniform response.

---

## 7. Adapter families

All three families implement the **same `Storage` Port** (Section 3) and provide
the **same observable atomic-publish guarantee** (Section 4); they differ only in
how they realize it. Selection is by configuration (#16); no caller changes when
the backend changes (FR-DATA-2). Adapters are named `<Tech><Port>` per
ARCHITECTURE.md Section 6 (e.g. `FsStorage`, `ObjectStorage`).

### 7.1 fs (local filesystem) — M1 default

| Aspect | Guarantee / mechanism |
|---|---|
| Backing | A directory tree on the API host's local disk, rooted at `<root>` (Section 2). |
| Atomic publish | `current` symlink flip via same-directory `rename(2)` (Section 4.2). |
| Single-file write | temp-write + fsync + atomic rename (Section 4.4). |
| Path-traversal | canonicalize + root-containment check (Section 6). |
| Best for | The legacy single-host posture and the simplest M1 deployment. |
| Caveat | `<root>` must be a single filesystem so the snapshot move and the symlink rename stay atomic; staging, snapshots, and the symlink must not straddle a mount boundary (a cross-device rename is not atomic). |

### 7.2 remote-fs (network/shared filesystem)

| Aspect | Guarantee / mechanism |
|---|---|
| Backing | A POSIX-like remote/shared mount (NFS, SMB, a CSI volume) presented as a normal path. |
| Atomic publish | Same `current` symlink-flip mechanism as fs, **provided the mount honors symlinks, atomic same-directory rename, and close-to-open consistency.** |
| Single-file write | temp-write + fsync + atomic rename, same as fs. |
| Path-traversal | Identical to fs (the adapter logic is shared). |
| Best for | Letting the authoritative store outlive a single API host / sit on shared infrastructure without moving to object storage. |
| Caveat | Atomicity and durability depend on the mount's semantics; the adapter documents the required guarantees (symlink support, atomic same-dir rename, fsync durability) and the operator must provision a mount that meets them. SMB mounts frequently lack POSIX symlink support, so the remote-fs adapter requires a backing filesystem/mount with POSIX symlink and atomic same-directory rename semantics (NFS qualifies; SMB often does not). Cross-device rename caveat (Section 7.1) applies identically. |

`remote-fs` may share most code with `fs` (both are path-based); they are
distinct entries because their **operational guarantees and failure modes
differ**, and #16 selects between them explicitly.

### 7.3 object (object storage)

| Aspect | Guarantee / mechanism |
|---|---|
| Backing | An S3-compatible object store; the Section 2 tree is a **key-prefix scheme**, not directories. |
| Atomic publish | **Pointer-object flip** (Section 4.2): write data objects under a fresh prefix, then atomically overwrite a single pointer/manifest object to reference it. Relies on the store's read-after-write and atomic single-object PUT. |
| Single-file write | PUT the new object, then update the pointer; the pointer flip is the atomic point. |
| Path-traversal | The same canonicalization is applied to derive the **key** from `rel_path`; a `rel_path` that would escape the server's key prefix is rejected (Section 6). No symlinks exist, removing that vector. |
| Garbage collection | Orphaned prefixes (from aborted/crashed publishes, or old prefixes after a flip) are reclaimed by a sweep keyed off the live pointer (Section 4.3). |
| Best for | Decoupling the authoritative store from any host filesystem; durability/scale beyond local disk. |
| Caveat | No atomic multi-object rename and no real directories — hence the pointer-flip design. List operations are prefix scans. |

### 7.4 Why backend switching stays a configuration change

The contract is what callers depend on: opaque keys/handles, scope-by-id, the
atomic-publish guarantee, and the traversal-safety guarantee — none of which name
a backend. The differences in Sections 7.1–7.3 are *inside* the adapter. Because
business logic depends only on the Port (ARCHITECTURE.md Section 5.1) and the
adapter is bound at the edge wiring (ARCHITECTURE.md Section 2.1), changing the
configured backend re-binds a different adapter with **no change to any use
case** (FR-DATA-2). The publish mechanism differs, but the guarantee it delivers
does not.

---

## 8. Data plane (HTTP transfer)

The control plane only *triggers* a working-set transfer; the bulk bytes ride a
separate **API-terminated HTTP data plane** so a multi-GB hydrate/snapshot never
blocks control traffic (REQUIREMENTS.md Section 5.2, ARCHITECTURE.md Section 4,
CONTROL_PLANE.md Section 5). These endpoints are the wire transport the Port's
Section 3.1 contract deliberately leaves out; Storage guarantees the
authoritative-side semantics, and this section is the matching transport.

**Auth.** Worker-only, never community-authenticated: a Worker is platform
infrastructure, not a member. The shared control-plane Worker credential is
presented as `Authorization: Bearer <credential>` and compared constant-time
(`control.worker_credential`, NFR-SEC-1) — the same model as the control-plane
gRPC stream. A missing/wrong credential is `401`. The trigger commands carry the
credential as their `transfer_token` and the full endpoint URL as
`transfer_url`, so the Worker treats both opaquely and never needs to know the
`(community, server)` scope itself.

**Archive format.** A stdlib **tar stream** of the working-set root (the same
format `open_hydrate_source` / `write_snapshot` already produce/consume,
Section 7.1). No compression at M1.

**Endpoints** (scoped by `(community_id, server_id)`):

| Method & path | Meaning | Success | Errors |
|---|---|---|---|
| `GET /data-plane/communities/{c}/servers/{s}/working-set` | Hydrate: stream the authoritative working set as a tar (with the resolved `server.jar` injected when present, #118). | `200` tar body | `204` no published snapshot *and* no resolved JAR (Worker starts from an empty dir); `401` |
| `POST /data-plane/communities/{c}/servers/{s}/snapshot` | Snapshot: stream a tar into staging and atomically publish it. | `204` | `400` length mismatch / incomplete; `400` `empty_snapshot` (staged an empty working set); `411` no `Content-Length`; `413` over the size cap; `401` |

**JAR posture (M1).** ARCHITECTURE.md Section 7.3 says the resolved server JAR
reaches the Worker as part of hydrate. As of issue #118 (version catalog + JAR
resolution) this is realised: `StartServer` ensures the resolved JAR is in the
content-addressed pool before placement and records its content key on the
`server` record (in the `config` JSONB blob, key `resolved_jar_sha256` — DATABASE.md
Section 7 has no dedicated JAR column), and the hydrate endpoint **injects** that
JAR into the working-set tar at the conventional `server.jar` relpath. The JAR is
still *omitted when not present* (no resolved JAR recorded, or the recorded JAR not
in the pool), so a working set with no resolved JAR is sent alone — the contract
above is unchanged. The injection prepends a single tar member to the working
set's members; when there is a resolved JAR but no published snapshot, the body is
a tar carrying just `server.jar` (a `200`, not the `204` of the nothing-to-send
case) so the Worker can still launch.

forge and spigot are **not** resolved: the version catalog lists/resolves only
vanilla (Mojang manifest), Paper (PaperMC API), and fabric (meta.fabricmc.net).
The `server_type` CHECK enum still permits `forge` and `spigot`, but server
create-validation rejects both — the catalog has no usable source (forge needs a
worker-side installer step; spigot has no official distribution API and create
recommends Paper instead). The fabric server launcher JAR has no upstream
checksum, so it is pooled content-addressed by its own SHA-256 without
source-digest verification.

**Proven-complete gate (FR-DATA-6).** `commit_snapshot` only publishes a staged
transfer the data plane signalled complete (Section 4.1). The signal is the HTTP
`Content-Length`: the snapshot endpoint refuses a body with no length, and after
streaming the body into staging it verifies the streamed byte count equals
`Content-Length`. On any mismatch (or a mid-transfer failure / client
disconnect) it `abort`s the staging area and the prior authoritative copy is
untouched — a partial upload is **never** published. The Worker sets the length
from the tar it buffered, so the count matches a complete upload exactly.

**Worker side.** The Worker's `DataTransfer` client (Go) does the byte movement
off the gRPC stream: hydrate = GET + stream-unpack into the instance working dir
(members path-sanitized — absolute paths, `..`, and symlink/hardlink members are
rejected, mirroring the API-side `filter="data"` discipline); snapshot = pack the
working dir into a tar and POST it with a `Content-Length`. Transport security
(CA bundle / mTLS / dev-insecure) mirrors the control channel
(CONFIGURATION.md Section 6.1). The session routes every server-scoped command —
hydrate/snapshot as well as lifecycle/file commands — to a per-server lane off
the receive loop: one server's commands run serially (start/stop never
interleave), but distinct servers' lanes run concurrently under a bounded
concurrency cap, so a slow transfer or graceful stop never delays another
server's command (issue #95).

**Lifecycle wiring (FR-DATA-4).** `StartServer` hydrates before the launch (the
API issues a hydrate trigger, then `StartServer`); a graceful `StopServer` takes
a final snapshot after the process exits and before reporting stopped (the API
issues the stop, then a snapshot trigger; the snapshot is best-effort — its
failure does not fail the stop). A `HydrateTrigger` is only valid for a stopped
server (refreshing a running server's working set would corrupt live state).

---

## 9. Design decisions

Each records the decision, alternatives, and rationale, compactly
(NFR-SCALE-1: thorough, not bloated). Decisions already stated inline — the
copy-on-write version scheme (Section 5) and the per-family publish mechanism
(Section 4.2) — are not repeated here.

### 8.1 Per-Community → per-server namespacing; JARs shared

**Decision.** Authoritative data is namespaced `communities/<id>/servers/<id>/`;
JARs live in a single shared, content-addressed `jars/` pool (Section 2).

**Alternatives.** (1) Flat per-server with the Community recorded only in the DB.
(2) Per-Community JAR pools.

**Rationale.** Community is the isolation unit (Section 6.2); making it the
top-level namespace makes isolation visible in the layout, scopes
path-traversal containment to a server root, and makes per-Community operations
(e.g. provisioning teardown) a single-prefix operation. JARs carry no Community
data and are reused across servers (FR-VER-3), so a shared content-addressed pool
deduplicates by construction; per-Community pools would store the same vanilla
JAR many times.

### 8.2 Atomicity enforced by `Storage`; state preconditions by the application

**Decision.** `Storage` guarantees atomic publish and traversal safety
unconditionally. Server-state preconditions (e.g. "restore requires a stopped
server", FR-BAK-4) are enforced by the **application layer**, not by `Storage`.

**Alternatives.** Make `Storage` state-aware (pass server state, refuse unsafe
ops).

**Rationale.** `Storage` has no notion of server runtime state (that lives on the
Worker / in the API records); injecting it would couple the Port to lifecycle
concerns and the control plane. The Section 6.9 policy is a business rule and
belongs in the use case. `Storage` stays a pure store: always-safe primitives,
no policy.

### 8.3 The Port hides the data-plane transport

**Decision.** `Storage` exposes streams and a publish handshake (Section 3.1);
the API↔Worker transfer wire format is the data plane (epic #8), not part of this
Port.

**Alternatives.** Fold the transfer protocol into `Storage`.

**Rationale.** Keeps `Storage` about the authoritative store and lets the data
plane evolve (chunking, resumable transfer, future delta sync — FR-DATA-5)
without changing the store contract. The two meet at the stream boundary only.

### 8.4 Backups are whole-working-set archives, not Storage-internal links

**Decision.** A backup is a self-contained archive (Section 3.3), independent of
any Worker and of the current `current/`.

**Alternatives.** Backups as references/snapshots-in-place sharing data with
`current/` (copy-on-write clones).

**Rationale.** FR-BAK-2 requires a backup to be a retained snapshot that does not
depend on a specific Worker; self-containment also means a backup survives
deletion of the live working set and restores cleanly via the same atomic-publish
path. In-place clones would couple a backup's integrity to the live copy's
lifecycle and are backend-specific (not all backends offer cheap clones).

### 8.5 Out of scope / deferred (carried as follow-ups, not implemented here)

- **JAR garbage collection** (Section 3.2): M1 never deletes JARs; a reference-
  counted GC is deferred.
- **Orphan-sweep scheduling** (Section 4.3): the recovery sweep is specified;
  *when* it runs (startup, periodic) is an operational detail for the
  implementation epic (#8).
- **Continuous delta sync** (FR-DATA-5) is explicitly deferred; the streaming
  Port shape leaves room for it.

---

## 10. Related documents

| Doc | Relationship |
|---|---|
| [`../REQUIREMENTS.md`](../REQUIREMENTS.md) | Source of truth: Sections 6.8–6.12, FR-DATA-1…7, FR-FILE-3, FR-FILE-4, FR-BAK-*, FR-VER-3. |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | `Storage` Port placement (Section 5.1), layering and edge wiring (Section 2), naming (Section 6), the running-server file-access decision (Section 7.2) that bounds this Port's file ops to the stopped-server case. |
| [`DATABASE.md`](DATABASE.md) | The metadata indexing the blobs this document lays out (`Server`, `Backup`, `FileEditHistory`). Blob layout is here; metadata tables are there. |
| [`CONFIGURATION.md`](CONFIGURATION.md) | The runtime keys that select the backend family and tune snapshot interval / version-retention count. This document defines *what* is selectable; `CONFIGURATION.md` names the keys. |
