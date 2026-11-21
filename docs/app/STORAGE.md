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

The Port contract (Sections 3–7) specifies **what the Port guarantees**, not
how the bytes move between API and Worker. The data-plane HTTP transfer
endpoints (hydrate / snapshot wire format, chunking) are the
transport those guarantees plug into; they are outside the Port and are
specified separately in Section 8. Where the data plane meets `Storage`, the
boundary is named but not specified (Sections 3.1, 4).

Target scale is small (NFR-SCALE-1): a few dozen Communities, tens of concurrent
servers, a single API instance. The contract is therefore deliberately thin —
enough operations to satisfy Sections 6.8–6.12, no speculative generality — while
keeping the *shape* backend-neutral so a larger backend can replace the default one
without touching business logic (REQUIREMENTS.md Section 1.1).

### 1.1 What lives behind the Port vs. above it

- **Blob layout** (the byte-level on-backend layout of working sets, JARs,
  backup archives, and file versions) is owned by this document.
- **Metadata** (the `Server`, `Backup` rows that index those blobs, schedules,
  audit) lives in the database and is owned by `DATABASE.md`. File
  versions are the exception: they carry no metadata rows and live wholly
  behind the Port (Section 5). `Storage` returns opaque keys/handles; the metadata
  layer records them. The Port never queries the database and the database never
  reaches into the backend.
- **Config key names** for selecting and tuning a backend are owned by
  `CONFIGURATION.md`. This document defines **what** is selectable and
  tunable (the backend family, the snapshot/version-retention knobs);
  `CONFIGURATION.md` names the keys.

---

## 2. Authoritative data layout

Authoritative data is namespaced **per Community, then per server**, mirroring
the isolation unit in REQUIREMENTS.md Section 6.2. JARs are **shared across all
Communities** because they are immutable, content-addressed artifacts reused
across servers (FR-VER-3) and contain no Community data.

```
<root>/
├── mcsd-owner.json                           # the store owner marker: which database this store belongs to (Section 4.3)
├── communities/
│   └── <community-id>/
│       └── servers/
│           └── <server-id>/
│               ├── current -> snapshots/<snapshot-id>/  # symlink to the live snapshot; the publish pointer (Section 4)
│               ├── generation                # combined marker (Section 3.1): line 1 the monotonic working-set generation counter (bumped on each authoritative publish: commit_snapshot, restore_backup, and authoritative file edits; also by the post-delete prune, which never removes it), line 2 (optional) the publishing Worker id
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
├── jars/                                     # shared, content-addressed server JARs (FR-VER-3)
│   └── <sha256>.jar
├── plugin-cache/                             # object backend only: content-addressed plugin/mod JAR cache
│   └── <sha256>
└── resource-packs/                           # object backend only: uploaded resource packs
    └── <pack-id>/
        └── <filename>
```

Notes:

- `current` is a **symlink** to the live entry under `snapshots/`; it names the
  authoritative copy and is the atomic-publish pointer (Section 4.2).
  Dereferencing `current/` reaches the authoritative working set. While a server
  is running that copy is temporarily stale (FR-DATA-4); `Storage` does not know
  server state — the application layer decides when to read `current/` vs.
  read-through to the Worker per the REQUIREMENTS.md Section 6.9 state-branching
  policy.
- `snapshots/<snapshot-id>/` holds published working-set states. Only the one
  targeted by the `current` symlink is authoritative; prior snapshots are
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
  on-disk name; the `fs` adapter uses gzip (`.tar.gz`); zstd is reserved, not
  implemented.
