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
- **Metadata** (the `Server`, `Backup` rows that index those blobs, schedules,
  audit) lives in the database and is owned by `DATABASE.md` (#15). File
  versions are the exception: they carry no metadata rows and live wholly
  behind the Port (Section 5). `Storage` returns opaque keys/handles; the metadata
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
│               ├── generation                # combined marker (Section 3.1): line 1 the monotonic working-set generation counter (bumped on each authoritative publish: commit_snapshot + restore_backup #873 + authoritative file edits #889), line 2 (optional) the publishing Worker id (#847)
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
│               ├── backups/
│               │   ├── <backup-id>.<archive-ext>  # a retained, self-contained snapshot archive
│               │   └── ...
│               └── final.tar.gz                # post-delete retained working set (Section 2.1); no DB row
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
- `generation` is a plain-text marker holding **two** values: line 1 is the
  current authoritative working-set generation counter (a monotonically
  increasing integer, starting at 1 after the first publish); line 2 (optional)
  is the **publishing Worker id** — the id of the Worker that produced `current`
  (issue #847), used by the publish-time generation guard (Section 8) to tell a
  same-Worker re-publish (lost-response self-heal) from a different-Worker stale
  publish (A→B→A). Both are written as **one atomic marker** (a single
  temp-sibling + atomic rename on `fs`, a single object `PUT` on object backends):
  a crash between two separate writes could attribute the *previous* publisher to
  the *new* generation and invert the guard, so the pair must be all-or-nothing.
  The marker is written immediately after the `current` pointer flip inside a
  **pointer-flip publish** — `commit_snapshot` and `restore_backup` (#873) — as a
  separate sequential write; a crash in the window between the flip and the marker
  write leaves the generation one behind (under-states by one — the safe direction:
  `current` already names the new world the producing Worker also holds in scratch,
  so a hydrate-skip cannot serve a *stale* world, and the failed call's retry
  republishes and bumps the marker into agreement). An **in-place authoritative
  file edit** (`write_file` / `delete_file` / `delete_dir` / `make_dir` /
  `rollback_file`, #889) instead mutates `current/` itself rather than flipping a
  fresh pointer, so its mutate→bump crash window is **not** the safe direction: a
  crash after the mutation but before the bump leaves the edited world live at the
  OLD generation. Multi-member edits (`delete_dir` on fs uses `rmtree` —
  per-member unlinks; on object backends `delete_dir` / `rename_dir` /
  `rename_file` are per-object loops) additionally have a **mid-mutation window**:
  a crash partway through the loop leaves `current/` partially mutated at a stale
  generation (see Section 4.4 accepted gap, issue #1608). A same-Worker scratch with `held == store` would then skip the
  post-edit hydrate (#767) and boot the PRE-edit world — re-opening #889's staleness
  for that edit until the next bump. To keep that window crash-recoverable rather
  than racy against a concurrent publish, the edit's mutate+bump — and a
  `commit_snapshot`'s stale re-check + flip + bump (#899) — run under a per-server
  lock so a concurrent publish/edit cannot interleave between the two steps. An edit
  resolves its read-set (the live pointer / `current` symlink) **inside** that lock
  (#920): resolving it outside would let it write back a snapshot the commit has
  already flipped away and reclaimed, losing the world. The lock covers only the
  re-check + pointer flip + bump (and the gates that must be atomic with the flip);
  a publish's bulk staging→snapshot copy runs **before** the lock and the superseded
  snapshot's reclaim runs **after** it, so a multi-minute copy never blocks edits.
  The post-lock reclaim is safe because an edit can no longer observe the pre-flip
  snapshot once it re-reads the pointer under the lock. The lock is **in-process
  only** (one uvicorn process today); a multi-process deployment would need a shared
  lock. After any successful publish/edit return the pointer, generation, and
  publisher are in agreement. A publish that declared no
  Worker id omits line 2 (no publisher claim, so the guard stays permissive). A
  server with no published snapshot has no `generation` marker; reading it in that
  state returns generation 0 / no publisher. On object backends it is a single key
  `generation` under the server prefix. Removed together with the rest of the
  working-set tree by the post-delete prune (Section 2.1).
- On object backends the tree above is a **key prefix scheme**, not real
  directories (Section 7.3); the same logical layout applies.

### 2.1 Post-delete retention (issue #777)

Deleting a server cascades its DB rows (the server row, its backup rows) but does
**not** wipe its Storage. To bound the disk cost while never destroying the latest
state, `DeleteServer` retains **exactly two** artifacts under the server directory,
**neither of which has a DB row** (they are operator-level artifacts):

1. **The latest backup archive, if one exists** — `backups/<newest-id>.tar.gz`. The
   newest backup by `created_at` is kept; every older archive is deleted,
   archive-first per the `delete_backup` ordering convention (Section 3.3). (The
   per-file `versions/` tree is not pruned here — it is removed by the working-set
   prune in point 2, before any archive delete.) Selection is the literal
   latest-by-`created_at` **regardless of `health`** (owner ruling on #777: "latest existing"): a
   QUARANTINED newest archive is still the one kept. The mandatory `final.tar.gz`
   below is the safety net — it is strictly newer and is the recommended recovery
   source if the retained backup is suspect.
2. **The current working set, packed as `final.tar.gz`** — mandatory. The live
   `current/` snapshot is packed into a single self-contained `tar.gz` at the server
   root, then the unpacked working-set tree (`snapshots/`, `incoming/`, `versions/`,
   the `current` pointer, and the generation marker) is removed. Packing is
   **fail-closed**: if it fails, the working set is left intact and the delete fails,
   so a deletion never silently loses the latest state. A server that never published
   a snapshot has no `final.tar.gz`. The pack deliberately **bypasses the #764 `.mca`
   integrity gate** (the gate applied to `create_backup_from_current`): a corrupt
   server must still be deletable, so torn regions are packed as-is.

Everything else for the server is removed; after the prune the server directory
holds only `backups/<newest-id>.tar.gz` (if any) and `final.tar.gz` (if published) —
no `snapshots/`, `incoming/`, `versions/`, `current` pointer, or generation marker
remain. The retained archives are unreferenced orphans by design (not the bug the
`delete_backup` ordering guards against).

**Crash-retry safety.** A `DeleteServer` retry is the advertised recovery path, so
the working-set prune is ordered to be idempotent: the pointer (object adapter) /
`current` symlink (fs adapter) — the one marker that says the working set is still
live and re-packable — is invalidated the instant `final.tar.gz` is durable, before
any other GC. A retry that finds the pointer already gone treats the prune as done
and finishes the GC without re-packing, so it can never overwrite a good
`final.tar.gz` with an empty/partial pack built from a half-deleted source.

**Operator recovery / disk reclaim.** Both retained artifacts are plain `tar.gz`
archives in the server's prior directory. To recover the data, an operator creates
a fresh server and **uploads** the chosen archive as a backup (the upload-backup
flow, issue #281), then **restores** it — `restore_backup` republishes the `tar.gz`
into the new server's `current/`. To reclaim the disk instead, delete the server
directory tree.

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
| `commit_snapshot(handle, *, publisher=None, expected_base=None) -> int` | Atomically publish the staged snapshot as the new authoritative copy; return the new generation | Atomic publish (Section 4). After return, `current/` reflects the complete transfer or the prior copy — never a partial. Bumps and returns the working-set generation counter (the new integer the Worker records as the generation its scratch is at) and records `publisher` (the producing Worker's id, issue #847) alongside it in one atomic marker, read back via `current_publisher`. A `None` publisher records no id (the guard stays permissive). `expected_base` is the commit-time stale re-check (issue #899): the generation the data-plane publish guard validated against before the upload stream (what `current` was at guard time). The commit re-reads the generation under the per-server publish/edit lock (Section 2 generation marker) and refuses with `StaleGenerationError` when it advanced past `expected_base` — an at-rest edit or restore that landed DURING the (multi-minute) upload window — so the just-bumped `current` is not clobbered; `None` skips the re-check (no base claim). Refuses with `IncompleteTransferError` if the transfer was not signalled complete. Refuses with `IntegrityCheckError` if the staged set contains corrupt `.mca` region files (issue #739), using the single region rule set (issue #927): a non-4096-aligned tail is the normal on-disk shape of a 26.x world, not a tear, on any source — the byte-precise check still catches realistic tears. Refuses with `MissingRegionsError` if the staged set dropped some-but-not-all `.mca` files of a still-live dimension (the partial-loss corruption signature, issue #854) — a full-dimension delete (ALL regions of a dir gone) is allowed, only a partial loss is refused; the error carries the per-directory lost names (recovery in Section 4.5). Refused publishes do NOT bump the generation; the staging is discarded. |
| `abort_snapshot(handle)` | Discard an incomplete/failed transfer | Deletes the staging area; `current/` is untouched. Also the cleanup path for crash recovery (Section 4.3). |
| `current_generation(community_id, server_id) -> int` | Return the current authoritative working-set generation | The counter `commit_snapshot` bumps, read back so the hydrate data plane can stamp the generation it serves (Section 8). Returns 0 when no snapshot has been published. |
| `current_publisher(community_id, server_id) -> str \| None` | Return the Worker id that published `current` | Read back from the combined `generation` marker (issue #847) so the publish-time generation guard (Section 8) can allow a same-Worker re-publish (lost-response self-heal) while refusing a different-Worker stale publish (A→B→A). `None` when no snapshot has been published, or the last publish declared no id (an older Worker) — in which case the guard cannot prove a foreign publisher and stays permissive. |
| `check_current_health(community_id, server_id) -> WorkingSetReport` | Structurally fsck the on-disk authoritative snapshot | Walk `current/` for corrupt `.mca` region files (issue #744). Read-only — never mutates `current/`. Raises `NotFoundError` if no snapshot has been published. |
| `prune_to_final_snapshot(community_id, server_id)` | Collapse the working set to one retained `final.tar.gz` and drop the tree | The `DeleteServer` reclaim path (Section 2.1, issue #777). Packs `current/` then removes `snapshots/`, `incoming/`, `versions/`, the `current` pointer, and the combined `generation`+publisher marker; leaves `backups/`. The pointer/symlink is invalidated the instant `final.tar.gz` is durable, so a crash-retry is idempotent and never re-packs over a good final. Fail-closed on a pack failure; bypasses the #764 `.mca` gate so a corrupt server stays deletable; no-op if nothing is published. |

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
| `jar_pool_stats() -> JarPoolStats` | Count + total bytes of the pooled JARs | A bounded scan of the one `jars/` namespace; platform-admin operational visibility (#286). |
| `list_jars() -> [JarPoolEntry]` | Enumerate pooled JARs with key, size, and store time | The input the reference-counted GC diffs against the live reference set; each entry's `modified_at` feeds the GC safety window (#293). |
| `delete_jar(JarKey)` | Remove a pooled JAR | Idempotent (no error if absent). The reclaim primitive the GC calls on an unreferenced JAR; the reference decision is the GC's, not Storage's. |

**Reference-counted GC (D4, #293).** The pool is reclaimed by a reference-counted
garbage collector: a periodic API-side loop (config `jar_gc.interval_seconds`,
default daily, gated on the control plane with the other schedulers) plus a
platform-admin `POST /versions/jar-pool/gc` manual trigger (audited; returns
`{scanned, deleted, freed_bytes}`).

*Reference model.* A pooled JAR is **live** iff some server row's config records it
as its resolved JAR (`resolved_jar_sha256`, Section 8) — the authoritative
reference set is that bounded DB scan. Snapshots and backups do **not** pin pool
JARs: the hydrate endpoint *injects* the resolved JAR into the working-set tar at
transfer time (Section 8), so the Worker's working dir carries `server.jar` and a
snapshot/backup of it **embeds the JAR inside its own tar** (the Worker excludes
nothing when packing at M1). A restore therefore needs no pool copy, and deleting
a server (which removes its rows, working set, and backups together) drops the
only reference pinning its JAR. Everything in the pool referenced by no row is an
orphan to reclaim.

*Safety window.* `StartServer` ensures (downloads + stores) the resolved JAR into
the pool **before** it commits the server row carrying that JAR's key
(ensure-then-commit ordering). So a freshly-pooled JAR is briefly present but
unreferenced — exactly an orphan to the GC — while an in-flight start finishes
committing. The GC therefore never deletes a JAR younger than a fixed safety
window (one hour, comfortably beyond a normal start's put-to-commit gap), keyed
off the JAR's store/upload time.

### 3.3 Backup archive create / list / restore / delete / transfer

Serve backup management (FR-BAK-1, FR-BAK-2, FR-BAK-4) and off-host transfer
(issue #281). A backup is a self-contained archive of a working set; it does not
depend on a Worker.

| Operation | Purpose | Notes |
|---|---|---|
| `create_backup_from_current(community_id, server_id) -> BackupKey` | Archive the authoritative `current/` into `backups/` | The **stopped-server** path (Section 6.9). For a **running** server the Worker quiesces the live world (save-off → save-all → settle-wait) and commits an on-demand snapshot (`commit_snapshot`) → then the application calls this; `Storage` only ever archives the authoritative copy. |
| `list_backups(community_id, server_id) -> [BackupKey]` | Enumerate a server's backups | Metadata (label, timestamp, size) lives in the DB (#15); this returns the keys. |
| `restore_backup(community_id, server_id, BackupKey, force=False)` | Atomically republish a backup into `current/` | Atomic publish (Section 4). Caller must ensure the server is **stopped** (FR-BAK-4); `Storage` enforces atomicity, the application enforces the stop precondition. A restore replaces `current/`, so it **bumps the working-set generation** like a `commit_snapshot` (issue #873) — otherwise a same-Worker scratch with `held == store` would skip the post-restore hydrate (#767) on the next start and boot the PRE-restore world. The publisher is stamped with the `api-restore` sentinel (no producing Worker), so the publish-time guard (Section 8) treats an in-flight stale snapshot from a real Worker as a different-publisher publish and refuses it, closing the restore-clobber window. The extracted backup is validated through the integrity gate (issue #743): a corrupt backup is refused with `IntegrityCheckError` (carrying the `WorkingSetReport`); `current/` is left untouched. Quarantining the backup is an application-layer concern — the caller receives the report and decides what to do. `force=True` (the `?force=true` API override, operator-only) publishes despite corruption and returns the `WorkingSetReport` so the caller can quarantine and audit. A restore **bypasses the missing-region gate (#854) by design**: that gate diffs a staged set against the prior `current/` to catch a Worker accidentally dropping live regions, but a backup is a **complete, self-consistent set captured as a whole** — it is the authoritative replacement, not an incremental delta over `current/`, so a backup that legitimately holds fewer regions than the current world (an older/smaller world being restored) is exactly the intended operation and must not be refused. The structural `.mca` integrity gate (#743) still applies (a backup cannot be *internally* corrupt); only the prior-set partial-loss comparison is skipped. **Restore flip↔marker crash window:** like `commit_snapshot`, the generation/publisher marker is a separate write after the `current` flip (Section 2 generation marker), so a crash in that window leaves `current` = the restored world but the marker stale (old generation/publisher); the restore call itself then fails so the caller knows it did not complete, a retry republishes and bumps the marker into agreement (self-healing), and the #827 restore lock serializes the window so no concurrent publish can interleave. |
| `delete_backup(community_id, server_id, BackupKey)` | Remove a backup archive | Idempotent. |
| `open_backup(community_id, server_id, BackupKey) -> ReadStream` | Stream a stored archive in its native format | Download (issue #281): yields the archive bytes **verbatim** — the adapter-internal `tar.gz` (Section 2), no recompression. `NotFoundError` for an unknown key. |
| `put_backup(community_id, server_id, WriteStream) -> BackupKey` | Store an uploaded archive verbatim under a fresh key | Upload (issue #281): the **application** has already validated the archive (opens + traversal-safe entries) before this is called; `Storage` only stores the bytes, so the new backup is restorable through `restore_backup` like a created one. |
| `backup_size(community_id, server_id, BackupKey) -> int` | Report a stored archive's byte count | The size recorded as `size_bytes` at create/upload (issue #281). `NotFoundError` for an unknown key. |

**Backup size impact of plugin/mod JARs (issue #1164).** Backups archive the
entire authoritative `current/` working set, which includes all plugin and mod
JARs deployed into the server's content directory (`mods/`, `plugins/`). A
server with 500 MiB of mods will produce backups of at least that size (plus
the world data), effectively doubling the per-server storage footprint when a
backup is retained. Operators should account for this when sizing storage and
setting backup retention policies for mod-heavy servers.

### 3.4 File read / edit on the authoritative copy

Serve file management for **stopped** servers (FR-FILE-1, FR-FILE-2). For a
running server, file ops route over the control plane to the Worker's live
working set and do **not** touch `Storage` (ARCHITECTURE.md Section 7.2); those
paths are not part of this Port.

| Operation | Purpose | Notes |
|---|---|---|
| `read_file(community_id, server_id, rel_path) -> bytes` | Read one file from `current/` (whole bytes) | `rel_path` is validated against traversal (Section 6). Whole-bytes by design: the small-edit / preview read, and the base64-payload `GET ?path=` route where the bytes *are* the JSON body. A large single-file **download** uses `open_file_stream` (issue #265). |
| `open_file_stream(community_id, server_id, rel_path) -> ReadStream` | Open a chunked read stream over one file in `current/` | The per-file analogue of `open_hydrate_source` (issue #265): a large single-file download streams without buffering the whole file in RAM. Same lease contract — the live snapshot is resolved and the active-reader lease taken on the FIRST iteration, released on finish/early-close/error (Section 4.2). `NotFoundError` for a missing file or unpublished snapshot. |
| `list_dir(community_id, server_id, rel_path) -> [entry]` | Browse a directory in `current/` | Same path validation. |
| `write_file(community_id, server_id, rel_path, bytes)` | Edit one file in `current/`, retaining the prior version | Captures the previous content into `versions/` (Section 5) before overwriting. The per-file write is atomic (Section 4.4). Bumps the generation + stamps the `api-edit` sentinel (#889). |
| `delete_file(community_id, server_id, rel_path)` | Delete one file from `current/`, retaining the prior content | Captures the content into `versions/` (Section 5) **before** removing, so a delete is reversible by rollback exactly like an edit. Missing path → `NotFoundError`. Bumps the generation + stamps the `api-edit` sentinel (#889). |
| `delete_dir(community_id, server_id, rel_path)` | Recursively delete a directory subtree from `current/` | **No** per-file version capture: file versioning (Section 5) is the fine-grained single-file mechanism, whereas whole-subtree recovery is what backups (Section 3.3) exist for; capturing a version per member of a large subtree would be a storage-amplification bomb. Missing dir → `NotFoundError`. Bumps the generation + stamps the `api-edit` sentinel (#889). **Not crash-atomic across the subtree:** fs uses `rmtree` (per-member unlinks); object uses a per-object delete loop. A crash mid-op leaves a partially deleted subtree at a stale generation (Section 4.4 accepted gap, #1608). |
| `rename_file(community_id, server_id, from_path, to_path)` | Rename/move a single file within `current/` | **No** version capture on either side (issue #1164): a rename does not change the content, so retaining versions would waste storage; the caller's content-addressed cache (plugin JARs) or backups cover recovery. Missing source → `NotFoundError`. Bumps the generation + stamps the `api-edit` sentinel (#889). Atomic on fs (`rename(2)`); on object backends it is a copy+delete pair — not crash-atomic (#1608). |
| `rename_dir(community_id, server_id, from_path, to_path)` | Rename/move a directory within `current/` | **No** per-file version capture (same reasoning as `delete_dir`, issue #1191). Missing source dir → `NotFoundError`. Bumps the generation + stamps the `api-edit` sentinel (#889). Atomic on fs (`rename(2)`); on object backends it is a per-object copy+delete loop — not crash-atomic (#1608). |
| `make_dir(community_id, server_id, rel_path)` | Create an (empty) directory in `current/` | Backend-dependent (see note). Idempotent. **Requires a published snapshot** — a never-snapshotted server has no live `current/` to create the directory under, so `make_dir` raises `NotFoundError` (behaviour aligned across both adapters in #896, including object which previously bumped the generation with no snapshot). Bumps the generation + stamps the `api-edit` sentinel (#889), uniformly with the other edits. On object backends it also writes a zero-byte `.dir` marker object under the prefix so the otherwise-empty directory is visible in listings (#1125; see note). |

**Empty-directory representation (`make_dir`).** fs / remote-fs materialize a real
empty directory, which rides the hydrate tar as a directory member (the tar is
built recursively, so empty dirs survive a snapshot round-trip). **Object storage
has no real directories** — a directory exists only as the shared key-prefix of
its files (Section 7.3), so an *empty* directory has no key to make it visible. To
give it one, `make_dir` writes a zero-byte `.dir` marker object under the directory
prefix (#1125), so the otherwise-empty directory shows up in listings. `list_dir`
filters the `.dir` marker out of its entries (`_entries_at_level`), so the API
never surfaces it as a file. The marker is a real object, though: it rides the
hydrate tar to the Worker (a literal `foo/.dir` file appears in the live working
directory), is re-packed into the next snapshot, and is carried into
`create_backup_from_current`/restore — so on object backends the empty directory
persists as a `.dir` marker throughout the working-set lifecycle rather than as a
true directory entry. On both backends `make_dir` bumps the generation marker
(#889, see Section 4.4), so the store generation stays in lockstep across adapters.

### 3.5 File version retention / rollback

Serve versioned edits (FR-FILE-3). See Section 5 for the retention scheme.

| Operation | Purpose | Notes |
|---|---|---|
| `list_file_versions(community_id, server_id, rel_path) -> [VersionId]` | List retained prior versions of a file | Ordered newest-first. Versions are storage-only — the ids are enumerated from the backend, not from a DB index (Section 5). |
| `read_file_version(community_id, server_id, rel_path, VersionId) -> bytes` | Read a specific retained version | For preview/diff before rollback. |
| `rollback_file(community_id, server_id, rel_path, VersionId)` | Restore a file to a retained version | Implemented as a `write_file` of the old content, so the pre-rollback content is itself retained (rollback is reversible) and the generation bump + `api-edit` sentinel ride that delegated write (#889). |

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

For the fs / remote-fs backends, the staged tree's file data is fsynced
(`_fsync_tree`) before the pointer flip, and the `snapshots/` directory is
fsynced after the staging-to-snapshots move so the rename entry is durable.
After the symlink rename the parent directory (`server_root`) is fsynced so the
flip itself survives power loss (symmetric with the Section 4.4 single-file
fsync note); a flip that is lost despite being complete is otherwise bounded by
the FR-DATA-5 RPO.

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

For the fs / remote-fs backends, the staged tree's file data is fsynced
(`_fsync_tree`) before the flip and the superseded snapshot is reclaimed only
after the flip, so `current` never resolves to a tree whose data blocks are
unflushed while the prior copy is already deleted (#1943).

The sweep also reclaims the **spool temp files** a crash leaves at a backup
write site — the fs adapter's `.backup.*.tmp` (create / upload) and
`.final.*.tmp` (prune) siblings — and, on the object backend, the **in-progress
multipart upload parts** a crash mid-`put_backup` (or mid-snapshot-member upload)
leaves behind. Multipart parts never list as objects, so the prefix scan cannot
see them; the object sweep lists them via `ListMultipartUploads` and aborts them
via `AbortMultipartUpload`. Both reclaim paths apply an **mtime / `Initiated` age
threshold** (1 h): a spool or upload younger than the threshold may belong to a
live write still in flight, so it is left alone — mirroring the `incoming/`
lease-guard discipline (#183) and keeping the sweep safe even if it ever runs
periodically rather than only at startup (Section 9.5).

`Initiated` is **optional** in the S3 `ListMultipartUploads` response, and
SeaweedFS 4.33 omits it. When it is absent the adapter age-gates the upload by
its parts instead: `ListParts` returns a per-part `LastModified` (SeaweedFS does
supply this), and the newest part's timestamp stands in for `Initiated` — so a
genuine crash-orphan with parts is still reclaimed on SeaweedFS, not only on real
S3/MinIO. An upload with **zero parts** and no `Initiated` (a crash between
`CreateMultipartUpload` and the first `UploadPart`) has no timestamp to read and
is conservatively treated as just-started, so the sweep never aborts it; that
residual micro-gap holds no part bytes and is left to the operator-side
`weed shell s3.clean.uploads` (Section 7.3 / DEPLOYMENT.md). If the object store
does not support `ListMultipartUploads` at all (build-dependent), the sweep logs
a WARN and continues the rest of the sweep — orphan-part hygiene degrades rather
than failing recovery.

### 4.4 Single-file writes

`write_file` (Section 3.4) also publishes atomically, by the same principle at
file granularity: write to a temp sibling, fsync, then atomically rename over the
target (fs / remote-fs) or PUT the new object then update the pointer (object).
This keeps a concurrent `read_file` from ever seeing a torn file. Capturing the
prior version into `versions/` (Section 5) happens **before** the overwrite, so a
crash mid-write leaves both the old `current/` content and the retained version
consistent.

Like a `commit_snapshot` or `restore_backup`, an authoritative file edit replaces
the published world, so **every authoritative `current/` mutation bumps the
working-set generation** (`write_file`, `delete_file`, `delete_dir`, `make_dir`,
`rollback_file`, #889) and stamps the **`api-edit` sentinel** as the publisher
(Section 3.1). Otherwise a same-Worker scratch with `held == store` would skip the
post-edit hydrate (#767) on the next start and boot the PRE-edit world, and that
scratch's in-flight stale snapshot — the same Worker, `base == current` — would
pass the publish-time guard (Section 8) and clobber the edit; the sentinel makes it
a different-publisher publish whose base now lags, so the guard refuses it. The bump
is uniform across all five ops for a simple invariant: even `make_dir` (which only
creates an empty directory — a real one on `fs`, a zero-byte `.dir` marker on
object backends per #1125) bumps, so the staleness reasoning never special-cases
which edit "really" changed the world.

A single-file `write_file` and a whole-working-set publish/restore are never
issued concurrently for the same server: they are serialized at the application
layer per the Section 6.9 state-branching policy and decision 9.2 (Storage file
edits happen only on a stopped server, while publish happens for a running
server's snapshot or during restore, which requires a stop). The Storage adapter
itself does not arbitrate concurrent publish and `write_file` on the same server;
the application layer is responsible for not issuing them concurrently.

**Accepted gap: multi-member edits are not crash-atomic (issue #1608).**
Single-file mutations (`write_file`, `delete_file`) are atomic at file
granularity (temp-sibling rename on fs, single-object PUT on object), but
multi-member edits span several non-atomic steps before the generation bump:

- **fs `delete_dir`:** `shutil.rmtree` — per-member unlinks.
- **fs `rename_dir` / `rename_file`:** a single `rename(2)` — **atomic**.
- **object `delete_dir`:** a per-object `delete_object` loop.
- **object `rename_dir` / `rename_file`:** per-object `copy_object` +
  `delete_object` loops.
- **object `delete_file`:** the version-capture step is a single-object PUT
  (atomic), but the content `delete_object` is a separate step.

A crash mid-loop leaves `current/` partially mutated at a stale generation.
Because the missing-region gate (Section 4.5) runs only at publish time, a
partially-deleted world hydrates silently on the next start — the gate does not
re-check `current/` on hydrate.

**Why not stage-then-flip:** routing these edits through the staging-then-publish
path (Section 4.1) would turn every directory delete/rename into a
full-working-set republish (copy the entire `current/` minus the deleted subtree
into a fresh snapshot, then flip the pointer), adding multi-minute latency on
large worlds for a narrow window (requires a crash during a manual at-rest file
op on a stopped server).

**Recovery:** the operator sees the request fail. Re-running the same edit
converges (the remaining members are deleted/renamed, then the generation bumps),
or a backup restore (Section 3.3) replaces the torn `current/` wholesale.

### 4.5 Recovering from a refused `working_set_incomplete` publish (#854/#887)

When the missing-region gate refuses a snapshot publish, the Worker's `POST
…/snapshot` returns `422 working_set_incomplete` and **`current/` keeps the prior
good snapshot** — the corruption is contained, nothing is lost. The 422 body (and
the matching API log line) carries `affected_count`, a bounded `directories` list
of `{directory, missing[]}` naming the lost `.mca` files per region dir, and
`truncated` (true when the list was capped — treat it as a sample, not the
exhaustive set). The refusal repeats on every republish until the mismatch is
resolved, because the staged set is still missing regions the prior `current/`
has.

The gate fires when a staged set dropped **some-but-not-all** region files of a
dimension that still exists — Minecraft would silently regenerate the missing
chunks as fresh empty terrain, so the gate fail-closes rather than publish a
silently-holed world. There are two legitimate causes and one corruption cause:

- **Intentional world shrink (delete a whole dimension).** Deleting *all* region
  files of a dir is a full-dimension delete and is **allowed** — only a partial
  loss is refused. So the documented override for an intentional shrink is to
  delete the *whole* dimension dir, not a subset of its regions.
- **Intentional removal of specific regions.** If the operator genuinely wants
  `current/` to no longer carry those regions, delete the listed names from
  `current/` itself via the at-rest file API (`delete_file` /
  `DELETE …?path=…`, Section 3.4) with the server **stopped**. On the next
  start the Worker re-hydrates from the reconciled `current/` (the generation
  bump from any authoritative edit — `api-edit` sentinel — makes `held < store`,
  forcing a fresh hydrate) and subsequent snapshots publish cleanly against the
  trimmed world.
- **Corruption (a crash truncated/dropped regions during save).** This is the
  signature the gate exists to catch. Recover the lost regions — restore a backup
  (Section 3.3, which bypasses this gate by design — a backup is a complete set)
  or let the Worker repack a healthy scratch — rather than deleting them from
  `current/`.

The recovery procedure is therefore: **stop the server → reconcile `current/`
(delete the listed names at-rest for an intended removal, or restore a backup for
corruption) → the next start re-hydrates the reconciled `current/`** (a
`restore_backup` — and any authoritative file-API edit — bumps the working-set
generation, so the Worker always sees `held < store` on the next start and
re-hydrates rather than reusing a stale scratch; see Section 3.3 `restore_backup`
row). Subsequent snapshots then publish cleanly against the reconciled world.

### 4.6 Worker `.displaced-<id>` trees: lifecycle, operator recovery, and cleanup (#906/#910/#911)

#### What creates a `.displaced-<id>` tree

When a server's final stop snapshot **definitively fails** — refused by the
integrity gate (#739/#927), refused by the missing-region gate (#854), or
otherwise non-transiently rejected — issue #845 retains the Worker's scratch dir
so the only copy of the world survives. On the **next start**, the `HydrateTrigger`
would normally overwrite the scratch dir with the authoritative snapshot.
Issue #910 changes that overwrite to a **rename aside**: the old scratch
`<scratch>/<id>` is moved to `<scratch>/.displaced-<id>` before the new working
set is unpacked in its place. The displaced tree is the retained-for-recovery copy
of the world as it stood before the failed final snapshot.

**Location on the Worker.** `<worker.scratch_dir>/.displaced-<server-id>` — a
dot-prefixed sibling of the server's normal scratch dir. Only one displaced tree
exists per server at any time (a new hydrate replaces the prior one).

**Lifecycle.** The displaced tree is **never GC'd automatically on server delete**
— it is the last surviving copy of the world exactly when it is most needed. It is
reclaimed only when a subsequent snapshot **succeeds** for the same server id
(`sweepDisplaced`, called from both the running-id and stopped-id
snapshot-success branches): that success proves the store supersedes the displaced
world, making the local copy redundant. A server deleted after a failed final
snapshot never snapshots again and its displaced tree therefore **persists on the
Worker indefinitely** — bounded to one working-set worth of disk per deleted
server. Note: the issue #924 scratch reclaim (`ReclaimDeletedScratches`)
intentionally does **not** reclaim `.displaced-<id>` trees — only the scratch dir
and `.hydrate-<id>-*` leftovers.

**Boot detection.** At Worker boot, after the held-server scan,
`WarnOrphanDisplacedTrees` logs a `WARN` for each `.displaced-<id>` tree whose
server id is **not** in the held-server set. A tree is "assigned" (not logged) if
the same id also has a live scratch in the held set; it will be GC'd on the next
successful snapshot. Note: a held-but-idle server that never snapshots again retains
its displaced tree silently and indefinitely — this is INTENDED (the tree is the
recovery copy; reclaiming it requires a successful snapshot). The manual-cleanup
criteria below apply equally to this case. A tree is "orphaned" (logged) if the
server was deleted or re-placed to another Worker and this Worker will never
snapshot it again:

```
WARN  displaced recovery tree for unknown/unassigned server found at boot; manual cleanup or recovery may be needed (see STORAGE.md Section 4.6)  path=<scratch>/.displaced-<id>  server_id=<id>
```

#### Operator recovery procedure

1. **Identify the displaced tree.** The boot WARN gives the full path. Or scan the
   Worker's `worker.scratch_dir` for directories matching `.displaced-*`.
2. **Assess the world.** The displaced tree is a plain working-set directory (the
   same layout the Worker normally holds in `<scratch>/<id>`). `level.dat` and
   region dirs live under `world/` inside the displaced tree (e.g.
   `.displaced-<id>/world/level.dat`, `.displaced-<id>/world/region/`). Inspect
   those paths to confirm the world data looks intact.
3. **Recover the world.** Two options:

   - **Repack as a new backup (recommended).** Tar the displaced tree, upload it
     as a backup to a server (the upload-backup flow, issue #281 / `POST
     /api/communities/{community_id}/servers/{server_id}/backups/upload`), and restore it
     (`restore_backup`). This brings the world back into the authoritative store
     and into a new or existing server — no manual SSH to the Worker host needed
     once the tar is in hand.

   - **Copy directly into an existing server's scratch.** Stop the target server,
     copy the displaced tree content into `<scratch>/<target-id>` (or swap the
     whole directory), then start the server. A hydrate will follow from the API
     unless the Worker holds a fresh enough generation — if in doubt, bump the
     authoritative store generation by any at-rest edit (`write_file` etc.) so the
     next start is forced to re-hydrate from the current store, not the patched
     scratch. **Use with care:** writing directly into the scratch bypasses all
     integrity gates.

4. **Remove the displaced tree.** Once recovery is confirmed and the world is
   safely in the store (or the world is genuinely not needed), delete the
   displaced tree: `rm -rf <scratch>/.displaced-<id>`. The directory name is
   dot-prefixed so it is never touched by any Worker-internal sweep (the
   `sweepHydrateLeftovers` and snapshot-spool sweeps only target their own
   prefixes); only `sweepDisplaced` removes it, and only on a successful snapshot
   for the matching id.

#### When is manual cleanup safe?

The displaced tree is safe to delete **after** confirming at least one of:

- The world data is in the authoritative store at a generation that covers the
  events since the last published snapshot (you can inspect the store's `current/`
  or compare file timestamps).
- A backup derived from the displaced tree (or from the authoritative store at a
  sufficiently recent generation) has been made and verified.
- The world is genuinely not needed (the server was intentionally deleted and its
  data is expendable).

Do **not** delete the displaced tree speculatively: it may be the only copy of the
world, and the next authoritative snapshot for this server would have GC'd it for
free.

#### Accepted micro-edge: running-id snapshot sweeps a displaced tree

A running-id periodic snapshot for server id S succeeds and calls `sweepDisplaced`
even if S had a stop→failed-final→re-place→hydrate sequence in the interim: the
new scratch for S (delivered by the hydrate) is the current authoritative world,
so sweeping `.displaced-S` at that snapshot is correct — the displaced tree is
no longer the only copy. The `#739` integrity checks gate the publish, so a
genuinely torn new scratch is refused and the displaced tree is retained until a
clean snapshot lands. This sequence is a non-regression micro-edge: the recovery
insurance (`sweepDisplaced` only on success) and the integrity gate together keep
the displaced tree alive exactly as long as it is needed.

---

## 5. File version retention

**Requirement (FR-FILE-3).** Each file edit is versioned; any retained version
can be restored.

**Decision — copy-on-write per file, count-bounded retention.** On every
`write_file` (and `rollback_file`), the **previous** content of that file is
copied into `versions/<rel-path>/<version-id>` before the new content is
published. `version-id` is a monotonic, opaque id whose lexicographic order is
creation order, so listing and pruning are a plain enumeration of the version
directory. Versions are **storage-only** — no database table indexes them
(DATABASE.md Section 8) and no author is recorded, so per-file attribution
("who edited this file") is not available from any layer.

Retention is bounded by a **configurable per-file version count** (default and
key owned by #16); when the count is exceeded the oldest version of that file is
pruned. Versioning applies to the **authoritative copy only** — edits to a
running server's live working set go over the control plane (ARCHITECTURE.md
Section 7.2) and are captured as versions when that working set is next
snapshotted and the resulting file differs, not on each keystroke.

**Crash-safe capture (issue #1955).** The version capture uses the same
temp-sibling + fsync + atomic rename discipline as Section 4.4 single-file
writes: the prior content is streamed into a dot-prefixed temp sibling
(`.{version-id}.*.tmp`) in the target `versions/` directory, fsynced, then
atomically renamed to its final name. A crash mid-capture leaves only the temp
sibling — never a truncated file under a valid version id. The enumerators
(`list_file_versions`, `_matches_newest_version`, `_prune_versions`) filter
dot-prefixed entries so leftover temps are invisible to callers. The startup
sweep (`_sweep_server`) reclaims stale `.*.tmp` files under `versions/` using
the same mtime age threshold as backup spool litter (issue #903).

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
differ**, and #16 selects between them explicitly. Config: set
`storage.backend = remote-fs` and point `storage.fs.root` at the mount path
(CONFIGURATION.md Section 5.2).

### 7.3 object (object storage)

| Aspect | Guarantee / mechanism |
|---|---|
| Backing | An S3-compatible object store; the Section 2 tree is a **key-prefix scheme**, not directories. |
| Atomic publish | **Pointer-object flip** (Section 4.2): write data objects under a fresh prefix, then atomically overwrite a single pointer/manifest object to reference it. Relies on the store's read-after-write and atomic single-object PUT. |
| Single-file write | PUT the new object, then update the pointer; the pointer flip is the atomic point. |
| Path-traversal | The same canonicalization is applied to derive the **key** from `rel_path`; a `rel_path` that would escape the server's key prefix is rejected (Section 6). No symlinks exist, removing that vector. |
| Garbage collection | Orphaned prefixes (from aborted/crashed publishes, or old prefixes after a flip) are reclaimed by a sweep keyed off the live pointer (Section 4.3). |
| Best for | Decoupling the authoritative store from any host filesystem; durability/scale beyond local disk. |
| Caveat | No atomic multi-object rename and no real directories — hence the pointer-flip design. List operations are prefix scans. Multi-member at-rest edits (`delete_dir`, `rename_dir`, `rename_file`) are per-object loops, not crash-atomic (Section 4.4 accepted gap, #1608). |

**Shipped deployment (issue #702).** This is the **default** backend for the
compose deployment, realized over **SeaweedFS** (Apache-2.0, master/volume,
designed for many small files). The deployment wiring, credentials, opt-out, and
the live contract tests are in
[DEPLOYMENT.md Section 5](../dev/DEPLOYMENT.md#5-storage-backend-object-on-seaweedfs-default).
The app implements its own snapshot/version logic, so S3 versioning / object-lock
are not required. Orphan multipart reclamation is **defense-in-depth (issue
#2260)**: the startup sweep age-gates uploads via `ListParts` when SeaweedFS omits
`Initiated` (Section 4.3), and behind it a bucket-level
`AbortIncompleteMultipartUpload` lifecycle rule reclaims the residual gap the
sweep cannot age-gate — an upload that crashes after `CreateMultipartUpload` but
before its first part, which carries no timestamp. The rule is applied by a
one-shot compose service (`seaweedfs-lifecycle`) on startup and self-verifies
that SeaweedFS honored it (DEPLOYMENT.md Section 5); `weed shell
s3.clean.uploads` remains an optional operator-side cleanup.
**Bucket provisioning (issue #946).**
SeaweedFS auto-creates the bucket on the first **write**, not on read, so a fresh
deployment needs no manual bucket setup: every **read** against the not-yet-created
bucket returns `NoSuchBucket`, which the adapter treats as empty/not-found, so the
startup sweep (and the API lifespan) boot cleanly against a bucketless store and the
first publish creates the bucket. A non-SeaweedFS S3 backend that does not
auto-create buckets must have the bucket **pre-provisioned**. The hot
path is **CopyObject-heavy and small-object-heavy** — each publish server-side
copies every world file into a fresh prefix and uploads members via multipart — so
operating cost/latency scale with **operation count** (files × snapshot
frequency), not stored size or egress; keep the snapshot interval coarse enough
that a publish completes well within it.

**At-rest integrity sweep limitation (issue #926).** The at-rest integrity sweep
(`check_current_health`, Section 3.1; `check_backup_health`) returns a healthy
`WorkingSetReport` unconditionally on the object backend — it does not inspect
the stored objects. The reason is structural: the object backend has no local
working-set directory to walk; the fsck implementation walks a local filesystem
tree (the `current/` symlink target on fs), and no equivalent materialisation
exists on the object side. The **publish-time** fsck (`_check_staged_regions`,
wired on `commit_snapshot`, `restore_backup`, and `create_backup_from_current`)
**is** implemented on the
object adapter and remains the authoritative gate — it downloads and validates
each `.mca` member during staging, so a corrupt region is refused before it
becomes authoritative. The gap is limited to the **read-only sweep** that
re-checks already-published snapshots and backups at rest: on the object backend
that sweep sees every server and backup as healthy regardless of actual content.
A future enhancement could fetch and structurally check the `.mca` objects from
the store (downloading headers only, mirroring the fs walker), but it is not
implemented — the publish-time gate is the correctness guarantee today, and the
sweep is defense-in-depth.

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

**Endpoints** (scoped by `(community_id, server_id)`). Like the rest of the HTTP
API these are namespaced under `/api` (issue #498); the API builds the full
`transfer_url` it hands the Worker over the control plane, so the Worker follows
whatever path the API emits:

| Method & path | Meaning | Success | Errors |
|---|---|---|---|
| `GET /api/data-plane/communities/{c}/servers/{s}/working-set` | Hydrate: stream the authoritative working set as a tar (with the resolved `server.jar` injected when present, #118). Response always carries `X-Working-Set-Generation: <n>` (the generation of the snapshot served; 0 when no snapshot has been published). | `200` tar body | `204` no published snapshot *and* no resolved JAR (Worker starts from an empty dir, generation header still present); `401` |
| `POST /api/data-plane/communities/{c}/servers/{s}/snapshot` | Snapshot: stream a tar into staging and atomically publish it. The request MAY carry `X-Working-Set-Base-Generation: <n>` (the store generation this set was hydrated from) and `X-Worker-Id: <id>` (the publishing Worker), both read by the publish-time generation guard (#847); a never-hydrated/older Worker omits them and the guard stays permissive. The content-integrity gate uses the single region rule set (#927): a non-4096-aligned tail is the normal on-disk format of a 26.x world, not corruption (byte-precise per-chunk bounds), on any source — the `X-Snapshot-Source` mode header (#923) is removed. On success the response carries `X-Working-Set-Generation: <n>` (the new generation minted by the publish). | `204` | `400` length mismatch / incomplete; `400` `empty_snapshot` (staged an empty working set); `409` `stale_generation` + `base_generation` + `current` (the declared base is older than the store's current generation AND that current was published by a *different* Worker — an A→B→A stale-scratch publish; a same-Worker lag is a lost response and is allowed to self-heal, #847); `411` no `Content-Length`; `413` over the size cap; `422` `working_set_corrupt` + `corrupt_count` (integrity gate refused the staged set, #739); `422` `working_set_incomplete` + `affected_count` + `directories` + `truncated` (the missing-region gate refused a staged set that dropped some-but-not-all region files of a still-live dimension, #854; `directories` is a **bounded** per-directory list of the lost `.mca` names — `{directory, missing[]}` capped per #887 — so the operator can drive the recovery in Section 4.5, `truncated` flags that the list was capped); `401` |

**`X-Worker-Id` trust (M1).** The `X-Worker-Id` the guard reads is **not**
authenticated within the shared-credential data plane: any Worker holding the
transfer token can claim any id (consistent with the #779 trust model). This is
acceptable — the guard is defense-in-depth behind #847's primary fix (the API
holds the assignment across the final snapshot, so the genuine stale cross-worker
publish never arises), and a spoofed id can at worst weaken (never strengthen) the
guard for a caller already trusted to write the working set.

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

The version catalog lists/resolves vanilla (Mojang
manifest), Paper (PaperMC API), fabric (meta.fabricmc.net), and forge (the Forge
Maven + promotions feed). Forge resolves to
the *installer* JAR, injected into the working set at `server.jar` like the other
types; the worker runs `--installServer` on first start, so the same hydrate
mechanism serves it. The fabric server launcher JAR has no upstream checksum, so
it is pooled content-addressed by its own SHA-256 without source-digest
verification; vanilla (SHA-1), Paper (SHA-256), and forge (SHA-1, the installer's
sibling `.sha1`) are verified against their published digest before pooling.

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
working dir into a tar and POST it with a `Content-Length`. Before packing, the
Worker runs a structural region fsck over the working set (issue #765): if any
`.mca` file is found corrupt, the snapshot is refused at source with a
`TRANSFER_FAILED` outcome — a clear failure with no wasted tar+upload — rather
than shipping a corrupt set the API gate (the `422` above) would reject after a
full round-trip. A fsck I/O error is best-effort (logged, transfer proceeds) so
the fsck never wedges a snapshot; the API integrity gate is the correctness
guarantee.

**One region rule set, applied everywhere (issue #927).** The structural fsck and
the API integrity gate run **one rule set** at every gate — no source-keyed mode
split. MC 26.x pads region files to a 4096-byte sector boundary only on
shutdown/close, so a **running** (even quiesced) world legitimately keeps an
**unpadded tail** — the last chunk ends mid-sector and the file size is not a 4096
multiple. (Verified on a live 26.1.2 server: the trailing chunk is complete and
decompresses cleanly.) The rule accepts that unpadded tail and applies a
**byte-precise** per-chunk bound (`offset*4096 + 4 + length <= size`): a trailing
chunk whose declared length overruns the real EOF is still corrupt, an entry
pointing at/past EOF is `sector_out_of_bounds`, a severed prefix is a short-read
`truncated_chunk`. Alignment is retained as a signal only for the
**sub-header-size** case (a non-zero size below the two 4096-byte header sectors is
a torn save, reported `not_4096_aligned`). A 0-byte file is an empty container
(healthy, #905).

The earlier design (#923/#925) split the rule by snapshot **source**: a **stopped**
world was assumed 4096-padded, so a non-4096 size there was treated as a torn save
(strict), while a running source ran the byte-precise rule (live), declared via an
`X-Snapshot-Source` request header. That `stopped => 4096-padded` invariant **does
not hold**: a sweep-stop timeout, SIGKILL, OOM, crash, or host loss can leave a
stopped world's regions unpadded, and the strict rule then refused the **stop-leg
checkpoint exactly when it is the last chance to capture the world** (observed
2026-06-12 local: the orphan sweep stopped a running 26.1.2 server, the stop-leg
redispatch issued a stopped-id snapshot, and the pre-pack fsck refused in 7ms —
`snapshot refused: 5/22 region files corrupt (e.g. r.-1.-2.mca: not_4096_aligned)`
— 5 regions still unpadded after the stop timeout). Strict added detection power
**only** under that invalid invariant, so the split is collapsed and the
`X-Snapshot-Source` header is removed end-to-end. Every gate — the Worker's pre-pack
fsck on either path, the API publish gate, `create_backup_from_current`,
`restore_backup`, `check_current_health`, `check_backup_health`, and the
integrity-sweep fscks — runs the single rule set, so a legitimately unpadded set
committed into `current/` (or any archive derived from it) is never falsely rejected.

Because `check_backup_health` runs the single rule set and the sweep rewrites the
health column every pass, a backup quarantined before #925 whose **only** finding
was `not_4096_aligned` (all referenced chunk extents within EOF) is re-marked
HEALTHY on the next sweep. This rescue is intentional: such a backup is loadable
content, and the realistic torn shapes stay caught (a location entry at/past EOF →
`sector_out_of_bounds`; truncation severing a referenced chunk → byte-precise
`truncated_chunk`/short-prefix).

Transport
security
(CA bundle / mTLS / dev-insecure) mirrors the control channel
(CONFIGURATION.md Section 6.1). The session routes every server-scoped command —
hydrate/snapshot as well as lifecycle/file commands — to a per-server lane off
the receive loop: one server's commands run serially (start/stop never
interleave), but distinct servers' lanes run concurrently under a bounded
concurrency cap, so a slow transfer or graceful stop never delays another
server's command (issue #95).

**Lifecycle wiring (FR-DATA-4).** `StartServer` hydrates before the launch (the
API issues a hydrate trigger, then `StartServer`); a graceful `StopServer` records observed=stopped and clears the assignment first
(once the Worker confirms the process is gone), then takes a final snapshot
best-effort (the API issues the stop, then — after the Worker confirms exit —
records stopped, clears the assignment, and issues a snapshot trigger; the
snapshot failure does not fail the stop; scratch is reclaimed only after the
snapshot publishes, issue #845). A `HydrateTrigger` is only valid for a stopped
server (refreshing a running server's working set would corrupt live state).

---

## 9. Design decisions

Each records the decision, alternatives, and rationale, compactly
(NFR-SCALE-1: thorough, not bloated). Decisions already stated inline — the
copy-on-write version scheme (Section 5) and the per-family publish mechanism
(Section 4.2) — are not repeated here.

### 9.1 Per-Community → per-server namespacing; JARs shared

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

### 9.2 Atomicity enforced by `Storage`; state preconditions by the application

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

### 9.3 The Port hides the data-plane transport

**Decision.** `Storage` exposes streams and a publish handshake (Section 3.1);
the API↔Worker transfer wire format is the data plane (epic #8), not part of this
Port.

**Alternatives.** Fold the transfer protocol into `Storage`.

**Rationale.** Keeps `Storage` about the authoritative store and lets the data
plane evolve (chunking, resumable transfer, future delta sync — FR-DATA-5)
without changing the store contract. The two meet at the stream boundary only.

### 9.4 Backups are whole-working-set archives, not Storage-internal links

**Decision.** A backup is a self-contained archive (Section 3.3), independent of
any Worker and of the current `current/`.

**Alternatives.** Backups as references/snapshots-in-place sharing data with
`current/` (copy-on-write clones).

**Rationale.** FR-BAK-2 requires a backup to be a retained snapshot that does not
depend on a specific Worker; self-containment also means a backup survives
deletion of the live working set and restores cleanly via the same atomic-publish
path. In-place clones would couple a backup's integrity to the live copy's
lifecycle and are backend-specific (not all backends offer cheap clones).

### 9.5 Out of scope / deferred (carried as follow-ups, not implemented here)

- **Orphan-sweep scheduling** (Section 4.3): **now implemented** — the recovery
  sweep runs at API startup and then on a periodic loop
  (`storage_sweep.interval_seconds`, daily by default; issue #2252, PR #2255). The
  spool / multipart reclaim paths apply a 1 h age threshold (Section 4.3, #903), so
  the periodic pass does not eat a live write's temp spool. What remains a carried
  follow-up is the multipart long-upload caveat below and its storage-layer
  mitigation (#2251).
  **Multipart long-upload caveat** (#916): the threshold is *not* fully safe for a
  long-running multipart upload. Unlike an fs spool's mtime — which advances as the
  write progresses — the S3 `Initiated` timestamp is fixed at create time and never
  refreshes, so a legitimate upload still streaming past the 1 h threshold *can now
  be aborted mid-flight* by a periodic sweep pass. This is a real risk under the
  periodic loop, where it was structurally impossible while the sweep ran at startup
  only (no live upload during recovery). The failure is loud, not silent — the
  upload's next `upload_part`/`complete` gets `NoSuchUpload`, which surfaces as the
  backup error and **must be retried** (`upload_multipart`'s cleanup routes through
  the *translated* idempotent abort, so a complete-vs-abort race makes that cleanup a
  no-op and the original error is no longer masked — issue #935). Mitigations: the
  daily loop cadence keeps the sweep cold, so overlap with a multipart upload still
  running after an hour stays rare; and the storage-layer
  `AbortIncompleteMultipartUpload` bucket lifecycle rule (umbrella #2251) is the
  primary defense — it reclaims orphan parts natively with its own generous age, so
  the sweep's in-process multipart abort is a backstop rather than the mechanism that
  must catch every orphan within a tight window.
- **Continuous delta sync** (FR-DATA-5) is explicitly deferred; the streaming
  Port shape leaves room for it.
- **WebUI surfacing of `working_set_incomplete`** (tracked: #900). The 422 the
  missing-region gate returns is today a transient response to the *Worker's*
  snapshot `POST` (Section 4.5); the WebUI never makes that call and no
  control-plane route exposes a server-level "last snapshot was refused as
  incomplete" condition (unlike backup `health`, #745, which the
  WebUI already badges). Surfacing it would need new machinery — persist the
  refused condition on the server record (or expose `check_current_health`,
  Section 3.1, over a control-plane route) and add a server-page badge/notice —
  which is out of scope for this gate-hardening change. **API contract for the
  follow-up:** reuse the 422 body shape above (`reason: "working_set_incomplete"`,
  `affected_count`, bounded `directories: [{directory, missing[]}]`, `truncated`)
  and badge the server following the #739/#745 health-badge pattern, pointing the
  operator at the Section 4.5 recovery.

---

## 10. Related documents

| Doc | Relationship |
|---|---|
| [`../REQUIREMENTS.md`](../REQUIREMENTS.md) | Source of truth: Sections 6.8–6.12, FR-DATA-1…7, FR-FILE-3, FR-FILE-4, FR-BAK-*, FR-VER-3. |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | `Storage` Port placement (Section 5.1), layering and edge wiring (Section 2), naming (Section 6), the running-server file-access decision (Section 7.2) that bounds this Port's file ops to the stopped-server case. |
| [`DATABASE.md`](DATABASE.md) | The metadata indexing the blobs this document lays out (`Server`, `Backup`). Blob layout is here; metadata tables are there. File versions have no metadata table — they are storage-only (DATABASE.md Section 8). |
| [`CONFIGURATION.md`](CONFIGURATION.md) | The runtime keys that select the backend family and tune snapshot interval / version-retention count. This document defines *what* is selectable; `CONFIGURATION.md` names the keys. |
