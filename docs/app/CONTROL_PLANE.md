# Control Plane

> Status: **Design** · Audience: contributors to `api/`, `worker/`, `proto/`
>
> This document is the reference for the API↔Worker **control-plane**
> contract: the gRPC bidirectional stream, its lifecycle, the command and event
> messages, and how each maps to [`../REQUIREMENTS.md`](../REQUIREMENTS.md). The
> binding contract is the buf module under [`../../proto/`](../../proto/)
> (`mcsd.controlplane.v1`); this document explains it. Where the two disagree,
> the `.proto` files and the requirements win and this document is wrong.

## Table of Contents

1. [Scope](#1-scope)
2. [Connection model](#2-connection-model)
3. [Message envelopes and correlation](#3-message-envelopes-and-correlation)
4. [Stream lifecycle](#4-stream-lifecycle)
5. [Commands (API to Worker)](#5-commands-api-to-worker)
6. [Events (Worker to API)](#6-events-worker-to-api)
7. [Error reporting](#7-error-reporting)
8. [Requirement mapping](#8-requirement-mapping)
9. [Related documents](#9-related-documents)

---

## 1. Scope

The control plane is the lightweight, always-on command/event channel between
the API and a Worker (REQUIREMENTS.md Section 5.2). It carries small,
latency-sensitive messages: lifecycle commands, RCON/console commands,
hydrate/snapshot **triggers**, file read/edit of a running server, and the
Worker's status / log / metrics / heartbeat events.

It does **not** carry bulk data. World/JAR/backup transfer (hydrate, snapshot)
rides the separate **data plane** — an API-terminated HTTP endpoint — so large
transfers never block control traffic (REQUIREMENTS.md Section 5.2,
ARCHITECTURE.md Section 4). The control plane only *triggers* a transfer and
hands the Worker the URL and a one-time token; the bytes move out of band.

---

## 2. Connection model

The Worker initiates and maintains a single persistent gRPC **bidirectional
stream** to the API (REQUIREMENTS.md Section 5.1). One stream multiplexes
everything for that Worker; the API never dials a Worker, so Workers need no
inbound exposure and can be added or removed dynamically (ARCHITECTURE.md
Section 7.2).

The service is one RPC:

```
service WorkerService {
  rpc Session(stream WorkerMessage) returns (stream ApiMessage);
}
```

- `WorkerMessage` — everything the Worker sends (registration, command
  results, events).
- `ApiMessage` — everything the API sends (registration acknowledgement,
  commands).

The channel is authenticated and encrypted (REQUIREMENTS.md NFR-SEC-1).
**Authentication** is the shared Worker credential carried in call metadata
(`authorization: Bearer <credential>`); **encryption** is server-side TLS on the
gRPC listener. The TLS material and the Worker credential are configuration
(CONFIGURATION.md Sections 5.1 and 6.1). Transport security sits below this
contract and is not modelled in the `.proto`.

**mTLS is deferred (M1).** The control channel currently uses server-side TLS
only: the API presents a certificate the Worker verifies, and the shared
credential authenticates the Worker. Client-certificate (mTLS) verification is
not implemented; the `control.tls.client_ca_file` / `api.tls.client_cert_file` /
`api.tls.client_key_file` keys are documented-deferred placeholders that keep the
config shape forward-compatible (CONFIGURATION.md Sections 5.1, 6.1).

### Deployment posture

Two ways to satisfy the encryption requirement:

1. **Direct TLS (default).** Set `control.tls.cert_file` / `control.tls.key_file`
   on the API; the gRPC listener serves TLS directly. The Worker verifies it
   against `api.tls.ca_file`. The required-unless-insecure rule means the API
   fails fast at startup if neither a cert/key pair nor `control.tls.insecure=true`
   is set (CONFIGURATION.md Section 5.1).
2. **Reverse-proxy termination.** Terminate TLS at a reverse proxy / load
   balancer in front of the API and run the gRPC listener with
   `control.tls.insecure=true` on a trusted internal network. The proxy presents
   the certificate the Worker verifies; the API-to-proxy hop must stay on a
   private link. Use this when TLS material is managed centrally at the edge.

Plaintext without a fronting proxy (`control.tls.insecure=true` exposed directly)
is local/dev only and logs a `WARN` at startup.

---

## 3. Message envelopes and correlation

Both directions wrap their payload in a `oneof` envelope so the single stream
can multiplex many message types and grow new ones without a new RPC. Every
envelope carries a `correlation_id` and a timestamp.

`correlation_id` traces a flow end to end (REQUIREMENTS.md NFR-OBS-1):

- An `ApiCommand` carries its own `command_id`. The matching `CommandResult`
  sets the enclosing `WorkerMessage.correlation_id` to that same `command_id`,
  so the API pairs a result to its command.
- `RegisterAck` echoes the `Register` message's `correlation_id`.
- Unsolicited events use a fresh `correlation_id` the API logs and traces.

```
WorkerMessage{correlation_id, emitted_at, oneof: Register | CommandResult | Event}
ApiMessage   {correlation_id, sent_at,    oneof: RegisterAck | ApiCommand}
```

`ApiCommand` itself is a second `oneof` (one command per message) carrying
`command_id` and the target `server_id`; `Event` is a `oneof` of the four event
kinds. Adding a command or event type is an additive `oneof` field — a
backward-compatible change under the module's `FILE` breaking rule.

---

## 4. Stream lifecycle

### 4.1 Connect and register

```
   Worker                                   API
     │  open Session stream                  │
     │ ───────────────────────────────────▶ │
     │  WorkerMessage{Register: id, caps}    │   (FR-WRK-1)
     │ ───────────────────────────────────▶ │
     │                                       │  add to Worker registry
     │   ApiMessage{RegisterAck: accepted,   │
     │              heartbeat_interval}      │   (FR-WRK-2)
     │ ◀─────────────────────────────────── │
     │                                       │
```

`Register` MUST be the first message on a fresh stream. It advertises the
Worker's id, version, and `WorkerCapabilities` (available drivers, capacity
hint, host resources) — the input to the API's greedy placement
(FR-WRK-1, FR-WRK-3). It also carries `held_servers`: each server whose working
set the Worker already holds in its persistent local scratch (the non-empty
per-server scratch dirs found at startup), tagged with the **generation** that
set is at — the authoritative store generation it was last hydrated from or
snapshotted to, persisted in the scratch. The API records this so it can skip the
destructive hydrate on a same-worker restart **only when the held generation is
fresh enough** — hydrating a live, newer set would unpack the last authoritative
snapshot over it and roll the world back, while a *stale* held set (e.g. an
A→B→A leftover scratch B has advanced past) must still hydrate (issue #763,
generalizing #696, see Section 5). Before advertising a held set's generation the
Worker structurally fsck's its region files (issue #834): a periodic running-id
snapshot makes the generation marker durable while the live world files are never
fsynced by the Worker, so a power loss can leave a durable gen-N marker next to a
torn local world. A held set whose region is torn is advertised at **generation 0**
— treated as stale, forcing the hydrate that recovers the consistent store copy
rather than booting the torn world. The fsck applies the single byte-precise
region rule (issue #927/#926 item 1): a *structurally sound* scratch left by a
crashed or non-gracefully-stopped 26.x server is **live-format** — its region
files carry the legitimate unpadded (non-4096-aligned) tail — so it now PASSES and
the Worker advertises its **held generation**. The #767 skip gate can then boot
that world directly, preserving the crashed server's progression, instead of a
forced gen-0 hydrate that would roll it back by up to a snapshot interval. Only a
*genuinely* torn scratch (a chunk overrunning EOF, an entry past EOF, a severed
prefix) falls back to generation 0. The fsck requires a quiesced working set
(regionfsck's safety contract), so the Worker's startup sequence runs the
container orphan sweep first to stop any live writers before scanning. A Worker that reports nothing held, or an
older Worker that does not set the field, hydrates as before. The
API answers `RegisterAck`: `accepted` plus the `heartbeat_interval` it expects
and the `transfer_deadline` that bounds one data-plane transfer Worker-side
(Section 5); on refusal, `accepted=false` with a `rejection_reason` and the API
closes the stream.

### 4.2 Steady state

```
   Worker                                   API
     │  Event{Heartbeat}  (every interval)   │   (FR-WRK-2)
     │ ───────────────────────────────────▶ │
     │  Event{StatusChange | LogLine |       │   (FR-MON-1..3)
     │        Metrics}                       │
     │ ───────────────────────────────────▶ │
     │                                       │
     │   ApiCommand{command_id, server_id,   │   (FR-SRV-2/5,
     │              start | stop | ...}      │    Section 6.9)
     │ ◀─────────────────────────────────── │
     │  CommandResult{correlation_id =       │
     │      command_id, success | error}     │   (NFR-OBS-1)
     │ ───────────────────────────────────▶ │
```

Commands and events interleave freely on the one stream. The API may have
several commands in flight; `command_id` keeps each result matched to its
command.

### 4.3 Heartbeat and liveness

The Worker emits `Event{Heartbeat}` every `heartbeat_interval` (returned in
`RegisterAck`). The API marks the Worker disconnected when heartbeats lapse past
its liveness window, `control.heartbeat_timeout_seconds` (CONFIGURATION.md
Section 5.1), which is set comfortably above the interval (FR-WRK-2).

### 4.4 Disconnect and reconnect

A stream ends on a clean close, a transport error, or a missed-heartbeat
timeout. On disconnect the API marks the Worker's servers accordingly
(FR-WRK-4); those servers can be (re)started on another eligible Worker after a
hydrate from authoritative storage. The Worker is responsible for reconnecting:
it opens a fresh `Session` and re-`Register`s from scratch, re-advertising its
capabilities. The control plane keeps **no** cross-stream session state —
each connect is a clean registration, matching the stateless, replaceable nature
of Workers (REQUIREMENTS.md Section 5.1, FR-WRK-4). The API's record of desired
state is authoritative and drives any catch-up commands after a reconnect.

---

## 5. Commands (API to Worker)

All commands are `ApiCommand` payloads with a `command_id` and `server_id`. Each
maps to a lifecycle operation, RCON forwarding, a data-plane trigger, or a
running-server file access.

| Command | Meaning | Result payload | Req. ref |
|---|---|---|---|
| `StartServer` | Launch the server (after a preceding hydrate). Carries the driver, JAR relpath, MC version (Worker picks the Java runtime), launch mode (`jar` is the default JAR launch; `forge-argsfile` launches Forge via its generated args file, running a supervised installer first when the working set is uninstalled), optional `memory_limit_bytes` (per-server memory ceiling; 0/unset = driver default; #706), and optional `cpu_millis` (soft CPU allocation in millicores; 0/unset = driver default; #723). | none | FR-SRV-2, FR-EXE-5, ARCHITECTURE.md Section 7.3 |
| `StopServer` | Stop the server; graceful stop triggers an event-driven snapshot. `force` skips graceful. | none | FR-SRV-2, FR-DATA-7 |
| `RestartServer` | Stop then start in place. | none | FR-SRV-2 |
| `ServerCommand` | Forward an RCON/console line. | `command_output` | FR-SRV-5 |
| `HydrateTrigger` | Pull the working set from the data plane before launch; carries the transfer URL + one-time token. | none | FR-DATA-4 |
| `SnapshotTrigger` | Push the working set back to the data plane (a running server's copy is bracketed save-off → async save-all → settle-wait → copy → save-on so the world is fully on disk and a region file is not captured torn — NOT `save-all flush`, whose synchronous main-thread flush crashed a live server via the watchdog, #693; a running snapshot that cannot quiesce — RCON down, save-off/save-all failing, or the save never settling — is refused `quiesce_unavailable` rather than packing a live world, #907). | none | FR-DATA-4, FR-DATA-7, Section 6.9 |
| `ReadFile` | Read a path from a running server's live working set. | `file_content` | Section 6.9, Section 7.2 |
| `EditFile` | Write a path in a running server's live working set. | none | Section 6.9, Section 7.2 |
| `ListFiles` | List a directory in a running server's live working set (read-only). | `file_listing` | Section 6.9, Section 7.2 |

Notes:

- **Hydrate / snapshot are triggers only.** The command carries a transfer URL
  and a short-lived token addressing the API's HTTP data plane; the Worker moves
  the bytes there, off this stream (REQUIREMENTS.md Section 5.2). The data-plane
  endpoint spec is STORAGE.md Section 8; Section 5.1 cross-references it.
- **File access rides the control plane** for *running* servers only; a stopped
  server's files are served from authoritative Storage by the API directly
  (Section 6.9). This covers `ReadFile`, `EditFile`, and `ListFiles` — a running
  server's browse view lists the live working set rather than the snapshot-stale
  authoritative copy. Path-traversal protection is enforced Worker-side
  (FR-FILE-4), realized by the Worker's `WorkingDir` Port (ARCHITECTURE.md
  Section 5.2). The control plane is for small interactive edits; bulk movement
  stays on the data plane (ARCHITECTURE.md Section 7.2).
- **`ListFiles` is a quick, inline command** answered on the receive loop like
  `ReadFile`/`EditFile` (no 5.1 trigger completion to await — it touches no data
  plane). The listing is non-recursive (immediate children) and read-only, and is
  bounded to a per-listing cap (the Worker clips a pathological directory and sets
  `file_listing.truncated`). Each `FileEntry` carries `name` / `is_dir` / `size`,
  the same shape the API's authoritative-Storage listing uses, so the running and
  at-rest sources unify into one response. History and rollback stay
  authoritative-only (versions exist only on the authoritative copy), so there is
  no live-version control-plane command.
- **Delete and config edit are not control-plane commands.** Server delete and
  config editing (FR-SRV-2) are API/Storage-side operations: deleting a running
  server is stop-then-delete, and config edits on a running server go through the
  `ReadFile` / `EditFile` file-access commands above (ARCHITECTURE.md
  Section 7.2).

### 5.1 Trigger completion semantics

A `HydrateTrigger` / `SnapshotTrigger` `CommandResult` reports the outcome of the
**whole data-plane transfer**, not just the dispatch of the trigger. The Worker
runs the transfer off the session's receive loop and emits the result only after
the bytes have moved (STORAGE.md Section 8):

- **Hydrate** — `success` means the working set is fully unpacked into the
  server's working dir. A `204` "no published working set" still counts as
  success: the Worker launches against an empty dir.
- **Snapshot** — `success` means the archive was uploaded **and** atomically
  published by the API. The Worker's upload returns only on the data plane's
  `204`, which the API sends after its proven-complete gate commits the staged
  transfer (STORAGE.md Section 8); a partial upload is aborted and surfaces as a
  failed result, never a success.

API-side orchestrations rely on this ordering. Hydrate-then-start (the API issues
`HydrateTrigger`, awaits its result, then `StartServer`) relies on the working
set being present before launch; the running-backup chain
(save-all → `SnapshotTrigger` → archive) relies on the snapshot being published
before it archives from authoritative Storage. A worker change that emitted the
trigger result before the transfer completed would silently break both flows —
the API would proceed against an unhydrated or unpublished working set with no
error.

The API hydrates **only when the Worker does not already hold a fresh-enough
working set**, not on every start (issue #763, generalizing #696). A same-worker
restart (the reconciler's same-worker re-dispatch, where the assigned Worker is
unchanged) starts on the Worker's **existing** working set when it is current: the
persistent scratch is the live, newer copy (snapshots are pushed *from* it), so a
hydrate there would clobber it with the last snapshot and roll the world back. The
API skips the hydrate when, and only when, the assigned Worker reported that
server in its `Register.held_servers` (Section 4.1) at a **generation at least the
authoritative store generation** — so a fresh/wiped/GC'd scratch (reported as not
held, or a Worker too old to report) AND a *stale* held set (a generation older
than the store) both still hydrate, rather than booting an empty world or starting
on stale leftover scratch. The store generation is a per-server counter the
authoritative Storage bumps on each `commit_snapshot` and stamps onto each hydrate
/ snapshot transfer, so the Worker's reported generation and the store's share one
number space. A fresh placement always hydrates: a first launch or a relocation
onto a different Worker must pull the authoritative working set (a server that
moved A→B→A returns via fresh placement, where A's leftover scratch is stale
because B advanced and snapshotted it).

### 5.2 Worker-side transfer deadline

The Worker bounds each data-plane transfer (the snapshot upload and the hydrate
download) with a per-transfer context deadline carried in `RegisterAck.transfer_deadline`
(Section 4.1, issue #874). Without it a transfer has no deadline at all — the
HTTP client carries no timeout and the trigger's context none either — so a
stalled upload could outlive the API's `snapshot_timeout_seconds` indefinitely,
the exact case the API-side timeout-hold (issue #869) recovers from. The bound
structurally closes it: once it fires Worker-side, no late publish can exist.

The API derives the deadline as `max(hydrate_timeout_seconds, snapshot_timeout_seconds)
+ margin`, so it is always **>=** the API budget. The API-side dispatch timeout
therefore fires first on a genuinely slow-but-healthy transfer; the Worker bound
is the cleanup backstop, not the primary deadline, and never kills a transfer the
API still considers in flight. A non-positive or unset value (an older API) leaves
the transfer unbounded, the prior behavior.

---

## 6. Events (Worker to API)

All events are `Event` payloads, scoped by `server_id` (empty for Worker-wide
events such as a heartbeat).

| Event | Meaning | Req. ref |
|---|---|---|
| `StatusChange` | Observed server-state transition; the Worker reports observed state, the API holds desired state. | FR-SRV-4, FR-MON-1 |
| `LogLine` | One line of server console output (stdout/stderr). | FR-MON-2 |
| `Metrics` | Basic runtime metrics (CPU, memory, player count; best-effort). | FR-MON-3 |
| `Heartbeat` | Periodic liveness signal. | FR-WRK-2 |

`ServerState` enumerates the observed states. REQUIREMENTS.md FR-SRV-4 names
running / stopped / starting / crashed; the contract adds the transient
`STOPPING` and `RESTARTING` states the lifecycle commands pass through, so a
client sees an accurate live state during a transition. The `ServerState` enum
is the full set of values a Worker can report; the API caches the last-reported
value in `observed_state` ([`DATABASE.md`](DATABASE.md)). The `unknown` value
that column also allows is an API-side inference (set when the owning Worker
disconnects), never reported by a Worker, so it is deliberately outside this wire
contract and the enum has no `UNKNOWN` value.

Real-time delivery is best-effort end to end: if the control plane is down the
API's REST endpoints still function and clients simply miss live updates
(FR-MON-4). That degradation is API-side behaviour, not part of this contract.

---

## 7. Error reporting

A `CommandResult` with `success=false` carries a `CommandError`: a
`CommandErrorCode` for programmatic handling and a human-readable `message` for
logs and operators (REQUIREMENTS.md NFR-OBS-1). The codes cover the failure
classes a Worker can hit:

| Code | When |
|---|---|
| `SERVER_NOT_FOUND` | The target server is unknown to this Worker (no live instance: stop/restart/command on a not-running server, or a missing file target). |
| `INVALID_STATE` | The command is invalid for the current settled state (e.g. start or hydrate a running server, or a server with a failed-stop orphan pending termination). |
| `BUSY` | Another mutating lifecycle command is already in flight for the server, so this one was refused without being applied (the reservation race, issue #824). Distinct from `INVALID_STATE`: the in-flight command's outcome is not yet known, so the API keeps the assignment/intent and retries on a later tick rather than converging an observed state. |
| `DRIVER_UNAVAILABLE` | The requested execution driver is not offered by this Worker. |
| `FILE_ACCESS_DENIED` | A file path was rejected. A refining `file_access_reason` (Section 7.2) splits the distinct conditions so the API maps each to an honest HTTP reason instead of one blanket `invalid_path`. |
| `TRANSFER_FAILED` | A hydrate/snapshot data-plane transfer failed. |
| `PORT_CONFLICT` | A `StartServer` could not publish a host port already in use (the container driver classifies it from the Docker daemon's error message). |
| `IMAGE_MISSING` | A `StartServer` could not find or pull the server's container image (the container driver classifies it from the Docker daemon's error message). |
| `INTERNAL` | An unclassified failure applying the command. |

A registration refusal is reported differently: `RegisterAck.accepted=false`
with a `rejection_reason`, since it predates any command.

### 7.1 Command-error contract (binding)

Which code the Worker emits for a given command kind and instance precondition
is pinned, as data, by
[`proto/contract/command_error_contract.json`](../../proto/contract/command_error_contract.json)
— the single source of truth shared across both languages. The table above
classifies the codes; the JSON binds the exact `(kind, precondition) -> code`
rows. Two table-driven tests hold both sides to it (issue #204):

- the Worker test
  (`worker/internal/application/instancemanager/contract_test.go`) drives the
  instancemanager into each precondition and asserts the emitted code equals the
  table, so a code change without a table update fails the Worker suite;
- the API test (`api/tests/servers/test_command_error_contract.py`) asserts every
  `CommandStatus` the API's convergence / special-case logic matches on is a
  `(kind, code)` the table says the Worker actually emits, so an API match on a
  code the Worker never produces (the #202 incident) fails the API suite.

Add a new convergence match or change a Worker emission only together with the
table; the asymmetry is intentional — drift on either side fails that side's CI.

### 7.2 File-access reason (issue #548)

`FILE_ACCESS_DENIED` is an umbrella for several conditions, only one of which is a
path-syntax problem. The Worker carries which one in `CommandError.file_access_reason`
(a `FileAccessReason` enum) so the API surfaces an honest HTTP reason instead of
collapsing every file denial into a misleading `invalid_path`. The field is
additive: `UNSPECIFIED` is the proto3 default, so an older Worker that never sets
it — and a genuine path denial — both keep the historical behaviour.

| `file_access_reason` | Worker condition (read / edit / list) | API result |
|---|---|---|
| `UNSPECIFIED` | A traversal-unsafe path (absolute, `..`), or an unrefined resolution denial. | 422 `invalid_path` |
| `IS_A_DIRECTORY` | A read or edit whose path is a directory. | 422 `is_a_directory` |
| `NOT_A_DIRECTORY` | A list whose path is a regular file. | 422 `not_a_directory` |
| `SYMLINK_REFUSED` | A refused final/intermediate-component symlink (the FR-FILE-4 escape-vector defence). | 422 `symlink_refused` |
| `PAYLOAD_TOO_LARGE` | A read result or an edit payload past the control-plane file cap. | 413 `file_too_large` |

The reason refines only `FILE_ACCESS_DENIED`; it is `UNSPECIFIED` for every other
code. The HTTP mapping lives at the file routes
(`api/src/mc_server_dashboard_api/servers/api/files.py`).

---

## 8. Requirement mapping

| Requirement | Where in the contract |
|---|---|
| Section 5.1 (one Worker-initiated bidi stream) | `WorkerService.Session`; `Register` is the first Worker message (Section 2, Section 4.1) |
| FR-WRK-1 (register + advertise capabilities) | `Register`, `WorkerCapabilities`, `HostResources` (Section 4.1) |
| FR-WRK-2 (liveness via heartbeat) | `Event{Heartbeat}`, `RegisterAck.heartbeat_interval` (Section 4.3) |
| FR-WRK-3 (greedy placement input) | `WorkerCapabilities.drivers` / `max_servers` / `resources` |
| FR-WRK-4 (replaceable Worker; reconnect) | Stateless reconnect + re-register (Section 4.4) |
| FR-SRV-2 (start/stop/restart) | `StartServer`, `StopServer`, `RestartServer` (Section 5) |
| FR-SRV-4 (observed runtime state) | `StatusChange`, `ServerState` (Section 6) |
| FR-SRV-5 (RCON/server command forwarding) | `ServerCommand` → `command_output` (Section 5) |
| FR-DATA-4 / FR-DATA-7 (hydrate/snapshot triggers) | `HydrateTrigger`, `SnapshotTrigger` (triggers only; Section 5) |
| Section 6.9 / Section 7.2 (running-server file access) | `ReadFile`, `EditFile`, `ListFiles` (Section 5) |
| FR-MON-1..3 (status/log/metrics) | `StatusChange`, `LogLine`, `Metrics` (Section 6) |
| FR-EXE-5 (Worker picks Java runtime) | `StartServer.minecraft_version`; no Java field from the API (Section 5) |
| NFR-OBS-1 (correlation IDs, error reporting) | `correlation_id`, `command_id`, `CommandError` (Section 3, Section 7) |

---

## 9. Related documents

| Doc | Covers |
|---|---|
| [`../REQUIREMENTS.md`](../REQUIREMENTS.md) | What v2 must do; the source of truth for scope. |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | The two planes (Section 4), the `ControlPlane`/`APIClient` Ports (Section 5), and file-access-on-control-plane (Section 7.2). |
| [`CONFIGURATION.md`](CONFIGURATION.md) | Control-channel ports, TLS material, and the heartbeat-timeout key (Sections 5.1, 6.1). |
| [`../../proto/README.md`](../../proto/README.md) | The buf module: install, lint, and conventions. The binding `.proto` contract lives there. |