- `generation` is a plain-text marker holding **two** values: line 1 is the
  current authoritative working-set generation counter (a monotonically
  increasing integer, starting at 1 after the first publish); line 2 (optional)
  is the **publishing Worker id** — the id of the Worker that produced `current`,
  used by the publish-time generation guard (Section 8) to tell a
  same-Worker re-publish (lost-response self-heal) from a different-Worker stale
  publish (A→B→A). Both are written as **one atomic marker** (a single
  temp-sibling + atomic rename on `fs`, a single object `PUT` on object backends):
  a crash between two separate writes could attribute the *prior* publisher to
  the *new* generation and invert the guard, so the pair must be all-or-nothing.
  In a **pointer-flip publish** the marker is a separate sequential write, on
  the side of the flip where a failure between the two errs safe. A
  `commit_snapshot` writes it immediately after the flip: a crash in between
  leaves the generation one behind (under-states by one — the safe direction:
  `current` already names the new world the producing Worker also holds in scratch,
  so a hydrate-skip cannot serve a *stale* world, and the failed call's retry
  republishes and bumps the marker into agreement). A `restore_backup` writes it
  immediately **before** the flip (#3564): no Worker holds the restored world, so
  an under-stated generation would let a same-Worker scratch with `held == store`
  skip the hydrate and boot the PRE-restore world, then publish it over the
  restore. A crash in between instead over-states by one: the prior world stays
  live under the new generation, so every holder of the old generation hydrates
  it again. That is the accepted cost: a stopped Worker's scratch progress that
  was never published (its final snapshot failed) is displaced by that hydrate
  on its next start and lost, where without the failed restore it would have
  booted and been published. The operator was replacing that world with the
  backup anyway. The snapshot upload guard takes its base with
  `settled_generation`, under the per-server lock, so it never observes a restore
  (or any other mutation) half done (#3564). An **in-place authoritative
  file edit** (`write_file` / `delete_file` / `delete_dir` / `make_dir` /
  `rollback_file`) instead mutates `current/` itself rather than flipping a
  fresh pointer, so its mutate→bump crash window is **not** the safe direction: a
  crash after the mutation but before the bump leaves the edited world live at the
  OLD generation. Multi-member edits (`delete_dir` on fs uses `rmtree` —
  per-member unlinks; on object backends `delete_dir` / `rename_dir` /
  `rename_file` are per-object loops) additionally have a **mid-mutation window**:
  a crash partway through the loop leaves `current/` partially mutated at a stale
  generation (see Section 4.4 accepted gap). A same-Worker scratch with
  `held == store` would then skip the post-edit hydrate and boot the PRE-edit
  world — the very staleness the bump exists to prevent — for that edit until the
  next bump. To keep that window crash-recoverable rather
  than racy against a concurrent publish, the edit's mutate+bump — and a
  `commit_snapshot`'s stale re-check + flip + bump — run under a per-server
  lock so a concurrent publish/edit cannot interleave between the two steps. An edit
  resolves its read-set (the live pointer / `current` symlink) **inside** that
  lock: resolving it outside would let it write back a snapshot the commit has
  already flipped away and reclaimed, losing the world. The lock covers only the
  re-check + pointer flip + bump (and the gates that must be atomic with the flip);
  a publish's bulk staging→snapshot copy runs **before** the lock and the prior
  snapshot's reclaim runs **after** it, so a multi-minute copy never blocks edits.
  The post-lock reclaim is safe because once an edit re-reads the pointer under
  the lock it cannot observe the pre-flip snapshot. The lock is **in-process
  only** (the API runs as a single uvicorn process); a multi-process deployment
  would need a shared lock. After any successful publish/edit return the pointer,
  generation, and publisher are in agreement. A publish that declared no
  Worker id omits line 2 (no publisher claim, so the guard stays permissive). A
  server with no published snapshot has no `generation` marker; reading it in that
  state returns generation 0 / no publisher. On object backends it is a single key
  `generation` under the server prefix. The post-delete prune (Section 2.1)
  **advances** it rather than removing it, so nothing ever lowers the generation:
  `DeleteServer` commits its row delete only after the prune, and a row that
  survives a failed commit must never read a generation below one a Worker may
  still hold, or that Worker's next start would skip the hydrate and boot stale
  scratch over a newer publish (issue #3434).
- On object backends the tree above is a **key prefix scheme**, not real
  directories (Section 7.3); the same logical layout applies.
- `plugin-cache/` and `resource-packs/` exist on the object backend only, where
  their stores share the bucket; on fs / remote-fs neither store is built, and
  a route that needs one answers 503. They sit outside the `Storage` Port: the
  pack and plugin rows are the source of truth for what is in them, and each is
  reclaimed by its own GC (the resource-pack GC is in Section 4.3). The four
  top-level prefixes are named once, in the object adapter's `KeyNamespace`.

### 2.1 Post-delete retention

Deleting a server cascades its DB rows (the server row, its backup rows) but does
**not** wipe its Storage. To bound the disk cost while never destroying the latest
state, `DeleteServer` retains **exactly two** artifacts under the server directory,
**neither of which has a DB row** (they are operator-level artifacts):

1. **The latest backup archive, if one exists** — `backups/<newest-id>.tar.gz`. The
   newest backup by `created_at` is kept; every older archive is deleted,
   archive-first per the `delete_backup` ordering convention (Section 3.3). (The
   per-file `versions/` tree is not pruned here — it is removed by the working-set
   prune in point 2, before any archive delete.) Selection is the literal
   latest-by-`created_at` **regardless of `health`** ("latest existing", not
   "latest healthy"): a QUARANTINED newest archive is still the one kept. The mandatory `final.tar.gz`
   below is the safety net — it is strictly newer and is the recommended recovery
   source if the retained backup is suspect.
2. **The current working set, packed as `final.tar.gz`** — mandatory. The live
   `current/` snapshot is packed into a single self-contained `tar.gz` at the server
   root, then the unpacked working-set tree (`snapshots/`, `incoming/`, `versions/`,
   and the `current` pointer) is removed and the generation marker is advanced
   (Section 2, generation marker). Packing is
   **fail-closed**: if it fails, the working set is left intact and the delete fails,
   so a deletion never silently loses the latest state. A server that never published
   a snapshot has no `final.tar.gz`, but its generation marker is still advanced, so
   a first upload racing the delete is refused at commit like any other (issue
   #3439). The pack deliberately **bypasses the `.mca`
   integrity gate** (the gate applied to `create_backup_from_current`): a corrupt
   server must still be deletable, so torn regions are packed as-is.

Everything else for the server is removed; after the prune the server directory
holds only `backups/<newest-id>.tar.gz` (if any), `final.tar.gz` (if published)
and the `generation` marker — no `snapshots/`, `incoming/`,
`versions/`, or `current` pointer remain. Nothing removes the server directory
after a successful delete, so the few-byte marker is retained with the archives
until an operator reclaims the directory. The retained archives are unreferenced orphans by design (not the bug the
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
flow, Section 3.3), then **restores** it — `restore_backup` republishes the `tar.gz`
into the new server's `current/`. To reclaim the disk instead, delete the server
directory tree.

---

## 3. The `Storage` Port contract

The Port is defined in `api/`'s domain layer and depended on by use cases
(ARCHITECTURE.md Section 5.1). Every operation is scoped by an explicit
`(community_id, server_id)` (or, for JARs, unscoped) so the adapter can resolve
the namespace and enforce isolation; no operation accepts an absolute path.

Operations are grouped by the requirement area they serve. Signatures are
language-neutral sketches; the binding Python interface is the `Storage` ABC in
`api/`'s storage domain layer (`storage/domain/port.py`).

### 3.1 Working-set hydrate / snapshot

Serve the runtime data lifecycle (FR-DATA-4). These move the whole working set
between `current/` and the data plane; they do **not** transfer to the Worker
directly (that is the data-plane endpoint's job, Section 8). `Storage` provides the
authoritative-side stream and the atomic-publish handshake.

| Operation | Purpose | Notes |
|---|---|---|
| `open_hydrate_source(community_id, server_id) -> ReadStream` | Open a read stream over the current authoritative working set | The data plane reads from this to feed a Worker on start/relocation (hydrate). Reads `current/`. |
| `begin_snapshot(community_id, server_id) -> SnapshotHandle` | Start an incoming snapshot transfer | Allocates an isolated `incoming/<transfer-id>/` staging area. |
| `write_snapshot(handle, WriteStream, expected_base?)` | Stream the Worker's working set into staging | Writes only into staging, never `current/`. May be called incrementally. Staging a concurrent delete's prune removed mid-write is refused as `PrunedStoreError` (Section 4.1). |
| `commit_snapshot(handle, *, publisher=None, expected_base=None, publisher_fence=None) -> int` | Atomically publish the staged snapshot as the new authoritative copy; return the new generation | Atomic publish (Section 4). After return, `current/` reflects the complete transfer or the prior copy — never a partial. Bumps and returns the working-set generation counter (the new integer the Worker records as the generation its scratch is at) and records `publisher` (the producing Worker's id) alongside it in one atomic marker, read back via `current_publisher`. A `None` publisher records no id (the guard stays permissive). `expected_base` is the commit-time stale re-check: the generation the data-plane publish guard validated against before the upload stream (what `current` was at guard time). The commit re-reads the generation under the per-server publish/edit lock (Section 2 generation marker) and refuses with `StaleGenerationError` when it advanced past `expected_base` — an at-rest edit or restore that landed DURING the (multi-minute) upload window — so the just-bumped `current` is not clobbered; `None` skips the re-check (no base claim). `publisher_fence` is the caller's async check that the publisher may still publish, awaited once under the same lock immediately before the publish; `False` refuses with `PublisherFencedError`. The data plane passes its assignment fence here, because the commit's slow steps before the lock (the fs staging fsync, the object copy) can outlast a release and re-placement. The fence runs while the lock is held, so it must not wait on anything that can wait on this server's lock; `None` skips it. Refuses with `IncompleteTransferError` if the transfer was not signalled complete. Refuses with `IntegrityCheckError` if the staged set contains corrupt `.mca` region files, using the single region rule set (Section 8): a non-4096-aligned tail is the normal on-disk shape of a 26.x world, not a tear, on any source — the byte-precise check catches realistic tears. Refuses with `MissingRegionsError` if the staged set dropped some-but-not-all `.mca` files of a still-live dimension (the partial-loss corruption signature) — a full-dimension delete (ALL regions of a dir gone) is allowed, only a partial loss is refused; the error carries the per-directory lost names (recovery in Section 4.5). Refused publishes do NOT bump the generation; the staging is discarded. |
| `abort_snapshot(handle)` | Discard an incomplete/failed transfer | Deletes the staging area; `current/` is untouched. Also the cleanup path for crash recovery (Section 4.3). |
| `current_generation(community_id, server_id) -> int` | Return the current authoritative working-set generation | The counter `commit_snapshot` bumps, read back so the hydrate data plane can stamp the generation it serves (Section 8). Returns 0 when no snapshot has been published. |
| `settled_generation(community_id, server_id) -> int` | Return the generation once no publish is in progress | `current_generation` read under the per-server publish/edit lock, so it waits for a `commit_snapshot` that has passed its `publisher_fence` and not yet bumped. A caller that commits a state the fence refuses (another Worker assigned) before this read therefore sees every publish the fence let through. The servers context's skip-hydrate decision reads the generation this way: a final snapshot that failed at the Worker's deadline releases the hold while its upload may still be committing, and its fence passes while nobody is assigned. The snapshot upload guard reads its base this way too (#3564). Every mutation of the published working set runs whole under that lock: a commit's flip and bump, a restore's bump and flip, an at-rest edit's write and bump (rollback and first-file seeding included), and a prune. So the guard sees each one either not started or complete, and one completing after the read advances the generation, which `commit_snapshot`'s `expected_base` re-check refuses. |
| `current_publisher(community_id, server_id) -> str \| None` | Return the Worker id that published `current` | Read back from the combined `generation` marker so the publish-time generation guard (Section 8) can allow a same-Worker re-publish (lost-response self-heal) while refusing a different-Worker stale publish (A→B→A). `None` when no snapshot has been published, or the last publish declared no id (a Worker that sent no `X-Worker-Id`) — in which case the guard cannot prove a foreign publisher and stays permissive. |
| `check_current_health(community_id, server_id) -> WorkingSetReport \| None` | Structurally fsck the on-disk authoritative snapshot | Walk `current/` for corrupt `.mca` region files. Read-only — never mutates `current/`. Raises `NotFoundError` if no snapshot has been published. `None` means the backend **examined nothing** — the answer of an adapter with no local working set to walk (object, the Section 7.3 limitation), deliberately not a healthy report, so the sweep can tell "nothing was looked at" from "looked at and clean". A backend answering `None` answers it for every server, published or not, since it reads nothing that would tell the two apart; the `NotFoundError` clause is the contract of an adapter that does examine. |
| `prune_to_final_snapshot(community_id, server_id)` | Collapse the working set to one retained `final.tar.gz` and drop the tree | The `DeleteServer` reclaim path (Section 2.1). Packs `current/` then removes `snapshots/`, `incoming/`, `versions/`, and the `current` pointer; advances the generation before the pointer goes, so the generation never goes down even when the caller's row delete then fails — carrying the current publisher forward until the pointer is gone, so a pointer that survives a failed prune keeps the publish-time guard refusing a stale different-publisher upload, and clearing it afterwards; leaves `backups/`. The pointer/symlink is invalidated the instant `final.tar.gz` is durable, so a crash-retry is idempotent and never re-packs over a good final. Fail-closed on a pack failure; bypasses the `.mca` gate so a corrupt server stays deletable; no-op if nothing is published. |

The hydrate/snapshot **wire transport** (how `ReadStream`/`WriteStream` bytes
cross the API↔Worker boundary) is the data plane (Section 8). `Storage` only
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
| `jar_pool_stats() -> JarPoolStats` | Count + total bytes of the pooled JARs | A bounded scan of the one `jars/` namespace; platform-admin operational visibility. |
| `list_jars() -> [JarPoolEntry]` | Enumerate pooled JARs with key, size, and store time | The input the reference-counted GC diffs against the live reference set; each entry's `modified_at` feeds the GC safety window. |
| `delete_jar(JarKey)` | Remove a pooled JAR | Idempotent (no error if absent). The reclaim primitive the GC calls on an unreferenced JAR; the reference decision is the GC's, not Storage's. |

**Reference-counted GC.** The pool is reclaimed by a reference-counted
garbage collector: a periodic API-side loop (config `jar_gc.interval_seconds`,
default daily, gated on the control plane with the other schedulers) plus a
platform-admin `POST /versions/jar-pool/gc` manual trigger (audited; returns
`{scanned, deleted, freed_bytes, failed}`). Both run only on a store this database owns;
otherwise the manual trigger answers `409 store_not_owned`, with the reason as
`detail` (Section 4.3).

*Reference model.* A pooled JAR is **live** iff some server row's config records it
as its resolved JAR (`resolved_jar_sha256`, Section 8) — the authoritative
reference set is that bounded DB scan. Snapshots and backups do **not** pin pool
JARs: the hydrate endpoint *injects* the resolved JAR into the working-set tar at
transfer time (Section 8), so the Worker's working dir carries `server.jar` and a
snapshot/backup of it **embeds the JAR inside its own tar** (the Worker excludes
nothing when packing). A restore therefore needs no pool copy, and deleting
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

*Record and failures* (issue #3871). The JAR-pool GC, the plugin-cache GC and the
resource-pack GC (Section 4.3) run a pass the same way:

- Each object a pass reclaims is logged at INFO with its key, its size and its
  age. A resource pack's line also names its pack id. A resource pack's key is
  logged escaped, here and in the WARNING below: it ends in the upload's
  filename, and a pack stored before the upload refused control characters may
  carry them (issue #3907).
- Each pass ends with one summary line: how many objects it listed, how many it
  reclaimed and their bytes, how many deletes failed, and how long it took. The
  line is at INFO when the pass reclaimed or failed anything, and at DEBUG
  otherwise, so a pass with nothing to do leaves no INFO line.
- A delete that fails is logged at WARNING with the key and the error. The pass
  skips that object and goes on, and the next pass tries it again, so one object
  that keeps failing does not starve the ones after it. The manual trigger counts
  these in `failed`.
- Five failed deletes in a row end the pass (issue #3906); a successful delete
  starts the count again. Each failed delete can cost botocore's retries on a
  5xx, so a store that fails them all would otherwise cost every object its
  retries and a WARNING with a traceback. The pass still logs its summary, at
  WARNING with the field `reason=stopped_after_consecutive_failures`, and the
  manual trigger returns what it did so far. The objects it never reached count
  in neither `deleted` nor `failed`; the next pass tries them.
- A delete the store gave no answer to at all ends the pass at once, since every
  later delete would wait out the same timeouts. On the object backend that is a
  transport failure (connection refused, a connect or read timeout, a dropped
  connection) once botocore's retries are spent. A store that answered, even with
  a 5xx or a refusal, failed only that key. On fs and remote-fs (the JAR pool
  only) it is an errno that says the device or the mount's transport is gone:
  `EIO`, `ENODEV`, `ENXIO`, `ESTALE`, `ETIMEDOUT`, `ENOTCONN`, `EHOSTDOWN`,
  `EHOSTUNREACH`, `ENETDOWN` or `ENETUNREACH`. `EIO` is among them because a soft
  NFS mount reports a server that stopped answering that way. `EBUSY`, `EACCES`
  and the rest can be about one file, and only skip it. `EROFS` is a
  whole-filesystem state, but each delete fails at once, so it skips the JAR too,
  and five in a row end the pass.
- A cancellation (shutdown) ends the pass at once too. A pass that ends at once
  logs no summary; the per-object INFO lines still record what it reclaimed. The
  owner marker's refusal (Section 4.3) stops a pass before it lists anything, as
  before.

### 3.3 Backup archive create / list / restore / delete / transfer

Serve backup management (FR-BAK-1, FR-BAK-2, FR-BAK-4) and off-host transfer
(download / upload). A backup is a self-contained archive of a working set; it does not
depend on a Worker.

| Operation | Purpose | Notes |
|---|---|---|
| `create_backup_from_current(community_id, server_id) -> BackupKey` | Archive the authoritative `current/` into `backups/` | The **stopped-server** path (REQUIREMENTS.md Section 6.9). For a **running** server the Worker quiesces the live world (save-off → save-all → settle-wait) and commits an on-demand snapshot (`commit_snapshot`) → then the application calls this; `Storage` only ever archives the authoritative copy. |
| `list_backups(community_id, server_id) -> [BackupKey]` | Enumerate a server's backups | Metadata (label, timestamp, size) lives in the DB (DATABASE.md); this returns the keys. |
| `restore_backup(community_id, server_id, BackupKey, force=False)` | Atomically republish a backup into `current/` | Atomic publish (Section 4). Caller must ensure the server is **stopped** (FR-BAK-4); `Storage` enforces atomicity, the application enforces the stop precondition. A restore replaces `current/`, so it **bumps the working-set generation** like a `commit_snapshot` — otherwise a same-Worker scratch with `held == store` would skip the post-restore hydrate on the next start and boot the PRE-restore world. The publisher is stamped with the `api-restore` sentinel (no producing Worker), so the publish-time guard (Section 8) treats an in-flight stale snapshot from a real Worker as a different-publisher publish and refuses it, closing the restore-clobber window. The extracted backup is validated through the integrity gate: a corrupt backup is refused with `IntegrityCheckError` (carrying the `WorkingSetReport`); `current/` is left untouched. Quarantining the backup is an application-layer concern — the caller receives the report and decides what to do. `force=True` (the `?force=true` API override, operator-only) publishes despite corruption and returns the `WorkingSetReport` so the caller can quarantine and audit. An archive whose bytes do not read back as a `tar.gz` at all — truncated, or damaged gzip/tar framing — raises `ArchiveUnreadableError` whatever `force` says, with `current/` untouched (Section 7.3, `UNREADABLE` vs. `QUARANTINED`). A restore **bypasses the missing-region gate (Section 4.5) by design**: that gate diffs a staged set against the prior `current/` to catch a Worker accidentally dropping live regions, but a backup is a **complete, self-consistent set captured as a whole** — it is the authoritative replacement, not an incremental delta over `current/`, so a backup that legitimately holds fewer regions than the current world (an older/smaller world being restored) is exactly the intended operation and must not be refused. The structural `.mca` integrity gate applies regardless (a backup cannot be *internally* corrupt); only the prior-set partial-loss comparison is skipped. **Restore marker↔flip window:** unlike `commit_snapshot`, the generation/publisher marker is written *before* the `current` flip (Section 2 generation marker), so a failure between the two leaves the prior world live at the advanced generation with the `api-restore` publisher: holders of the old generation hydrate it again and an in-flight stale snapshot is refused. The accepted cost: a stopped Worker's unpublished scratch progress is displaced and lost by that hydrate on its next start. The per-server lock serializes the window so no concurrent publish can interleave. A failure *after* the flip — the fs parent-dir fsync, an object pointer write the store applied but whose response was lost — leaves the restored world live at the advanced generation and raises `RestorePartiallyPublishedError` carrying the `WorkingSetReport` and the failure as its `cause`. The caller treats it as a published restore whose later step failed: it quarantines a forced corrupt backup, runs its post-restore steps over the live working set, and audits a forced corrupt restore as published (#3564). On fs a replace of `current` that returned is a known flip, so a failure after it is reported as published with no re-read. A replace that raised may still have renamed (a remote filesystem such as NFS can apply a rename and report it failed), so one read of the link settles it: naming the restored snapshot, it published; naming the prior one, it did not and the failure is re-raised; anything else, or a failed read, is `RestorePublicationUnknownError`. On the object backend a failed pointer write is confirmed by re-reading the pointer (up to 3 reads with a short backoff); when no read answers, `RestorePublicationUnknownError` (same fields) says the restore may have published, and the caller handles it the same way. The copied snapshot is never deleted once its pointer write was attempted. On both backends a failed staging cleanup (on fs, its path probe can itself raise on a failing mount) is logged rather than raised over the outcome on its way out, and the sweep reclaims what is left. |
| `delete_backup(community_id, server_id, BackupKey)` | Remove a backup archive | Idempotent. |
| `open_backup(community_id, server_id, BackupKey, byte_range=None) -> ReadStream` | Stream a stored archive (or one byte range of it) in its native format | Download: yields the archive bytes **verbatim** — the adapter-internal `tar.gz` (Section 2), no recompression. `NotFoundError` for an unknown key. `byte_range` is an **inclusive** `(first, last)` pair the caller has already resolved against `backup_size`, and the stream then yields exactly `last - first + 1` bytes — how a download interrupted mid-transfer resumes. It is a real ranged read (an object-store ranged GET; a filesystem seek), never a prefix discarded off the full stream: serving the tail of a multi-GB archive must not re-read its head. |
| `put_backup(community_id, server_id, WriteStream) -> BackupKey` | Store an uploaded archive verbatim under a fresh key | Upload: the **application** has already validated the archive (opens, reads to its gzip end, and every member passes the one archive member rule below) before this is called; `Storage` only stores the bytes, so the new backup is restorable through `restore_backup` like a created one. One exception until issue #3828: the upload gate does not yet read member headers the way restore does, so an archive holding a GNU sparse map that cannot describe a file (issue #3802) passes the upload and is then refused by `restore_backup` and `check_backup_health` as `ArchiveUnreadableError`. |
| `backup_size(community_id, server_id, BackupKey) -> int` | Report a stored archive's byte count | The size recorded as `size_bytes` at create/upload. `NotFoundError` for an unknown key. |

**One archive member rule.** Both backends judge a backup archive's members by
one rule, shared code in both adapters (`refuse_unrestorable_member`), wherever
they read them: in `restore_backup` and in the `check_backup_health` probe. So
restore and the probe reach the same verdict for an archive on either backend
(issue #3550). The upload gate judges an uploaded archive's members by the same
code (`archive_member.unrestorable_reason`) before it is stored, so an archive
restore would refuse is a `422 invalid_archive` at upload, not a backup that
fails later (issue #3579). The gate also refuses a `..` between backslashes
(`world\..\level.dat`), a separator only on Windows; restore reads `\` as an
ordinary name character. The gate, restore and the probe all read the archive
whole (issues #3778, #3798, `archive_member.EndCheckedReader`): the tar is
decompressed by our own gzip reader (`archive_member.GzipReader`, issue #3835),
the member walk must stop at the end-of-archive marker, only zeros may follow
it, every gzip member's header, CRC32 and ISIZE must check, only another gzip
member or zero padding may follow a gzip member, and the stream may hold at
most 10,000 gzip members. So all three refuse a header past the first that does
not read, which `tarfile` takes for the end and so would cut the member list
short, a missing or cut marker, a second tar after the first, bytes after the
gzip stream, a gzip member header whose CRC does not match or that sets a
reserved flag bit (issue #3847), and more than 10,000 gzip members, and all
three read every member of a `tar.gz` of several gzip members.
The walk is forward only: a member whose data runs on past its last 512-byte
block, such as a GNU sparse map that takes more data than the member stores,
makes `tarfile` seek back to the next header, and all three refuse that seek. A
read that stays within the block's padding seeks back to no header and is still
accepted (issue #3828). Restore and the probe
read every member's data, a left-out reserved member's (below) included, and
count it and the zeros past the marker toward the decompressed-size cap, so a
backup whose member data ends within about 10 KiB of the cap (`tarfile`'s
record padding) is refused too. The gate's cap counts every byte it
decompresses, headers, extended-header data and skipped member data included
(issue #3810, `archive_member.CappedReader`). The gate also holds its walk to
header limits (`EndCheckedReader`'s `header_limits`): it refuses more than
1 MiB of headers before a member, extended headers included, since `tarfile`
holds those whole, and members that hold more than 32 PAX records each or 8 Mi
characters of names, link names and PAX records in all, since `tarfile` keeps
every member with its names and a copy of the global records; the refusal's
detail names the limit. GNU tar writes one PAX record a user xattr, so a member
with more than about 30 xattrs is refused; real server data carries a handful
at most. A sparse member's map is not bounded yet (issue #3869). Restore, the
probe and the snapshot ingest do not apply these limits: they have no entry
cap, and a large working set would reach the character limit (131,072 files
with 64-character names do). The gate also refuses a chain of extended headers
too long for `tarfile`, which reads them recursively: a few hundred before one
member. The gate runs off the event loop thread
(issue #3801), on a pool of two threads that it shares with the files upload's
extract and the server import (issues #3843, #3912), so a slow archive cannot
hold the default executor that the fs backend and the sweep run on. The plugin
upload's manifest read and the resource-pack upload's validation run on that
pool too, but are not admitted, so an admitted upload can wait behind them. The
three archive upload routes admit at most four archive uploads at a time between
them: two validating and two waiting. One past that is `503 upload_busy`, refused before
its body is read into memory. A validating thread still holds the GIL, so the
event loop runs late while it decompresses: with one such thread, a 1 ms loop
tick was measured late by up to 29 ms at the 99th percentile and 100 ms at
most (several threads were not measured). Once an upload's body is read, a
client that disconnects does not cancel it: the validation runs to its end,
keeping its pool thread and its admission slot, and the backup is stored.
Empty gzip members and zero padding after the gzip stream add no decompressed
bytes, so no decompressed-size cap sees them; the gzip reader bounds what they
cost in the gate, restore and the probe instead (issue #3835): at most 10,000
gzip members, and zero padding skipped in bulk rather than one byte at a time.
Each member is
first read without the conventional
tar root prefix that `tar -C dir -cf - .` writes (issue #3574): the root's own `.` /
`./` directory entry is skipped, and exactly one leading `./` is stripped from
every other member, so `./world/level.dat` is judged, restored and checked
against the reserved names below as `world/level.dat`. A member is restorable
only when it is then:

- a regular file or a directory — a hard link, symlink, device, FIFO or other
  special member is refused;
- at a relative name with no `..` component — an absolute name (leading `/`)
  is refused, and so is any `..`, even one that stays inside the tree; and
- at a canonical name, with no empty or `.` segment — `world//level.dat`,
  `world/./level.dat` and `././level.dat` (a second `./` once the root prefix
  is gone) are refused (issue #3574). The one trailing `/` tar writes on a
  directory member is fine.

An archive holding a refused member reads fine, but nothing can be restored
from it, so restore and the probe both raise `ArchiveUnreadableError` whatever
`force` says, with `current/` untouched. The servers seam answers restore with
`409 backup_unreadable`, and the sweep records `UNREADABLE` (Section 7.3). The
platform's own producers never emit such a member: the Worker's pack and the fs
writer name members after real directory entries. An object snapshot can still
hold two kinds of legacy key, and the backup and `final.tar.gz` writers leave
both out (issue #3574):

- the `//.dir` key from a past root `make_dir` bug (issue #1944), whose member
  would be the absolute `/.dir`. Hydrate and the directory listing hide it too.
- a **legacy non-canonical key** — one with an empty or `.` segment, such as
  `world//level.dat`, which snapshot ingest and restore accepted before issue
  #3574. Each one left out is logged as a WARNING naming the key. The directory
  listing skips the empty- or `.`-named entry such a key would make, with the
  same WARNING, so a walk over it (the directory download, the server export)
  ends instead of listing the same level forever. Hydrate still hands it to the
  Worker, whose unpack cleans the path, so the Worker's next snapshot stores it
  at its canonical name.

The fs extraction also keeps `tarfile.data_filter`'s other protections (mode
sanitising and the like) on top of the rule. Snapshot ingest reads members the
same way — root prefix stripped, root entry skipped — and takes only canonical
names, on both backends, so no new non-canonical key is ever stored.

**Backups stored before the read to the end.** Until PR #3800 (issue #3778)
the upload gate read an archive with `tarfile`, which stops at the
end-of-archive marker, so it admitted an archive with no marker, with non-zero
bytes in the rest of the last record, with bytes after the gzip stream, or with
a second `tar.gz` appended. Restore and the probe now refuse all four (issue
#3798): restore raises `ArchiveUnreadableError` whatever `force` says, and the
next health sweep records the backup `UNREADABLE`. The same goes for the damaged
archives the gate admitted: one whose gzip CRC32 or ISIZE does not match, whose
gzip trailer is cut or whose header past the first does not read, all admitted
before PR #3800, and one with a sparse member whose data runs past its last
512-byte block, admitted until issue #3798. This is intended: such an archive
cannot be shown to hold every member it claims, and no producer the platform
runs writes one. Until issue #3835 the gate read gzip with `gzip.GzipFile`, so
it also admitted any number of gzip members, a gzip member header whose CRC
does not match and one that sets a reserved flag bit. Restore and the probe
now refuse all three the same way. No producer the platform runs or tests
writes any of them. A writer that puts at most 64 KiB of tar in each gzip
member, as BGZF does, passes 10,000 members at about 620 MiB of tar. Both backup writers use `tarfile`'s `w:gz`, the Worker's pack closes its
tar writer on every path, and GNU tar, pigz, Go `archive/tar` and Python
`tarfile` archives read whole.

**Backup size impact of plugin/mod JARs.** Backups archive the
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
| `read_file(community_id, server_id, rel_path) -> bytes` | Read one file from `current/` (whole bytes) | `rel_path` is validated against traversal (Section 6). Whole-bytes by design: the small-edit / preview read, and the base64-payload `GET ?path=` route where the bytes *are* the JSON body. A large single-file **download** uses `open_file_stream`. |
| `open_file_stream(community_id, server_id, rel_path) -> ReadStream` | Open a chunked read stream over one file in `current/` | The per-file analogue of `open_hydrate_source`: a large single-file download streams without buffering the whole file in RAM. Same lease contract — the live snapshot is resolved and the active-reader lease taken on the FIRST iteration, released on finish/early-close/error (Section 4.2). `NotFoundError` for a missing file or unpublished snapshot. |
| `list_dir(community_id, server_id, rel_path) -> [entry]` | Browse a directory in `current/` | Same path validation. |
| `write_file(community_id, server_id, rel_path, bytes)` | Edit one file in `current/`, retaining the prior version | Captures the prior content into `versions/` (Section 5) before overwriting. The per-file write is atomic (Section 4.4). Bumps the generation + stamps the `api-edit` sentinel (Section 4.4). |
| `delete_file(community_id, server_id, rel_path)` | Delete one file from `current/`, retaining the prior content | Captures the content into `versions/` (Section 5) **before** removing, so a delete is reversible by rollback exactly like an edit. Missing path → `NotFoundError`. Bumps the generation + stamps the `api-edit` sentinel (Section 4.4). |
| `delete_dir(community_id, server_id, rel_path)` | Recursively delete a directory subtree from `current/` | **No** per-file version capture: file versioning (Section 5) is the fine-grained single-file mechanism, whereas whole-subtree recovery is what backups (Section 3.3) exist for; capturing a version per member of a large subtree would be a storage-amplification bomb. Missing dir → `NotFoundError`. Bumps the generation + stamps the `api-edit` sentinel (Section 4.4). **Not crash-atomic across the subtree:** fs uses `rmtree` (per-member unlinks); object uses a per-object delete loop. A crash mid-op leaves a partially deleted subtree at a stale generation (Section 4.4 accepted gap). |
| `rename_file(community_id, server_id, from_path, to_path)` | Rename/move a single file within `current/` | **No** version capture on either side: a rename does not change the content, so retaining versions would waste storage; the caller's content-addressed cache (plugin JARs) or backups cover recovery. Missing source → `NotFoundError`. Bumps the generation + stamps the `api-edit` sentinel (Section 4.4). Atomic on fs (`rename(2)`); on object backends it is a copy+delete pair — not crash-atomic (Section 4.4). |
| `rename_dir(community_id, server_id, from_path, to_path)` | Rename/move a directory within `current/` | **No** per-file version capture (same reasoning as `delete_dir`). Missing source dir → `NotFoundError`. Bumps the generation + stamps the `api-edit` sentinel (Section 4.4). Atomic on fs (`rename(2)`); on object backends it is a per-object copy+delete loop — not crash-atomic (Section 4.4). |
| `make_dir(community_id, server_id, rel_path)` | Create an (empty) directory in `current/` | Backend-dependent (see note). Idempotent. **Requires a published snapshot** — a never-snapshotted server has no live `current/` to create the directory under, so `make_dir` raises `NotFoundError` on both adapters. Bumps the generation + stamps the `api-edit` sentinel (Section 4.4), uniformly with the other edits. On object backends it also writes a zero-byte `.dir` marker object under the prefix so the otherwise-empty directory is visible in listings (see note). |

**Empty-directory representation (`make_dir`).** fs / remote-fs materialize a real
empty directory, which rides the hydrate tar as a directory member (the tar is
built recursively, so empty dirs survive a snapshot round-trip). **Object storage
has no real directories** — a directory exists only as the shared key-prefix of
its files (Section 7.3), so an *empty* directory has no key to make it visible. To
give it one, `make_dir` writes a zero-byte `.dir` marker object under the directory
prefix, so the otherwise-empty directory shows up in listings. `list_dir`
filters the `.dir` marker out of its entries (`_entries_at_level`), so the API
never surfaces it as a file. The marker is a real object, though: it rides the
hydrate tar to the Worker (a literal `foo/.dir` file appears in the live working
directory), is re-packed into the next snapshot, and is carried into
`create_backup_from_current`/restore — so on object backends the empty directory
persists as a `.dir` marker throughout the working-set lifecycle rather than as a
true directory entry. On both backends `make_dir` bumps the generation marker
(Section 4.4), so the store generation stays in lockstep across adapters.

**The Worker's generation-marker name is reserved at the root.** The Worker
stamps `.mcsd_generation` in the working-set root on every hydrate and treats
every root entry whose name starts with `.mcsd_generation` as its own: a
snapshot leaves such entries out, and a hydrate does not unpack them. So no
operator entry may be created there, and the application refuses one above this
Port on every door that writes into `current/`: the files-API write (running too,
where it would overwrite the live marker), make-dir, upload and its archive
members, a rename's destination, a rollback (a version retained before the
guard included), a server import (directory-only members too) and a backup
upload — each a
`422` `reserved_name` (issue #3433). Only the root name is reserved; a
same-named entry deeper in the tree is ordinary user data. Reads, deletes and a
rename *away* stay open, so an entry planted before the guard can be cleared
while the server is stopped. Before the Worker skipped these members on
unpack, such an entry as a directory took the marker's name and made every
hydrate, and so every start, fail on the marker write.

Restoring a backup **already stored** — one created or uploaded before the name
was reserved — is not refused for such an entry: refusing would leave that backup
unrestorable. Instead `restore_backup` leaves out every archive member that
lands at, or under, a root name starting `.mcsd_generation` before publication,
on both backends, and logs one `WARNING` naming the backup and how many members
it dropped. The member rule (Section 3.3) is applied first, so an absolute or
`..` spelling such as `/.mcsd_generation/x`, `world/../.mcsd_generation/x` or
`.mcsd_generation/../server.properties` is refused as an unrestorable archive,
not dropped. A `./.mcsd_generation-cache` member is the same member as
`.mcsd_generation-cache` once the tar root prefix is stripped, so it is dropped.
The backup health check judges members the same way, so it judges
exactly what a restore would publish. A nested same-named member is restored as usual. The dropped entries are
inert anyway — the Worker would skip them on unpack — so dropping them only keeps
them out of `current/`.

### 3.5 File version retention / rollback

Serve versioned edits (FR-FILE-3). See Section 5 for the retention scheme.

| Operation | Purpose | Notes |
|---|---|---|
| `list_file_versions(community_id, server_id, rel_path) -> [VersionId]` | List retained prior versions of a file | Ordered newest-first. Versions are storage-only — the ids are enumerated from the backend, not from a DB index (Section 5). |
| `read_file_version(community_id, server_id, rel_path, VersionId) -> bytes` | Read a specific retained version | For preview/diff before rollback. |
| `rollback_file(community_id, server_id, rel_path, VersionId)` | Restore a file to a retained version | Implemented as a `write_file` of the old content, so the pre-rollback content is itself retained (rollback is reversible) and the generation bump + `api-edit` sentinel ride that delegated write. |

---

## 4. Atomic publish

**Requirement (FR-DATA-6).** A snapshot or backup restore must never overwrite
the authoritative copy with a partial transfer. The authoritative `current/`
must, at every instant and after any crash, reflect either the *prior*
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
   what `current` resolves to. Only after it succeeds is the prior snapshot
   reclaimed.

A transfer is "proven complete" before publish by the caller signalling
end-of-stream plus an integrity check (size/manifest match between what the
Worker sent and what landed in staging). The exact completeness signal is part of
the data-plane contract (Section 8); `Storage.commit_snapshot` is the gate that
refuses to publish without it.

An empty staging area is not a publishable transfer: a worker packing an empty
working set is a bug signal, never a valid snapshot. `Storage.commit_snapshot`
refuses it with `IncompleteTransferError`, which the snapshot endpoint surfaces
as `400 empty_snapshot` (Section 8). One exception: a concurrent delete's prune
removes `incoming/` along with the working set, so a staging area found missing
while the store shows the prune signature (generation moved past the guard's base,
no live `current`) is refused as `PrunedStoreError` instead, the same
`409 deleted_during_upload` as the commit re-check. The same holds when the
prune removes the spool, staging directory or staged objects while a lock-free
step still reads them: the fs write's staging check, spool creation and spool
reopen, and the commit's empty check (including a staging the prune has emptied
but not yet removed), integrity gate, fsync or object copy. A missing file or
object without the prune signature stays the error it was, and an empty upload
without it stays `empty_snapshot`. A failed object copy first removes the objects
it already copied into its fresh, never-pointed-at snapshot prefix, so a prune
that ran between two copies leaves nothing behind; if that removal fails too, the
error is logged, the original error still propagates, and the sweep reclaims the
prefix.

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
  never absent and never partial. The prior snapshot is deleted only after
  the flip returns.

  This is chosen over `renameat2(RENAME_EXCHANGE)` (Linux-only; commonly
  unsupported over NFS/SMB) and over deleting `current/` then renaming the new
  copy into place (which leaves a window where `current` does not exist —
  exactly what FR-DATA-6 forbids). A symlink flip needs only atomic same-directory
  rename, which is the guarantee the remote-fs adapter requires of its mount in
  any case (Section 7.2), so it is the portable option across all path-based
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
| After move into `snapshots/`, before the **symlink flip** | `current` still points at the old snapshot; the freshly moved `snapshots/<snapshot-id>/` is not yet referenced, so it is an orphan, cleaned by the startup sweep (it is unreferenced because no symlink targets it, and the crash took the publish's registration with it, below) | (no analogue — object stage writes directly under the new prefix) |
| During the **symlink flip** | same-directory rename is atomic — `current` resolves to either the old or the new snapshot; it is never absent and never a partial pointer | the pointer PUT is atomic — it either references the old or the new prefix |
| After the flip, before prior-snapshot reclaim | `current` points at the new snapshot; the prior `snapshots/<snapshot-id>/` is an orphan (unreferenced by `current`), cleaned on recovery | pointer references the new prefix; old prefix is an orphan, GC'd |

The invariant in every row: **`current` (or the object pointer) always resolves
to one complete snapshot — never absent, never partial.** Recovery is
idempotent: the startup sweep reclaims any `snapshots/<snapshot-id>/` not
targeted by `current` and any leftover `incoming/` staging dir, and re-running
`abort_snapshot` or the sweep is always safe. The sweep runs at API startup and
then on a periodic loop (`storage_sweep.interval_seconds`, daily by default;
CONFIGURATION.md Section 5.15).

**The snapshot a publish is still filling** (#3893). Until its flip, a publish's
new `snapshots/<snapshot-id>/` is named by no pointer, so it looks like an
orphan. On the object backend that lasts for the whole server-side copy, minutes
for a big world, and then for the wait for the per-server lock and the gates
before the flip; on fs, for the move into `snapshots/` plus one directory fsync.
A sweep that reclaimed it then would let the publish flip onto a partial or
missing snapshot and report success. So every whole-working-set publish (a
snapshot commit, a backup restore, and the first file write to a server that has
never published) registers the new snapshot in memory before its first write
into it, and releases it only after the flip or once the publish has failed. The
registration holds for as long as the publish runs, however long that is.

The sweep decides about each candidate snapshot when it is about to delete it, not
from its listing, and it reads the registration before the pointer. A publish
releases its registration only after its flip, so a candidate found unregistered
has either been flipped already, which the pointer read then shows, or been
abandoned. A pass that overlaps a publish therefore never reclaims the snapshot
being filled, whatever the sweep cadence. A hydrate reader's lease is read the
other way round, after the pointer: it is taken on the live snapshot and outlives
the flip that supersedes it.

The registration is in process, like the `incoming/` staging pin, the hydrate
reader leases and the per-server lock (Section 2), so it assumes the single API
instance Section 1 targets: **one API process per store**. A sweep in a second
process would not see it. A crash takes the process's registrations with it, together with the
publishes they belonged to, so the snapshot an interrupted publish was filling is
an ordinary orphan. The startup sweep reclaims it before the restarted API serves,
when no publish can be in flight.

The post-flip reclaim is best-effort on every backend (#3269). On fs, a fault
reclaiming the prior snapshot is logged and the tree is left to the sweep. On
the object backend, a store outage while deleting the staging or prior prefix is
handled the same way. The object restore's local archive spool is removed last,
and a failure to remove it is logged too. Either way the publish or restore
still returns its result, because its content is already live.

For the fs / remote-fs backends, the staged tree's file data is fsynced
(`_fsync_tree`) before the flip and the prior snapshot is reclaimed only
after the flip, so `current` never resolves to a tree whose data blocks are
unflushed while the prior copy is already deleted.

The sweep also reclaims the **spool temp files** a crash leaves at a backup
write site — the fs adapter's `.backup.*.tmp` (create / upload) siblings — and,
on the object backend, the **in-progress multipart upload parts** a crash
mid-`put_backup` (or mid-snapshot-member upload) leaves behind. Multipart parts
never list as objects, so the prefix scan cannot
see them; the object sweep lists them via `ListMultipartUploads` and aborts them
via `AbortMultipartUpload`. It lists them under **every top-level prefix the API
uploads to**, not only the ones the `Storage` adapter keys under, because the
resource-pack and plugin-cache stores upload via multipart into the same bucket
(issue #3277). Those prefixes are `communities/` (snapshot members, restore
members, backups, the post-delete `final.tar.gz`), `jars/` (`put_jar`),
`plugin-cache/` and `resource-packs/`. The object adapter's `KeyNamespace` names
them once: every store builds its keys from it and the sweep walks all of it, so
a prefix added there is swept with no further change. Both reclaim paths apply
an **mtime / `Initiated` age threshold** (1 h): a spool or upload younger than
the threshold may belong to a live write still in flight, so it is left alone —
mirroring the `incoming/` lease-guard discipline and keeping the periodic
passes as safe as the startup one (the long-upload caveat is in Section 9.5).

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

The fs prune's `.final.*.tmp` siblings are not the sweep's: every prune removes
them when it starts, regardless of age. Only a prune writes them, and it holds
the in-process server lock for its whole run, so none can belong to a live
prune.

**Resource-pack orphans** (issue #3277). A stored resource pack whose
`resource_packs` row is gone is never served, since the row is the source of
truth, but the multipart sweep cannot see it either: it is a complete object.
Two use cases can leave one behind. Each orders its two writes so that a
failure between them leaves an unserved blob rather than a row whose blob is
gone:

- `DeleteResourcePack` commits the row's delete before it removes the blob
  (#1962). A store outage on the blob delete is logged, the route answers 204,
  and a repeated DELETE finds no row and never reaches the store.
- `UploadResourcePack` stores the blob before it commits the row (#2458). A
  failed commit leaves the blob with no row, and so does an upload the store
  completed but whose response was lost.

A **resource-pack GC** reclaims them. It is a servers-side use case, not part
of `sweep`, because it reads the pack rows and the `Storage` adapter has no
database. It mirrors the plugin-cache GC:

1. List every blob under `resource-packs/`, with the pack id its key names and
   its store time. A key not shaped `<pack-id>/<filename>` names no pack and is
   left alone.
2. Read the pack rows. The read follows the listing, so a row committed by then
   is seen. A blob whose pack has a row is live.
3. Spare any blob younger than a **1 h safety window**. An upload in flight has
   stored its blob and not yet committed its row, which is exactly an orphan's
   state, told apart only by age. The window covers the whole upload as well as
   the commit, since a store may stamp a multipart object with the time the
   upload began. That rests on throughput, not on a bound: a pack goes up as one
   part, and below about 75 KB/s a 256 MiB pack takes longer than an hour. Such
   an upload is left to the re-read in step 4 and its residual race.
4. Re-read that pack's row immediately before deleting its blobs, and keep them
   if the row is there. This catches a row committed after the read in step 2
   (the plugin-cache GC's #1404 pattern). It narrows the race to the gap between
   the re-read and the delete without closing it. Losing that race would take an
   upload whose commit lands more than the safety window after its blob was
   stored.

It logs every pack it reclaims and skips a pack whose delete fails, as the
JAR-pool GC does (Section 3.2, "Record and failures").

It runs as an API lifespan task on the storage sweep's cadence
(`storage_sweep.interval_seconds`, CONFIGURATION.md Section 5.15). Its first
pass runs after a random delay, uniform between 0 and
min(`storage_sweep.interval_seconds`, 600) seconds from startup (CONFIGURATION.md
Section 5.7). It is gated on the resource pack store
existing, not on the control plane. On fs / remote-fs there is no resource pack
store, so no blob to reclaim, and the GC does not run.

**Store ownership** (issue #3870). A store (a bucket, or an fs root) must belong
to exactly one database. The JAR-pool, plugin-cache and resource-pack GCs delete
what no row of *this* database names, and the sweep deletes staging and multipart
uploads that no lease in *this* process holds. Pointed at a store that another
database also uses, they would delete that database's live objects. Examples are
a probe or dev stack that copied the live object-store settings, and two
deployments sharing one bucket. For a resource pack, the deleted object is the
only copy of a user upload.

So the store records which database owns it. Each database holds an
installation id, minted once by migration and never changed (DATABASE.md
Section 1.1). The **owner marker** is `mcsd-owner.json`: an object at the bucket
root on the object backend, or a file at `<root>/` on fs and remote-fs. It holds
`{"installation_id": ..., "created_at": ...}` and sits outside every namespace
a GC or sweep lists. At startup, before the startup sweep, the API claims its
store:

- **No marker:** the API writes one naming its installation only on evidence
  that the store is this database's (issue #3886). Claiming another database's
  store would make every one of its objects an orphan. The evidence comes from a
  sample of the namespaces a GC or sweep lists, and the store is claimed if:
  - **the store is empty.** No namespace holds an object, so a fresh deployment
    claims its new store. An in-progress multipart upload is not an object: a
    store holding only uploads in flight counts as empty, and once it is claimed,
    the sweep aborts them when they are an hour old.
  - **the ids in its keys are rows of this database.** A key under
    `communities/<id>/` names a community, and one under `resource-packs/<id>/`
    a resource pack. The API samples up to three distinct ids of each, the lowest
    first, and claims the store only if every one is a `community` or
    `resource_packs` row. A key whose segment is not an id refuses too. An
    existing deployment therefore claims its store at the first start after the
    upgrade, and a probe database with communities of its own does not claim the
    live store.
  - **it holds only content-addressed objects, and this database names stored
    objects.** `jars/` and `plugin-cache/` name content, not rows, so there is no
    id to match. The API then claims the store if this database has a `server` or
    a `resource_packs` row; every other row that names a stored object (a backup,
    a plugin, a pooled JAR) hangs off a server. A fresh database does not claim
    it.

  On the object store, the sample is one listing per id (one per content
  namespace), each read at the store's default page size until it finds a key,
  following a page that is truncated but holds none. SeaweedFS keeps the
  directories of deleted keys, and 4.33 reads only `MaxKeys + 2` entries of a
  directory, so a one-key listing reported a store holding a live object as
  empty. On fs, the sample is up to three entries each of `communities/` and
  `jars/`; there are no resource packs on fs, and a directory left empty by
  deletes counts, which errs on the side of refusing.

  The write is conditional, so when two APIs start together on an unmarked store,
  exactly one claims it. On the object store it is a PUT with `If-None-Match: *`,
  which SeaweedFS honors. On fs, a fully written temporary file is hard-linked
  into place. A store that rejects the conditional PUT gets a plain PUT; the
  marker is then read back, and the read before each pass catches a lost race.

  Without that evidence the API does not claim the store. It logs one ERROR that
  names the store, the ids or the rule that failed and the claim command below,
  and every destructive pass refuses as unclaimed.
- **A marker naming this installation:** the store is this database's.
- **A marker naming another installation, or none it can read:** the store is
  not this database's. The API logs an ERROR at startup and keeps the marker.

The sample and the database's answers are taken only at startup, and only when
the store has no marker. The claim reads the database and writes the store, so
the API fails to start if either is unreachable.

**The claim command.** An operator claims a store the API left unclaimed with

```sh
docker compose exec api python -m mc_server_dashboard_api.store_claim_cli
```

(`cd api && uv run python -m mc_server_dashboard_api.store_claim_cli` outside
compose; it reads the same `MCD_API_*` settings as the API). It writes the marker
naming this database's installation, whatever the store holds, so run it only
for the database that owns the store: the API's GCs then reclaim every object in
it that no row of this database names. It refuses when a marker already exists,
whichever installation it names, and it deletes nothing. On a store without
conditional writes it reads the marker back and reports it when another
claimant's write landed last. It exits 0 when it claimed the store, 1 when it
refused, and 2, with one line, when the configuration, the database or the store
failed. Restart the API afterwards: an API that left the store unclaimed at
startup claims it only at its next start. A legitimate deployment needs the
command once when the sample does not show its store to be its own: its store
still holds the objects of a deleted community (the `final.tar.gz` of its
servers), or its database has no server and no resource pack while the store
still holds pooled JARs.

Before **every** pass, each destructive GC and sweep reads the marker again. It
runs only if the marker names this installation. This covers the storage sweep
(at startup and on its loop, both backends), the JAR-pool GC (its loop, and
`POST /versions/jar-pool/gc`, which answers `409 store_not_owned` with the
reason as `detail`), the plugin-cache GC and the resource-pack GC. A refused pass
deletes nothing and logs an ERROR on every pass, naming the marker and both
installation ids. Everything
else continues: serving, uploads, publishes, backups. Readiness (`/readyz`) is
not affected either, because the data path still works; a refused pass only
stops reclaiming space and orphaned staging, which the ERROR reports. A marker
removed while the API runs is not written again until the next start or the
claim command, and the passes refuse until a marker names this installation
again; an API that claimed the store at startup then resumes at its next pass.
An API that left the store unclaimed at startup keeps refusing until it
restarts. The passes that do not diff the store against the
database are not gated: the backup retention prune and the post-delete prune
remove the objects of rows this database holds, by their ids.

The API reads its installation id once, at startup, and compares every pass
against that. **Stop the API before replacing its database**: an API left running
while its database is swapped for another (a restore, a clone, another
installation's) keeps the old id, still matches the marker, and runs its GCs
against the new rows.

Cases the marker does not cover:

- **A database restored to an earlier point** keeps its installation id, so its
  GCs run. Objects the store gained after the restore point have no row in the
  restored database. The safety windows (1 h) spare only the newest of them, and
  the re-read before each delete only catches a row committed meanwhile, so a pass
  reclaims every such object stored more than an hour before it runs. Restore the
  store from the same point, or copy out what it gained since, before the restored
  database's API starts.
- **A database cloned from a dump** keeps the installation id too, so two
  databases now carry the same one. An API on the clone (a probe or staging
  stack seeded from a live dump) matches the live store's marker, and its GCs
  run against the live objects with rows that are stale or diverging. A clone
  must therefore never be pointed at the live store. A dump taken before
  `0045_installation` is the opposite case: upgrading it mints a new id, which
  the existing marker does not name, so its passes refuse until the reset below.
- **The claim of an unmarked store rests on a sample**: at the first start
  after the upgrade that introduced the marker, and again after the marker is
  deleted or lost. The API sees up to three communities and three resource packs
  (issue #3886), not every object. A stack with a database of its own claims
  another database's unmarked store when every id the claim samples is one of
  its own: its uploads to that store sort lowest and fill the sample, or the
  store holds only JARs and plugin blobs and its database has a server or a
  resource pack. A clone carries the live ids, so it matches them. The API that
  claims becomes the owner: its startup sweep runs at once, and its GCs run on
  their intervals against every object its database does not name, which on a
  live store is the live data. The live API's passes refuse, but that does not
  protect the live objects. A probe stack must therefore never point at a live
  store.
- **The owner is not the only writer.** The marker stops a foreign API's GCs, not
  its writes, and not the owner's GCs: the owner reclaims a foreign API's uploads
  as orphans.

To hand a store to another database, or to settle a refusal: stop every API that
uses the store, delete `mcsd-owner.json`, and start the API whose database should
own it. It claims the store at startup if the sample shows it to be its own;
otherwise run the claim command above and restart it. Its next passes reclaim
everything in the store that its database does not name. A downgrade of
migration `0045_installation` followed by an upgrade mints a new installation
id, which the marker no longer names, and so does upgrading a dump taken before
it; the passes then refuse until this reset.

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
`rollback_file`) and stamps the **`api-edit` sentinel** as the publisher
(Section 3.1). Otherwise a same-Worker scratch with `held == store` would skip the
post-edit hydrate on the next start and boot the PRE-edit world, and that
scratch's in-flight stale snapshot — the same Worker, `base == current` — would
pass the publish-time guard (Section 8) and clobber the edit; the sentinel makes it
a different-publisher publish whose base now lags, so the guard refuses it. The bump
is uniform across all five ops for a simple invariant: even `make_dir` (which only
creates an empty directory — a real one on `fs`, a zero-byte `.dir` marker on
object backends) bumps, so the staleness reasoning never special-cases
which edit "really" changed the world.

A single-file `write_file` and a whole-working-set publish/restore are never
issued concurrently for the same server: they are serialized at the application
layer per the state-branching policy (REQUIREMENTS.md Section 6.9, Section 9
decision 3: Storage file edits happen only on a stopped server, while publish
happens for a running server's snapshot or during restore, which requires a
stop). The Storage adapter
itself does not arbitrate concurrent publish and `write_file` on the same server;
the application layer is responsible for not issuing them concurrently.

**Accepted gap: multi-member edits are not crash-atomic.**
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

### 4.5 Recovering from a refused `working_set_incomplete` publish

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
silently-holed world. Only a snapshot publish is gated: a backup restore is an
operator-chosen rollback to a complete set, which legitimately holds fewer
regions than the world it replaces, so it skips the gate on both backends
(Section 3.3 `restore_backup` row). There are two legitimate causes and one
corruption cause:

- **Intentional world shrink (delete a whole dimension).** Deleting *all* region
  files of a dir is a full-dimension delete and is **allowed** — only a partial
  loss is refused. So the documented override for an intentional shrink is to
  delete the *whole* dimension dir, not a subset of its regions.
- **Intentional removal of specific regions.** If the operator genuinely wants
  `current/` to drop those regions, delete the listed names from
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

### 4.6 Worker `.displaced-<id>` trees: lifecycle, operator recovery, and cleanup

#### What creates a `.displaced-<id>` tree

When a server's final stop snapshot **definitively fails** — refused by the
integrity gate (Section 8), refused by the missing-region gate (Section 4.5), or
otherwise non-transiently rejected — the Worker retains its scratch dir so the
only copy of the world survives. On the **next start**, the `HydrateTrigger` does
not overwrite that scratch dir with the authoritative snapshot; it **renames it
aside**: the old scratch `<scratch>/<id>` is moved to `<scratch>/.displaced-<id>`
before the new working set is unpacked in its place. The displaced tree is the
retained-for-recovery copy of the world as it stood before the failed final
snapshot.

**Location on the Worker.** `<worker.scratch_dir>/.displaced-<server-id>` — a
dot-prefixed sibling of the server's normal scratch dir. Only one displaced tree
exists per server at any time; a hydrate that finds the slot already occupied **keeps
the existing tree** and discards the working set it just displaced (oldest-wins,
below).

**Which copy wins: oldest-wins.** A hydrate that displaces a live working set
while a `.displaced-<id>` is already present must choose between two trees, and
**neither is guaranteed to be in the store**: a surviving `.displaced-<id>` almost always
means that tree was never published *from this Worker* (any snapshot succeeding here calls
`sweepDisplaced`), and the live set may itself be torn or simply un-snapshotted. The
Worker retains the **first-displaced** tree and drops the newer one.

The "almost" covers a class, not a single case: a snapshot whose **sweep did not remove
the tree**. Three deliberate routes reach it, each described in full under "when it
declines", below:

- the running-id branch declines before the sweep starts, because the working directory is
  no longer provably the one that snapshot packed (issue #2291);
- the sweep renames the tree out of the slot, re-checks that same identity, finds the
  working directory replaced and **puts the tree back** (issue #3118);
- another sweep for the same id already holds that id's slot claim, so this one declines
  **without renaming** (issue #3118).

A survivor from any of the three may hold a published prefix plus the delta since that
pack — which is why oldest-wins keeps it rather than reading a successful snapshot as
proof the tree is redundant.

Note the scope: `sweepDisplaced` only walks *this* Worker's scratch dir, so a snapshot
that succeeded for the same server on **another** Worker (an A→B→A re-placement) does not
reclaim this tree. A retained tree can therefore be arbitrarily old even while the server
snapshotted successfully elsewhere — what remains true is that a success on B publishes
B's working set, never this tree.

The case this gets right is the torn-world path: a torn `<scratch>/<id>` fails the
boot region fsck, the Worker rewrites its generation marker to 0 so every registration
advertises generation 0 (CONTROL_PLANE.md Section 4.1), and the API's skip gate dispatches
a hydrate — and retaining the older *intact* tree is better than retaining the torn one.
The cost is real and goes the other way when the newer tree is fine: the discarded set is the store
copy **plus everything Minecraft wrote since**, none of which was published either, so it
can be strictly newer than the tree retained. (Concretely: a server whose snapshots keep
being refused by the integrity gate runs for days, an operator's `restore_backup` bumps
the store generation so the skip gate stops skipping, and the resulting hydrate discards
those days.) Because that loss is otherwise invisible, the Worker emits a `WARN` naming
**both** paths at the moment it decides:

```
WARN  hydrate: an older displaced recovery tree already exists; keeping it and discarding the working set this hydrate replaces (oldest-wins, issue #2278; see STORAGE.md Section 4.6)  server_id=<id>  retained=<scratch>/.displaced-<id>  retained_mtime=<ts>  discarded=<scratch>/<id>
```

Treat that line as an operator signal: the retained tree is the one to recover from, and
the discarded one is gone once the hydrate's swap-in succeeds — unless a concurrent sweep
emptied the slot first, in which case the hydrate keeps the set in the slot and says so in
an `INFO` line ("when it declines", below). "Occupied" means the slot
holds an actual working set — at least one entry that is not Worker-private generation
state. A regular file, a symlink, an empty directory, or a directory holding only
`.mcsd_generation` (or a crashed write's `.mcsd_generation-XXXX` temp sibling) is cleared
and the ordinary displace path runs, so world-less junk in the slot can never shadow a
real world. That is the same "holds a working set" test the held-server scan applies.

The policy is deliberately **not** health-aware — the Worker does not fsck both trees to
pick the better one, because that puts a region scan on the hydrate
path and makes the rule unpredictable under partial failures.

**Lifecycle.** The displaced tree is **never GC'd automatically on server delete**
— it is the last surviving copy of the world exactly when it is most needed. It is
reclaimed only when a subsequent snapshot **succeeds** for the same server id
(`sweepDisplaced`, called from both the running-id and stopped-id
snapshot-success branches — the running-id branch reclaims only while the working
directory is still the tree it packed, and either branch declines while another sweep for
the id holds that id's slot claim, "when it declines" below): that success proves
the store holds a state newer than the displaced world, making the local copy
redundant. The sweep first **renames** the tree out of the slot, to
`<scratch>/.sweeping-<id>-<nonce>`, and only then removes it. Removing it in place would
take seconds for a world-sized tree, and a hydrate landing inside that traversal would
read the half-deleted tree as an occupied slot, keep it under oldest-wins and drop the live
set it displaced. The rename empties the slot atomically, so a hydrate finds either the whole
tree or nothing. A `.sweeping-<id>-*` directory is garbage by the time the next boot reads
it — either the sweep had decided to delete it and did not finish, or it withdrew that
decision and had nowhere to put the tree back. The cases where such a tree was still worth
something, and why none of them can be told apart at boot, are under "when it declines",
below. If the Worker crashed or the
removal failed, the tree stays under that name until the next Worker boot, which removes
every `.sweeping-*` tree — and, by the sibling pass beside it, every `.hydrate-*` tree —
before it scans the held servers. Both passes run only when that boot's **container orphan
sweep succeeded**: a boot that cannot prove no container is still running leaves the trees
alone, because an unswept orphan may still be writing into one (below). A server deleted after
a failed final
snapshot never snapshots again and its displaced tree therefore **persists on the
Worker indefinitely** — bounded to one working-set worth of disk per deleted
server. Note: the deleted-server scratch reclaim (`ReclaimDeletedScratches`)
intentionally does **not** reclaim `.displaced-<id>` trees — only the scratch dir
and `.hydrate-<id>-*` leftovers.

A `.hydrate-<id>-*` tree is the opposite case, and its lifecycle differs accordingly: it
is either the temp tree a hydrate unpacks the store copy into or the working set
oldest-wins elected to **drop**, never the copy worth keeping (a hydrate parks that
directly at `.displaced-<id>` for exactly this reason), so every sweep deletes one
unconditionally. Three do it per id — the next hydrate for that server, the
post-final-snapshot scratch GC and the deleted-server reclaim — and each of the last two
sweeps the leftovers **before** removing `<scratch>/<id>`, because that directory is what
keeps the id in `held_servers` and therefore what makes any later per-id pass reachable at
all. For an id that never comes back (deleted, or re-placed onto another Worker) none of
the three runs again, so the Worker also removes every `.hydrate-*` tree at boot, in the
same pass position as the `.sweeping-*` reclaim above: after the container orphan sweep,
before the held-server scan.

Both boot passes are **gated on that sweep having succeeded**, and the reason is a tree
that is not garbage at all. A sweep that failed at an earlier boot leaves an orphan
container running with `<scratch>/<id>` bind-mounted, and nothing re-adopts containers, so
the Worker does not know it exists: a hydrate for that id then parks the *live* tree aside
— at `.hydrate-<id>-superseded-*` when the `.displaced-<id>` slot is occupied, otherwise at
`.displaced-<id>` and from there to `.sweeping-<id>-*` at the next successful snapshot's
sweep — while the orphan's mount follows the inode and keeps writing into it. Deleting that
at the next boot would destroy a live world, and the server's open descriptors would go on
writing into unlinked inodes. So a boot whose sweep failed logs a `WARN` and defers both
reclaims; the trees wait for a boot whose sweep succeeds. The sweep itself stays
**non-fatal** on purpose — a transient Docker socket failure must not stop the Worker from
serving every other server — which is exactly why the reclaims check it rather than assume
it. The same signal gates one more boot step, for the same reason applied to reading rather
than deleting: the held-server scan advertises the recorded generations **unjudged** (and
rewrites no marker) when the sweep did not succeed, because its region fsck would otherwise
read a live orphan's world mid-write and the generation 0 that follows dispatches a hydrate over it
(CONTROL_PLANE.md Section 4.1).

**Scratch capacity.** One hydrate that displaces a live working set peaks at **three
world-sized copies of that server**: the unpacked temp tree, the retained
`.displaced-<id>` (left untouched), and the live set parked aside until the swap-in
succeeds. The retained displaced tree is one of the three, not a fourth term.
Hydrates for distinct servers can be at that peak simultaneously — the Worker runs up to
`maxConcurrentLanes` (4) command lanes concurrently — so budget `worker.scratch_dir` as
**3× the largest world × the number of overlapping hydrates** (4 in the worst case), plus
one world-sized copy per retained `.displaced-<id>` belonging to a server that is *not*
currently hydrating, plus the ordinary scratch dirs of the servers this Worker holds.
Running out of space is not data loss: the unpack, the generation-marker write and the
fsync all complete before the first rename, so an `ENOSPC` fails the hydrate with the live
working set (and any existing displaced tree) untouched and the transfer is simply
retried.

**An unreadable slot blocks hydrates for that server until it is cleared.** Because the
slot decides which world survives, a hydrate that cannot *read* `.displaced-<id>` (a
permission problem on the directory, a descriptor exhaustion, an I/O error) fails rather
than guessing: guessing "occupied" would discard the live working set, and guessing "junk"
would delete a recovery tree. The failed hydrate deletes and discards nothing — the live
set stays at `<scratch>/<id>` and the slot is left byte-identical — and it surfaces the
same way an `ENOSPC` hydrate does (`transfer_failed`). Unlike `ENOSPC`, though, it does
**not** clear on retry: every subsequent hydrate for that server hits the same slot and
fails the same way, so the server cannot start until an operator intervenes. If a server
persistently fails to hydrate with a permission/IO error, inspect
`<worker.scratch_dir>/.displaced-<id>`: recover it if it holds a world (procedure below),
then fix its permissions or remove it.

**Boot detection.** At Worker boot, after the held-server scan,
`WarnOrphanDisplacedTrees` logs a `WARN` for each `.displaced-<id>` tree whose
server id is **not** in the held-server set. It skips a `.displaced-*` directory whose
suffix is not a canonical server id (CONTROL_PLANE.md Section 5): the Worker never
creates one, so it is not a recovery tree. The tree it names is the
**first-retained** branch, which can be frozen at a displacement much older than the last
world this Worker held — read its mtime, not its existence, when judging how current it
is. A tree is "assigned" (not logged) if
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
   same layout the Worker normally holds in `<scratch>/<id>`). A tree displaced because
   the boot region fsck judged it **torn** carries a `.mcsd_generation` marker of `0`:
   the Worker rewrote it so the verdict reached the API, and the generation the tree was
   actually at survives only as `recorded_generation` in that boot's `held set has a
   corrupt region` `WARN` for the server id. Grep the Worker log for it before judging
   how old the tree is. `level.dat` and
   region dirs live under `world/` inside the displaced tree (e.g.
   `.displaced-<id>/world/level.dat`, `.displaced-<id>/world/region/`). Inspect
   those paths to confirm the world data looks intact. **Check its age.** Under
   oldest-wins this is the EARLIEST retained local branch, not the latest: later
   hydrates may have displaced and discarded newer working sets while this tree sat in the
   slot. Each such discard logged the `WARN` above, so grep the Worker log for
   `discarding the working set` on this server id to see whether newer branches existed.
   If a `swept by a concurrent snapshot` `INFO` follows that `WARN` (see "when it
   declines", below), the slot holds the set that hydrate displaced, not an older tree.
3. **Recover the world.** Two options:

   - **Repack as a new backup (recommended).** Tar the displaced tree, upload it
     as a backup to a server (the upload-backup flow, `POST
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
   `sweepHydrateLeftovers`, snapshot-spool and boot-time `.sweeping-*` / `.hydrate-*`
   sweeps only target their own prefixes); only `sweepDisplaced` removes it, and only on
   a successful snapshot for the matching id.

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

#### When a snapshot sweeps a displaced tree — and when it declines

Both snapshot-success branches call `sweepDisplaced`, and the checks that make them
decline deliberately differ. The identity pin below is the **running-id** branch's alone —
the stopped-id branch holds the per-id reservation instead, so no hydrate can be racing it
— while the id's slot claim further down is taken by **both**, and either branch declines
when it finds that claim already held. Those are the checks; a sweep is best-effort at
every step besides, so a tree also survives a sweep that simply failed.

A running-id periodic snapshot for server id S succeeds and calls `sweepDisplaced`
even if S had a stop→failed-final→re-place→hydrate sequence in between: the
new scratch for S (delivered by the hydrate) is the current authoritative world,
so sweeping `.displaced-S` at that snapshot is correct — the displaced tree is
not the only copy any more. The integrity checks (Section 8) gate the publish, so a
genuinely torn new scratch is refused and the displaced tree is retained until a
clean snapshot lands. This sequence is a micro-edge handled by construction: the recovery
insurance (`sweepDisplaced` only on success) and the integrity gate together keep
the displaced tree alive exactly as long as it is needed.

A second, narrower sequence is why the running-id branch pins the working directory's
identity before it sweeps. A running-id snapshot takes no per-server reservation, so the
success that triggers `sweepDisplaced` for S may belong to an **older, already-dropped Worker stream** rather
than to the stream that currently owns S — and the tree it would remove is then one a NEW
stream's re-placement hydrate has just created. The exposed window is small (the dropped
stream's upload is cancelled with its stream, so only the brief post-upload tail can still
sweep), but the tree removed there is not redundant: it holds the published state **plus**
whatever the world progressed since that snapshot's *pack* — auto-save resumes before the
upload, and the racing stop's shutdown save lands after it. That bound assumes the racing
hydrate *created* the tree; under oldest-wins a hydrate that finds the slot
occupied creates none, so what such a sweep removes is instead the older retained tree,
which the bound does not describe at all. The Worker therefore **declines the sweep**
whenever the working directory is not the tree the snapshot packed, using the same
identity pin that guards the snapshot's generation stamp (CONTROL_PLANE.md
Section 4.1). The publish still succeeds — only the GC is skipped, with an `INFO` line
naming the reason:

```
INFO  skipped sweeping the displaced recovery tree: the working dir is no longer the directory this snapshot packed  server_id=<id>  reason=working_dir_replaced
```

`reason` is one of three values, and each one sends you to a different place (the same
three reasons appear on the second check below):

- `working_dir_replaced`: a different directory now sits at `<scratch>/<id>`. This is
  the concurrent re-placement hydrate described above.
- `working_dir_absent`: nothing is at `<scratch>/<id>` any more, or it was already gone
  when the snapshot pinned it.
- `identity_unavailable`: the Worker could not establish the directory's identity (a
  descriptor or permission failure while pinning or re-checking it), so it cannot prove
  the tree is unchanged and declines the sweep.

**The tradeoff.** The GC occasionally declines to reclaim disk: a `.displaced-<id>` tree
can survive a snapshot that succeeded, and it is then reclaimed only by the next successful
snapshot for that id — the ordinary GC-on-success contract, so a server that keeps
snapshotting reclaims it on the following tick. A server that stops snapshotting right
after such a skip keeps the tree until an operator removes it by hand (step 4 of the
recovery procedure above). No other automatic path reclaims it, because the deleted-server
reclaim deliberately excludes `.displaced-<id>` (Lifecycle, above). Treat it
as one more world-sized copy under "Scratch capacity" above. What the decline
buys is the reverse guarantee: a `.displaced-<id>` tree never disappears on a snapshot
success that published some *other* tree. That is not a promise the tree is kept —
the ordinary clock keeps running, and the next snapshot that succeeds for this id while
the working directory is still the one it packed reclaims it (Lifecycle, above). What a
decline adds is **no deadline of its own**: recovery (the procedure above) races nothing
but that ordinary reclaim, so it does not have to be done inside the window this skip
opened.

**The sweep re-checks after its rename.** The identity check above runs *before* the
sweep starts, and it keeps passing until the racing hydrate renames the working directory
aside — so what the sweep's rename lifts out of the slot is whatever is there at that
instant, not what the check saw. A hydrate can clear world-less junk from the slot and
park its own live set there in between, and the rename then carries off that fresh
recovery copy. The sweep therefore checks the identity a **second** time, after the rename
and before anything is unlinked. It removes the tree only while the working directory is
still the one the snapshot packed; otherwise it renames the tree back into the slot, and
leaves it under its `.sweeping-<id>-*` name for the next boot to reclaim when it cannot.
Two things have to hold for the tree to go back, and both are content, not names:

- **The tree must hold a working set** — by the *same rule the hydrate applies to the
  slot*: it must be a directory (checked without following a symlink, so a link to a
  populated directory is junk on both sides) holding at least one entry that is not
  Worker-private generation state. Running-id snapshots take no per-server reservation, so
  *two* sweeps for one id can be in this window at once (only a running-id sweep ever puts
  a tree back, but the sweep it would block may be the stopped-id one), and world-less
  junk put back by one of them would occupy the slot against the other, which may be
  holding the hydrate's live set. Junk is left under `.sweeping-` instead, which is what
  the sweep was going to do with it anyway. A tree the Worker cannot *read* is **kept**,
  not dropped: a transient `EACCES`/`EMFILE`/`EIO` must never become "delete this at the
  next boot", the same direction an unreadable slot takes on the hydrate side.
- **The slot must be empty.** Not "must hold nothing worth keeping": a directory rename
  cannot replace an existing directory here, so marker-only junk in the slot blocks the
  put-back whatever the sweep thinks of it, and emptying the slot first is not available to
  a sweep — it holds no reservation, and removing the slot in place is the very thing the
  rename-then-remove discipline above exists to avoid (a hydrate can clear that junk and
  park its live set in between, and the removal would take the live set). Only a hydrate,
  under its per-id reservation, clears the slot.

**One sweep decides about the slot at a time.** Keeping an unclassifiable tree is only
safe because no *other* sweep can be deciding about the same slot meanwhile. Running-id
snapshots take no per-server reservation, so two sweeps for one id can reach it at once —
two running-id ones, or one from an older, already-dropped stream reaching it alongside
the stopped-id sweep, whose reservation rejects hydrates and starts but not a sweep that
takes no reservation at all — and while both sit between their rename and their decision,
whatever one of them puts back occupies the slot against the other, which may be holding
the hydrate's live recovery copy. The Worker therefore claims the id's slot for that
window, in memory. **Both** branches take that claim, and whichever sweep finds it already
held **declines**: it renames nothing and the tree stays where it is, to be swept by the
next successful snapshot for the id. For the stopped-id branch that is the only
*deliberate* decline, since it passes no identity pin — but not the only way it leaves the
tree behind: the sweep is best-effort at every step, so a slot it cannot read, a
`.sweeping-` name it cannot create and a rename out of the slot that fails all leave the
tree exactly where a held claim does (a slot that holds *nothing* is not this case — there
is simply no tree to sweep). What is specific to that branch is where those outcomes land:
it has just removed `<scratch>/<id>`, the advertisement that keeps the id eligible for any
later per-id pass (Lifecycle, above), so a tree left in the slot there waits for a
re-placement onto this Worker or for an operator (step 4 of the recovery procedure above)
— no next tick reclaims it. Only past the rename does a failure leave the tree under
`.sweeping-` instead, where the next boot's reclaim takes it. The claim covers only the
few syscalls from finding the tree to putting it back or committing to remove it — never
the world-sized traversal — and it refuses no command, so it is not the per-server
reservation CONTROL_PLANE.md Section 4.1 records as deliberately not taken. The invariant
it buys, together with the two content rules above: **a tree that holds a working set is
never deleted because some other tree's classification came out junk or uncertain.**

Both outcomes are logged, since they decide what the next boot deletes:

```
INFO  put back the displaced tree this snapshot was sweeping: the working dir was replaced mid-sweep, so the tree may be the replacing hydrate's recovery copy  server_id=<id>  retained=<scratch>/.displaced-<id>  reason=working_dir_replaced
INFO  left a swept displaced tree for the next boot to reclaim: the working dir was replaced while this snapshot swept it, and it could not go back into the slot  server_id=<id>  swept_to=<scratch>/.sweeping-<id>-<nonce>  reason=working_dir_replaced
```

What is left goes to the next boot rather than being deleted on the spot. A power loss
after the rename out of the slot and before the put-back is durable, and a slot a hydrate
refilled with a world-less directory, both leave a tree under `.sweeping-` that the boot
reclaim removes. Both are strictly narrower than the unconditional removal they replaced,
and the boot reclaim is deliberately not taught to put trees back: it cannot tell those
cases from an ordinary interrupted sweep, and guessing would resurrect trees a sweep was
entitled to delete. Neither check needs the per-server reservation on
running-id snapshots, which remains deliberately not taken (CONTROL_PLANE.md Section 4.1).

**A hydrate re-checks the slot before it discards.** The identity check still passes
while a concurrent hydrate is working, right up to the moment that hydrate parks the
working directory aside. So the sweep's rename can empty the slot after the hydrate's
oldest-wins check found it occupied, and before the hydrate discards the set it displaced.
Discarding then would leave no local tree at all. The sweep has removed the tree
oldest-wins kept, and the discard removes the one given up for it. Run one after the
other, the two operations keep one tree between them in either order. If the sweep runs
first, the hydrate finds the slot empty and parks the displaced set there. If the hydrate
runs first, the sweep declines and the kept tree stays. The discard is justified only by
the kept tree still being there, so the hydrate checks the slot again after its swap-in.
If the slot is empty, it moves the displaced set into the slot instead of deleting it.
That set holds the snapshot's published pack plus whatever the world wrote since:

```
INFO  hydrate: the retained displaced tree was swept by a concurrent snapshot before the discard; keeping the replaced working set in its slot instead (issue #3112)  server_id=<id>  retained=<scratch>/.displaced-<id>
```

Together with the sweep's own second check, that closes the no-local-tree outcome. The
sweep empties the slot *before* it re-checks, so a sweep that goes on to remove has
already left the slot empty for the hydrate's re-check to find. And it removes only while
the working directory is still the one it packed — which stops being true the moment the
hydrate parks that directory aside, before the hydrate can discard anything. Whichever
order the two run in, one tree survives beside the newly hydrated one: the kept tree if
the hydrate got there first, the displaced set (put into the slot by whichever of the two
finds it empty) if the sweep did. When both try to fill the same empty slot, one rename
wins and the loser leaves its tree under its own aside name for the reclaim that owns it.

---

## 5. File version retention

**Requirement (FR-FILE-3).** Each file edit is versioned; any retained version
can be restored.

**Decision — copy-on-write per file, count-bounded retention.** On every
`write_file` (and `rollback_file`), the **prior** content of that file is
copied into `versions/<rel-path>/<version-id>` before the new content is
published. `version-id` is a monotonic, opaque id whose lexicographic order is
creation order, so listing and pruning are a plain enumeration of the version
directory. Versions are **storage-only** — no database table indexes them
(DATABASE.md Section 8) and no author is recorded, so per-file attribution
("who edited this file") is not available from any layer.

Retention is bounded by a **configurable per-file version count** (default and
key owned by CONFIGURATION.md); when the count is exceeded the oldest version of that file is
pruned. Versioning applies to the **authoritative copy only** — edits to a
running server's live working set go over the control plane (ARCHITECTURE.md
Section 7.2) and are captured as versions when that working set is next
snapshotted and the resulting file differs, not on each keystroke.

**Crash-safe capture.** The version capture uses the same
temp-sibling + fsync + atomic rename discipline as Section 4.4 single-file
writes: the prior content is streamed into a dot-prefixed temp sibling
(`.{version-id}.*.tmp`) in the target `versions/` directory, fsynced, then
atomically renamed to its final name. A crash mid-capture leaves only the temp
sibling — never a truncated file under a valid version id. The enumerators
(`list_file_versions`, `_matches_newest_version`, `_prune_versions`) filter
dot-prefixed entries so leftover temps are invisible to callers. The startup
sweep (`_sweep_server`) reclaims stale `.*.tmp` files under `versions/` using
the same mtime age threshold as backup spool litter (Section 4.3).

**Alternatives considered.**
1. *Whole-working-set versioning* (snapshot the entire `current/` per edit) —
   simple to reason about but stores enormous redundant data per single-file
   edit; rejected at this scale.
2. *Delta/diff chains per file* — storage-efficient but adds a reconstruction
   step and a corruption-propagation risk across the chain; rejected as
   premature optimization for small files like configs (NFR-SCALE-1).
3. *Time-windowed retention* instead of count-bounded — harder to reason about
   storage bounds; count is simpler and predictable. (A time policy is not
   provided; the Port shape leaves room for one.)

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
- **Symlinks** are refused at **every** path component, not followed — see the
  per-component rule below.
- The `(community_id, server_id)` scope is applied by the adapter from
  trusted, API-minted ids (Section 2), never from the `rel_path`, so cross-server
  or cross-Community access is structurally impossible.

This is enforced in the adapter (not the use case) so that **every** backend gets
the protection and a future backend cannot forget it; the rejection is a typed
domain error so the API surface can map it to a uniform response.

### 6.1 The per-component symlink rule

**Symlinks in a working set are unsupported, full stop — at rest included.** The
stored working set cannot acquire one through any supported write: uploads refuse
symlink members, hydrate rejects them, and the Worker's snapshot tar skips them.
The live copy on a Worker is different. While a server runs, its own code, which
the system does not trust ([SECURITY.md](SECURITY.md) Section 6), has that copy
bind-mounted and can create symlinks in it at will. The snapshot walk skips them
without following them, and the Worker's file operations on a running server
refuse to follow them, so none of them reaches the store. One can therefore
reach the stored working set only out of band, from an operator over SSH. The
hydrate does not rely on its fresh staging directory holding no link either: it
opens every directory on a member's path relative to the one before it without
following a link (`openat2` with `RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS`, or
`O_NOFOLLOW` where `openat2` is unavailable: a kernel older than 5.6, or a
seccomp profile that denies it). It creates each file only where nothing is
(`O_EXCL`), so neither a symlink nor a hard link already at a member's name is
written through. Either fails the hydrate instead of redirecting a write (issue
#3940).

Every operation that takes a `rel_path` resolves it the same way, in this order:

1. **Containment** — the resolved candidate must stay inside the server root.
   Failing it is `PathTraversalError`, and it is decided **first**, so a link out
   of the root always reports the escape rather than the refusal below.
2. **The symlink refusal** — if resolving the path crossed a symlink at any
   component, it is `SymlinkRefusedError`, distinct from `PathTraversalError`. The
   edge renders it as `422 symlink_refused`, which is the same reason the Worker's
   running-server path returns (`FileAccessReasonSymlinkRefused`), so one
   browser click gets the same sentence whether the server is at rest or running.
3. **The literal join is opened** — nothing has been followed, so the open answers
   what is really at the path.

Consequences worth stating:

- Read-through of an operator-created **intermediate** symlink is deliberately
  foreclosed on the fs backend (browsing a subfolder SSH-symlinked onto another
  disk, say). That workflow is unsupported system-wide — the running path
  refuses it and hydrate will not start such a server at all.
- A symlink **loop** and an over-long component stay the modelled **miss**: a
  non-strict canonicalization leaves both literal, so step 2 sees nothing crossed
  and the open reports `ELOOP` / `ENAMETOOLONG`, which the read paths fold
  into the miss.
- **Mutations** apply the rule to their parent chain, and a symlink **dirent** (a
  leaf link) supports exactly two operations — being listed and being deleted.
  `delete_file` unlinks the link itself (working, dangling and looping
  alike), capturing no version because a link has no readable content to retain;
  every other mutation refuses it — `write_file`, a `rename` **source**, and
  `make_dir` are `symlink_refused`, `delete_dir` misses (a link is never a
  directory dirent), and `retain_file_version` is a no-op. So the mutation surface
  matches the listing: the delete button acts on the dirent the browser shows, and
  nothing writes through, moves, or destroys a link's target.
- `path_exists` likewise applies it to the parent chain only: its leaf is described
  as itself, because a link occupies its name whatever it points at.
- The fs realization compares the already-computed canonical path against the
  literal join, so the rule costs no syscall beyond the one every resolve pays
  anyway (no `lstat` is needed). It leaves a resolve-then-open window (only
  operator SSH can race it at rest); closing that wants an `O_NOFOLLOW` open and
  is not provided.
- On a backend without symlinks (object storage, Section 7.3) step 2 is vacuous.

---

## 7. Adapter families

All three families implement the **same `Storage` Port** (Section 3) and provide
the **same observable atomic-publish guarantee** (Section 4); they differ only in
how they realize it. Selection is by configuration (CONFIGURATION.md Section 5.2); no caller changes when
the backend changes (FR-DATA-2). Adapters are named `<Tech><Port>` per
ARCHITECTURE.md Section 6 (e.g. `FsStorage`, `ObjectStorage`).

### 7.1 fs (local filesystem) — default

| Aspect | Guarantee / mechanism |
|---|---|
| Backing | A directory tree on the API host's local disk, rooted at `<root>` (Section 2). |
| Atomic publish | `current` symlink flip via same-directory `rename(2)` (Section 4.2). |
| Single-file write | temp-write + fsync + atomic rename (Section 4.4). |
| Path-traversal | canonicalize + root-containment check (Section 6). |
| Best for | A single-host deployment where the API and its store share one disk — the simplest deployment. |
| Caveat | `<root>` must be a single filesystem so the snapshot move and the symlink rename stay atomic; staging, snapshots, and the symlink must not straddle a mount boundary (a cross-device rename is not atomic). |

### 7.2 remote-fs (network/shared filesystem)

| Aspect | Guarantee / mechanism |
|---|---|
| Backing | A POSIX-like remote/shared mount (NFS, SMB, a CSI volume) presented as a normal path. |
| Atomic publish | Same `current` symlink-flip mechanism as fs, **provided the mount honors symlinks, hard links, atomic same-directory rename, and close-to-open consistency.** |
| Single-file write | temp-write + fsync + atomic rename, same as fs. |
| Path-traversal | Identical to fs (the adapter logic is shared). |
| Best for | Letting the authoritative store outlive a single API host / sit on shared infrastructure without moving to object storage. |
| Caveat | Atomicity and durability depend on the mount's semantics; the adapter documents the required guarantees (symlink support, hard-link support, atomic same-dir rename, fsync durability) and the operator must provision a mount that meets them. SMB mounts frequently lack POSIX symlink support, so the remote-fs adapter requires a backing filesystem/mount with POSIX symlink and atomic same-directory rename semantics (NFS qualifies; SMB often does not). The store owner marker is claimed by hard-linking a temporary file into place at the root (Section 4.3), so the mount must also support hard links, and one that does not fails the API at startup (NFS qualifies). Cross-device rename caveat (Section 7.1) applies identically. |

`remote-fs` may share most code with `fs` (both are path-based); they are
distinct entries because their **operational guarantees and failure modes
differ**, and `storage.backend` selects between them explicitly. Config: set
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
| Caveat | No atomic multi-object rename and no real directories — hence the pointer-flip design. List operations are prefix scans. Multi-member at-rest edits (`delete_dir`, `rename_dir`, `rename_file`) are per-object loops, not crash-atomic (Section 4.4 accepted gap). |

**Compose deployment.** This is the **default** backend for the
compose deployment, realized over **SeaweedFS** (Apache-2.0, master/volume,
designed for many small files). The deployment wiring, credentials, opt-out, and
the live contract tests are in
[DEPLOYMENT.md Section 5](../dev/DEPLOYMENT.md#5-storage-backend-object-on-seaweedfs-default).
The app implements its own snapshot/version logic, so S3 versioning / object-lock
are not required. Orphan multipart reclamation is **defense-in-depth**:
the startup sweep age-gates uploads via `ListParts` when SeaweedFS omits
`Initiated` (Section 4.3), and behind it a bucket-level
`AbortIncompleteMultipartUpload` lifecycle rule reclaims the residual gap the
sweep cannot age-gate — an upload that crashes after `CreateMultipartUpload` but
before its first part, which carries no timestamp. The rule is applied by a
one-shot compose service (`seaweedfs-lifecycle`) on startup and self-verifies
that SeaweedFS honored it (DEPLOYMENT.md Section 5); `weed shell
s3.clean.uploads` is an optional operator-side cleanup as well.
**Bucket provisioning.**
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

**Explicit transport budget.** The S3 client is built with an
explicit `botocore.config.Config` rather than inheriting botocore's hidden
defaults (60 s connect/read timeouts and the `legacy` retry mode), so every
object operation — hydrate, snapshot, upload, restore, and the at-rest backup
archive — runs against a known,
operator-sized ceiling and a stalled read fails fast instead of silently hanging.
The three knobs (`storage.object.connect_timeout_seconds`,
`read_timeout_seconds`, `retry_max_attempts`; CONFIGURATION.md Section 5.2) are
threaded into `make_s3_client_factory`. `read_timeout_seconds` bounds a single
socket read, not a whole transfer, so a multi-GB stream (chunked at 8 MiB) is
unaffected while a genuinely stalled read is still capped; retries use botocore's
`standard` mode (capped exponential backoff, broader retryable-error set).

**At-rest integrity sweep limitation.** The at-rest **structural**
fsck (`check_current_health`, Section 3.1; the `.mca` walk behind
`check_backup_health`) does not inspect the stored regions on the object backend.
The reason is structural: the object backend has no local working-set directory to
walk; the fsck implementation walks a local filesystem tree (the `current/` symlink
target on fs), and no equivalent materialisation exists on the object side. The
**publish-time** fsck (`_check_staged_regions`, wired on `commit_snapshot`,
`restore_backup`, and `create_backup_from_current`) **is** implemented on the
object adapter and is the authoritative gate — it downloads and validates
each `.mca` member during staging, so a corrupt region is refused before it
becomes authoritative. The gap is limited to the **read-only sweep** that
re-checks already-published snapshots at rest: on the object backend that sweep
examines no server's `current` at all. Fetching and structurally checking the
`.mca` objects from the store (downloading headers only, mirroring the fs walker)
is not implemented — the publish-time gate is the correctness guarantee, and the
sweep is defense-in-depth.

**Saying so in the sweep summary.** On this backend `check_current_health`
returns `None` — **not examined** — rather than a healthy `WorkingSetReport`: a
healthy report here would be a verdict nothing produced, and one an operator could
not tell apart from a real one, given that `check_backup_health` on this backend
*does* read the bytes (below). The servers seam reports `None` as
`SnapshotScan.NOT_EXAMINED` and the sweep counts it as `snapshots_not_examined`,
printed on its own `integrity_sweep_cli` line and kept out of `snapshots scanned`.
A backend that examines nothing answers this for **every** server, published or
not: it reads nothing that would tell the two apart, so unlike fs it cannot skip
the never-published ones. The limitation above stands — no published snapshot is
read at rest on this backend; the sweep summary says so rather than claiming
otherwise.

**Backup readability probe.** `check_backup_health` on the object backend is
*not* limited that way: it answers the question a `HEAD` never could — can the
store still **produce** this archive? A store can serve an archive whose body ends
deterministically short of the `Content-Length` its `HEAD` declares. The
signature of that failure: the connection stalls for ~12 s at the same offset on
every attempt and then aborts, reproducibly across days. Such a backup is
unrestorable, yet a probe that reads no bytes would list it as
`health: healthy`.

The probe therefore streams the stored object end to end — the same object the
download and restore paths read — and requires both that the delivered byte count
equals the declared length **and** that the bytes decompress as a gzip stream
terminating at a well-formed CRC32 + ISIZE trailer, so silent bit-rot is caught
and not only truncation. Decompressed output is discarded as it is produced
(`GzipReadProbe`), and nothing is buffered whole. The bytes the read delivers are
spooled to a local temp file, which a sound read then walks member by member the
way restore does, discarding the bytes: bytes the store can produce can still hold
nothing restore can stage. A member the archive member rule (Section 3.3)
refuses raises `ArchiveUnreadableError`, and so does an archive that does not
read to its end the way restore reads it (One archive member rule, Section 3.3);
members inflating past the restore decompressed-size
cap raise `ArchiveTooLargeError`, as in restore. It does not stage members or
region-fsck — that stays fs-only, above.

The spool has an operational cost that operators on this backend must provision
for:

- **Location.** Python's default temp directory (`TMPDIR`, normally `/tmp`) in
  the API process, which is not a storage volume.
- **Size.** About one full compressed archive per concurrent health check, with
  no explicit cap. The decompressed-size cap does not bound it: the member walk
  runs only after the whole archive has been downloaded into the spool.
- **CPU.** The member walk decompresses the archive a second time, after the
  gzip probe.

A transient fault on the spool (the fs errno policy's set, such as `EIO`) is
`StorageUnavailableError`: the sweep stops with no verdict written, exactly as for
a store outage, and the fault never reaches the short-body classification, so it
cannot mark an intact archive unreadable. `ENOSPC` / `EDQUOT` on the spool stay
the raw resource error that policy keeps them as. An archive the store
cannot reproduce raises `ArchiveUnreadableError`, which the servers seam
translates to `BackupUnreadableError` and the sweep records as `UNREADABLE` — a
verdict of its own, not the structural `QUARANTINED` — so the stored health, the
summary, the audit trail and the webui all tell "the bytes are gone" from "the
world is corrupt".

**`UNREADABLE` vs. `QUARANTINED`.** The two verdicts carry different remedies. A
`QUARANTINED` backup holds a structurally corrupt world: every byte is there, and
the operator override (`?force=true`) can still restore it. An `UNREADABLE`
backup's archive could not be read back at all — the store cannot produce its
bytes, or (a dangling row) holds no archive for it — so nothing can restore it,
and the fault points at storage rather than at the world. The restore use case
therefore refuses an `UNREADABLE` backup from its recorded verdict, before any
read and whatever `force` says (409 `backup_unreadable`), and the webui offers no
restore for it. The sweep audits the two under separate operations
(`backup:quarantine`, `backup:mark_unreadable`).

A restore can also be the **first** read to find an archive unreadable: on either
backend its extraction raises `ArchiveUnreadableError` when the bytes do not read
back as a `tar.gz` (truncated, damaged gzip/tar framing, a gzip stream that does
not end soundly or a tar that does not end at its end-of-archive marker, issue
#3798, or a member header
whose `GNU.sparse.size`, `GNU.sparse.realsize` or `GNU.sparse.map` field, or
sparse format 1.0 map, is not a number, issue #3790, or whose numbers cannot
describe a file: a negative size, or a sparse map entry with a negative offset
or length, one past the real size, or entries that overlap or are out of order,
issue #3802) or
hold a member the extraction refuses, and `ArchiveTooLargeError` when the
members inflate past the decompressed-size cap. Neither can be overridden, so the servers seam translates
both to `BackupUnreadableError`, for the restore and for the sweep's probe alike.
The restore answers the same 409 and records `UNREADABLE` on the row — the verdict the sweep would have reached,
just as a refused corrupt restore records `QUARANTINED` — so the next attempt is
refused from the verdict instead of re-reading the archive. On the object backend
that verdict is reached only once every declared byte is in the restore's spool: a
restore reads the archive once, so a body that ends short of its `HEAD` length is
the single ambiguous observation the probe below re-reads to classify, and restore
refuses it as an outage (`ObjectStoreUnavailableError`, 503) rather than condemning
the backup. The fs `check_backup_health` reaches the verdict the same way as
restore, its extraction failing before any region is walked.

Neither verdict is sticky. The sweep re-reads **every** backup row whatever its
recorded health, so a `QUARANTINED` or `UNREADABLE` row whose archive now reads
back sound returns to `HEALTHY`. The same pass is what re-classifies rows
quarantined before `UNREADABLE` existed: migration `0038` leaves them as they are,
so until a sweep completes a `quarantined` row may mean either finding.

A false *unhealthy* is as harmful as a false *healthy* — it
condemns a restorable backup and emits a spurious audit entry — so the gzip
probe refuses nothing that restore accepts. The probe reads gzip through the
same decoder as restore (`archive_member.GzipDecoder`, issues #3835, #3847;
One archive member rule, Section 3.3): every gzip member, a concatenated one
too (legal gzip, and what the shell tools produce), its header CRC and reserved
flag bits, its CRC32 and ISIZE, zero padding between and after members, and at
most 10,000 members. Only the end of the stream waits for the whole body, so a
body cut short reaches the re-read below. The member walk that follows the
probe on the spool reads the archive as restore does, so it refuses a second
tar after the end-of-archive marker, or a first gzip member that holds only
part of the tar (issue #3236). So the health check and restore reach one
verdict on the gzip stream, its headers and trailers included.

Reading every archive is deliberate cost: the sweep is operator-invoked only
(`integrity_sweep_cli`, never run on boot), so there is no sampling, cap, or
config knob.

**Damage vs. outage.** A body that ends early looks identical whether the object's
bytes are damaged or the store is merely having a bad minute — and a verdict on
the latter would condemn every backup in the deployment over one outage. The
damage class the probe exists to catch is *persistent*: the transfer never gets
past one fixed point, on every attempt. So the probe re-reads once, after a short
backoff so a momentary fault has a chance to clear, and the two reads decide:

- **The re-read completes** — the archive is healthy (subject to the same gzip
  check as any full read): the store just produced every declared byte, which a
  damaged object cannot do.
- **Both reads ended short, each after delivering bytes** — the archive is
  unreadable, reported as ending at the **further** of the two points. The two
  need not agree: a connection torn down with an RST rather than a graceful close
  discards whatever was still in flight — a timing-dependent few MB — so two reads
  of one damaged body stop at different offsets (issue #2381). An RST only ever
  loses bytes, never invents them, so neither read got past the cut and the
  further one is the better estimate of it.
- **Neither read delivered a byte** — the archive is unreadable (issue #2379).
  The store answered `HEAD` for this object moments earlier, and a store that is
  down fails there first, so an object it reproducibly serves nothing of is
  treated as dead rather than as an outage. Treating it as an outage stopped every
  sweep pass on that one object, so no other backup was ever checked again.
- **One read ended short after delivering bytes, the other delivered none** — an
  outage. The empty read never reached the body and says nothing about where this
  object's bytes end, so it cannot corroborate the short one, and the pair stays
  `ObjectStoreUnavailableError` → `BackupStorageUnavailableError`. The sweep does
  not catch that: the pass stops, logging which backup it died on, and the CLI
  exits non-zero with an operator-facing message rather than a traceback.

Accepting disagreeing stop points trades away some outage protection: a store that
tears down both reads mid-body, at different points, now marks the backup
`UNREADABLE` rather than stopping the pass. The alternative was worse in practice — requiring
the two points to be equal misread an RST-torn damaged body as an outage on about
one probe in five, aborting the whole pass. Condemning a reproducible zero-byte
delivery trades away more of it: a backend that answers `HEAD` but serves no body
— half-up, or failing between the `HEAD` and the reads — marks an intact backup
`UNREADABLE`. That verdict is never terminal: every pass re-probes every row, and
a later complete read returns it to `HEALTHY`. The extra read is paid only on the
failure path. A clean early EOF takes the same re-read path as a transport
teardown — it is the same observation and equally unable to tell damage from an
outage on one attempt. The stop points are byte-exact in practice, not rounded to
the 8 MiB read chunk: a read returns whatever the connection has buffered rather
than waiting for a full chunk.

Supporting this, `_iter_body` translates **any** exception raised while reading a
response body to `ObjectStoreUnavailableError`, so a teardown is a typed storage
outcome rather than a raw third-party type crossing the Port. The breadth is
deliberate: the shapes a short body produces are spread across three libraries and
none is in the upload-failure tuple — aiohttp's `ClientPayloadError` (what a
short body raises) is a *sibling* of `ClientConnectionError`, which is all
aiobotocore's `StreamingBody.read` maps, and botocore's `IncompleteReadError` is a
bare `BotoCoreError`. A name-based enumeration there is a standing bug, because a
miss does not degrade the probe, it disables it. `BaseException` is not caught, so
`GeneratorExit` and `CancelledError` still pass through.

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

**Archive format.** A plain, uncompressed stdlib **tar stream** of the
working-set root (the same format `open_hydrate_source` / `write_snapshot`
produce/consume, Section 7.1). No compression: the snapshot read takes the body
as a plain tar only, so a gzip, bzip2 or xz body, sound or not, is refused as
`422 invalid_archive` (issue #3786). The Worker's pack writes plain tar. It
refuses a member whose path below the working-set root is longer than 4095 bytes
(`PATH_MAX` − 1), whatever the Worker's data root, and the hydrate creates each
member relative to the directory it unpacks into. On the Worker side, then, no
data root's length counts against a member: one that packs on one Worker unpacks
on any other (issue #3341). The member walk must end at the tar end-of-archive
marker with only zeros after it, and must not seek back, or the body is
`422 invalid_archive` too (issue #3798): a header past the first that does not
read would otherwise end the walk and publish the members before it alone, and
a member whose data runs past its last 512-byte block would be published padded
with the next member's bytes (one that stays within the block's padding is
issue #3828).

**Endpoints** (scoped by `(community_id, server_id)`). Like the rest of the HTTP
API these are namespaced under `/api`; the API builds the full
`transfer_url` it hands the Worker over the control plane, so the Worker follows
whatever path the API emits:

| Method & path | Meaning | Success | Errors |
|---|---|---|---|
| `GET /api/data-plane/communities/{c}/servers/{s}/working-set` | Hydrate: stream the authoritative working set as a tar (with the resolved `server.jar` injected when present). Response always carries `X-Working-Set-Generation: <n>` (the generation of the snapshot served; 0 when no snapshot has been published). | `200` tar body | `204` no published snapshot *and* no resolved JAR (Worker starts from an empty dir, generation header still present); `401` |
| `POST /api/data-plane/communities/{c}/servers/{s}/snapshot` | Snapshot: stream a tar into staging and atomically publish it. The request MAY carry `X-Working-Set-Base-Generation: <n>` (the store generation this set was hydrated from) and `X-Worker-Id: <uuid>` (the publishing Worker, canonicalised so any spelling of its UUID compares equal to the assigned worker id), both read by the publish-time generation guard; a never-hydrated Worker omits them and the guard stays permissive. The content-integrity gate uses the single region rule set (below): a non-4096-aligned tail is the normal on-disk format of a 26.x world, not corruption (byte-precise per-chunk bounds), on any source — no request header declares a snapshot source. On success the response carries `X-Working-Set-Generation: <n>` (the new generation minted by the publish). | `204` | `400` length mismatch / incomplete; `400` `empty_snapshot` (staged an empty working set); `422` `invalid_archive` (the byte-complete body does not read as a plain, uncompressed tar — a compressed body included — does not end at its end-of-archive marker with only zeros after it, holds a member whose data runs past its last 512-byte block, or carries a member ingest refuses: an absolute, `..` or non-canonical name on both backends, or a link on fs (the object backend leaves a link member out)); `409` `stale_generation` + `base_generation` + `current` (the declared base is older than the store's current generation AND that current was published by a *different* Worker — an A→B→A stale-scratch publish; a same-Worker lag is a lost response and is allowed to self-heal); `409` `deleted_during_upload` + `base_generation` + `current` (a concurrent delete's retention prune ran under the per-server lock during the upload window, so the commit's re-check found the generation advanced and no live snapshot, or the prune had removed the spool or staging itself, before the commit or under one of its lock-free steps — the delete wins and the staging is discarded); `409` `publisher_not_assigned` (the `X-Worker-Id` publisher is not the server's assigned Worker while another one is, checked after the upload stream and again under the per-server lock immediately before the publish; the staging is discarded); `411` no `Content-Length`; `413` over the size cap; `422` `working_set_corrupt` + `corrupt_count` (integrity gate refused the staged set); `422` `working_set_incomplete` + `affected_count` + `directories` + `truncated` (the missing-region gate refused a staged set that dropped some-but-not-all region files of a still-live dimension; `directories` is a **bounded** per-directory list of the lost `.mca` names — `{directory, missing[]}`, capped — so the operator can drive the recovery in Section 4.5, `truncated` flags that the list was capped); `401` |

**`X-Worker-Id` trust.** The `X-Worker-Id` the guard reads is **not**
authenticated within the shared-credential data plane: any Worker holding the
transfer token can claim any id (consistent with the shared-credential trust model
above). This is acceptable — the guard is defense-in-depth behind the primary
protection (the API holds the assignment across the final snapshot, so the genuine
stale cross-worker publish never arises), and a spoofed id can at worst weaken
(never strengthen) the guard for a caller already trusted to write the working set.

**JAR posture.** The resolved server JAR reaches the Worker as part of hydrate
(ARCHITECTURE.md Section 7.3): `StartServer` ensures the resolved JAR is in the
content-addressed pool before placement and records its content key on the
`server` record (in the `config` JSONB blob, key `resolved_jar_sha256` — DATABASE.md
Section 7 has no dedicated JAR column), and the hydrate endpoint **injects** that
JAR into the working-set tar at the conventional `server.jar` relpath. The JAR is
*omitted when not present* (no resolved JAR recorded, or the recorded JAR not
in the pool), so a working set with no resolved JAR is sent alone. The injection
prepends a single tar member to the working set's members; when there is a resolved JAR but no published snapshot, the body is
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
rejected, as the API's fs adapter rejects them when it takes an archive in. A
working set is plain files and directories on every leg: the Worker packs
nothing else, and neither backend holds or gives out a link member — the fs
adapter refuses one at snapshot and restore and writes any link its store holds
regardless as a regular file (hard link) or leaves it out (symlink), and an
object has no way to be one, issue #3371); snapshot = pack the
working dir into a tar and POST it with a `Content-Length`. Before packing, the
Worker runs a structural region fsck over the working set: if any
`.mca` file is found corrupt, the snapshot is refused at source with a
`TRANSFER_FAILED` outcome whose message reads `snapshot refused: <n>/<total>
region files corrupt (e.g. <file>: <reason>)` — a clear failure with no wasted
tar+upload — rather
than shipping a corrupt set the API gate (the `422` above) would reject after a
full round-trip. A fsck I/O error is best-effort (logged, transfer proceeds) so
the fsck never wedges a snapshot; the API integrity gate is the correctness
guarantee.

**One region rule set, applied everywhere.** The structural fsck and
the API integrity gate run **one rule set** at every gate — no source-keyed mode
split. MC 26.x pads region files to a 4096-byte sector boundary only on
shutdown/close, so a **running** (even quiesced) world legitimately keeps an
**unpadded tail** — the last chunk ends mid-sector and the file size is not a 4096
multiple (the trailing chunk is complete and decompresses cleanly). The rule
accepts that unpadded tail and applies a
**byte-precise** per-chunk bound (`offset*4096 + 4 + length <= size`): a trailing
chunk whose declared length overruns the real EOF is still corrupt, an entry
pointing at/past EOF is `sector_out_of_bounds`, a severed prefix is a short-read
`truncated_chunk`. Alignment is retained as a signal only for the
**sub-header-size** case (a non-zero size below the two 4096-byte header sectors is
a torn save, reported `not_4096_aligned`). A 0-byte file is an empty container
(healthy).

**Why no source-keyed split.** The alternative is to split the rule by snapshot
**source**: assume a **stopped** world is 4096-padded, so a non-4096 size there is
a torn save (strict), while a running source runs the byte-precise rule (live),
with the source declared via a request header. That `stopped => 4096-padded`
invariant **does not hold**: a sweep-stop timeout, SIGKILL, OOM, crash, or host
loss can leave a stopped world's regions unpadded, and a strict rule would then
refuse the **stop-leg checkpoint exactly when it is the last chance to capture the
world** (a running server the orphan sweep stops on a timeout can keep several
regions unpadded after the stop, and the pre-pack fsck would refuse the stopped-id
snapshot with `not_4096_aligned`). Strict adds detection power **only** under that
invalid invariant, so there is one rule set and no source header. Every gate — the
Worker's pre-pack fsck on either path, the API publish gate,
`create_backup_from_current`, `restore_backup`, `check_current_health`,
`check_backup_health`, and the integrity-sweep fscks — runs the single rule set, so
a legitimately unpadded set committed into `current/` (or any archive derived from
it) is never falsely rejected.

Because `check_backup_health` runs the single rule set and the sweep rewrites the
health column every pass, a quarantine is not sticky: a backup whose recorded
quarantine rests **only** on `not_4096_aligned` while all referenced chunk extents
lie within EOF — a shape the single rule set accepts — is marked HEALTHY on the
next sweep. This rescue is intentional: such a backup is loadable content, and the
realistic torn shapes stay caught (a location entry at/past EOF →
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
server's command.

**Lifecycle wiring (FR-DATA-4).** `StartServer` hydrates before the launch (the
API issues a hydrate trigger, then `StartServer`). A stop takes the final
snapshot **before** it releases the Worker assignment: the API
issues the stop, then — once the Worker confirms the process is gone — records
observed=stopped, issues the snapshot trigger, and only then clears the
assignment. Holding the assignment across the snapshot is what keeps a release
from discarding a capturable working set: a start racing the in-flight upload
finds the server still assigned and 409s on its unassigned-only
compare-and-set instead of re-placing it on a different Worker that would
hydrate the pre-snapshot generation. Scratch is reclaimed only after the
snapshot publishes.

The snapshot does not decide the stop's outcome, but it does decide the
release:

- **Success, or the Worker disconnected** — the assignment is cleared (the
  `SERVER_NOT_FOUND` path additionally requires that the same call established
  the row is at rest under this Worker; a convergence write beaten by a fresher
  report holds instead). A failed result is logged loud because no retry exists
  for a stopped server, but publication is unconfirmed: a later cross-worker
  re-placement may lose progression since the last periodic snapshot
  (#845/#847). Before recovery, inspect authoritative Storage's current snapshot;
  the result carries no publication receipt and cannot distinguish a failed
  publish from a lost publish response ([CONTROL_PLANE.md](CONTROL_PLANE.md)
  Section 5.1). The stop already succeeded and the server is down, so the release
  still proceeds. The deliberate exception is the Worker's
  `working_set_absent` refusal: it holds no working set for the id, so there was
  nothing to capture and the line is INFO, not loss.
- **The snapshot dispatch timed out, or the HTTP request was cancelled** — the
  assignment is HELD. The Worker session is healthy and the transfer is still
  uploading (the control plane has no command-cancel, so abandoning the pending
  result signals nothing to the Worker: it uploads to completion and may publish
  late); releasing would reopen the very race above, whereas an upload that
  lands while the row is still held publishes against the base it hydrated from.
  The Worker's late `CommandResult` releases the hold as soon as the upload
  settles, and the reconciler is the backstop once the stop grace
  lapses.

Wherever a final snapshot is taken, it precedes the clear: on the graceful
`StopServer`, on the reconciler's `redispatch_stop` replay of it, on the
`SERVER_NOT_FOUND` release those two share when the Worker holds no live
instance for the id (that is the answer a Worker gives for a
*crashed* instance, whose retained scratch still holds the crash-window world),
and on the reconciler's stale-assignment arm for a stopped or crashed server
whose Worker is still connected. The remaining legs of that
arm release with **no** snapshot at all: a disconnected Worker cannot be asked
for one, and an `unknown` or `stopping` row may still have a live process, so
there is no settled working set to capture. A `HydrateTrigger` is only valid for
a stopped server (refreshing a running server's working set would corrupt live
state).

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
concerns and the control plane. The REQUIREMENTS.md Section 6.9 policy is a
business rule and belongs in the use case. `Storage` stays a pure store:
always-safe primitives,
no policy.

### 9.3 The Port hides the data-plane transport

**Decision.** `Storage` exposes streams and a publish handshake (Section 3.1);
the API↔Worker transfer wire format is the data plane (Section 8), not part of this
Port.

**Alternatives.** Fold the transfer protocol into `Storage`.

**Rationale.** Keeps `Storage` about the authoritative store and lets the data
plane evolve (chunking, resumable transfer, delta sync — FR-DATA-5)
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

### 9.5 Out of scope (not implemented)

- **A sweep-side guard for long multipart uploads.** The recovery sweep runs at
  API startup and then periodically (Section 4.3), and its spool / multipart
  reclaim paths apply the 1 h age threshold so a periodic pass does not eat a
  live write's temp spool. That threshold is *not* fully safe for a long-running
  multipart upload: unlike an fs spool's mtime — which advances as the write
  progresses — the S3 `Initiated` timestamp is fixed at create time and never
  refreshes, so a legitimate upload still streaming past the 1 h threshold *can
  be aborted mid-flight* by a periodic sweep pass (a startup-only sweep cannot
  collide with a live upload; a periodic one can). The failure is loud, not
  silent — the upload's next `upload_part`/`complete` gets `NoSuchUpload`, which
  surfaces as the backup error and **must be retried** (`upload_multipart`'s
  cleanup routes through the *translated* idempotent abort, so a
  complete-vs-abort race makes that cleanup a no-op and the original error is
  not masked). No in-sweep detection of a live upload is provided; the
  mitigations are the daily loop cadence, which keeps the sweep cold so overlap
  with a multipart upload still running after an hour stays rare, and the
  storage-layer `AbortIncompleteMultipartUpload` bucket lifecycle rule
  (Section 7.3), the primary defense — it reclaims orphan parts natively with
  its own generous age, so the sweep's in-process multipart abort is a backstop
  rather than the mechanism that must catch every orphan within a tight window.
- **Continuous delta sync** (FR-DATA-5) is not implemented; the streaming Port
  shape leaves room for it.
- **WebUI surfacing of `working_set_incomplete`.** The 422 the missing-region
  gate returns is a transient response to the *Worker's* snapshot `POST`
  (Section 4.5); the WebUI never makes that call and no control-plane route
  exposes a server-level "last snapshot was refused as incomplete" condition
  (unlike backup `health`, which the WebUI badges). Surfacing it would need new
  machinery — persist the refused condition on the server record (or expose
  `check_current_health`, Section 3.1, over a control-plane route) and add a
  server-page badge/notice — none of which is provided. **API contract if it is
  added:** reuse the 422 body shape above (`reason: "working_set_incomplete"`,
  `affected_count`, bounded `directories: [{directory, missing[]}]`,
  `truncated`) and badge the server following the backup health-badge pattern,
  pointing the operator at the Section 4.5 recovery.

---

## 10. Related documents

| Doc | Relationship |
|---|---|
| [`../REQUIREMENTS.md`](../REQUIREMENTS.md) | Source of truth: Sections 6.8–6.12, FR-DATA-1…7, FR-FILE-3, FR-FILE-4, FR-BAK-*, FR-VER-3. |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | `Storage` Port placement (Section 5.1), layering and edge wiring (Section 2), naming (Section 6), the running-server file-access decision (Section 7.2) that bounds this Port's file ops to the stopped-server case. |
| [`DATABASE.md`](DATABASE.md) | The metadata indexing the blobs this document lays out (`Server`, `Backup`). Blob layout is here; metadata tables are there. File versions have no metadata table — they are storage-only (DATABASE.md Section 8). |
| [`CONFIGURATION.md`](CONFIGURATION.md) | The runtime keys that select the backend family and tune snapshot interval / version-retention count. This document defines *what* is selectable; `CONFIGURATION.md` names the keys. |
