# Game Ingress Relay

> Status: **Implemented** (epic #659; Bedrock ingress epic #1540) · Audience:
> contributors to `api/`, `worker/`, `relay/`, `proto/`, `webui/`
>
> This document is the design for epic
> [#659](https://github.com/mmiura-2351/mc-server-dashboard-v2/issues/659):
> players join a server at `<slug>.<base_domain>` with no port number, the
> Worker's IP is never exposed, and a Worker behind NAT (no public IP, no
> inbound ports) can serve players. It specifies the new **relay** component,
> the dial-back **tunnel contract**, hostname routing, and session recording.
> Once the proto messages exist, the buf module under
> [`../../proto/`](../../proto/) is the binding contract and this document
> explains it; where they disagree, the `.proto` files win.

## Table of Contents

1. [Scope](#1-scope)
2. [Topology and components](#2-topology-and-components)
3. [Hostnames: slugs and DNS](#3-hostnames-slugs-and-dns)
4. [Join sequence](#4-join-sequence)
5. [Tunnel contract](#5-tunnel-contract)
6. [Relay-to-API contract](#6-relay-to-api-contract)
7. [Minecraft protocol handling at the relay](#7-minecraft-protocol-handling-at-the-relay)
8. [Session records and moderation](#8-session-records-and-moderation)
9. [Coexistence with the direct path](#9-coexistence-with-the-direct-path)
10. [Failure modes and blast radius](#10-failure-modes-and-blast-radius)
11. [Security posture](#11-security-posture)
12. [Operational requirements](#12-operational-requirements)
13. [Configuration](#13-configuration)
14. [Database changes](#14-database-changes)
15. [HTTP API and Web UI changes](#15-http-api-and-web-ui-changes)
16. [Decision log](#16-decision-log)
17. [Out of scope / future work](#17-out-of-scope--future-work)

---

## 1. Scope

Today players connect directly to the host running the Worker on
`server.game_port` (DEPLOYMENT.md), which requires the Worker host to have a
reachable address and an open inbound port. In the target topology — each user
runs a Worker on their own PC — that exposes the operator's home IP and forces
port-forwarding.

This design adds an ingress path with these properties (epic #659 outcomes):

- A player joins using **only a hostname** (`<slug>.<base_domain>`), no port.
- The **Worker's IP is never visible** to players; players only ever talk to
  the relay.
- A Worker **behind NAT** — no public IP, no inbound ports — can host playable
  servers. The Worker keeps its outbound-only posture (CONTROL_PLANE.md
  Section 2).
- A **vanilla Java client** connects as-is: no client mods, no SRV records, no
  manual configuration.
- `online-mode` authentication keeps working: the relay is a byte-level
  pass-through, and Minecraft's encryption and Mojang session auth remain
  end-to-end between client and server.
- Moderation does not degrade: the relay records player IP / claimed identity
  per session (Section 8), replacing the IP visibility the Minecraft server
  process loses.

The architecture is forced by the constraints: a vanilla client can only open
a plain TCP connection, so NAT traversal without client-side software is not
possible. The only shape that satisfies the epic is a **public relay** plus a
**Worker-initiated outbound tunnel**. The hostname-routing trick is that the
Minecraft handshake packet carries the hostname the player typed, so the relay
can route by hostname after reading one plaintext packet (the Minecraft
analogue of SNI; the same principle as Infrared / mc-router).

General-purpose OSS tunnels (frp, rathole, …) were considered and rejected:
they cannot route raw TCP by Minecraft hostname, so each server would need its
own public port — which contradicts the port-less goal — and they would add a
second credential/config surface. The relay is purpose-built and small.

---

## 2. Topology and components

```
                     *.mc.example.com  (wildcard DNS → relay IP)
                              │
                              ▼
                   ┌─────────────────────┐
 player ──TCP────► │  relay  (Go)        │ ◄──TLS tunnel conns── worker (NAT'd)
 :25565            │  - game listener    │       (outbound dial-back,
                   │  - tunnel listener  │        one per player session)
                   └──────────┬──────────┘
                              │ gRPC (TLS + relay credential)
                              ▼
                   ┌─────────────────────┐
                   │  api                │ ──existing gRPC stream──► worker
                   │  - RelayService     │      (TunnelDial command)
                   │  - session records  │
                   └─────────────────────┘
```

| Component | Role |
|---|---|
| **`relay/`** (new top-level Go module, sibling of `worker/`) | Public data path. Accepts player TCP on the game listener, parses the handshake, resolves the hostname via the API, accepts the Worker's dial-back on the tunnel listener, and splices the two connections. Reports sessions to the API. Holds no persistent state. |
| **`api/`** | Control plane and source of truth, as today. Gains a `RelayService` gRPC service (served on the existing gRPC listener), a `TunnelDial` command on the existing Worker stream, the `slug` column, the `game_session` table, and a retention prune loop. |
| **`worker/`** | Gains one new command handler: `TunnelDial` — dial the relay's tunnel endpoint, present a token, splice to the local game port. No new config, no new persistent connections, stateless as before. |

The relay is a **separate Go service** (owner decision, Section 16): all game
traffic is a data path, so it must not share a process with the API — an API
restart must not drop players, and Go matches the Worker toolchain and suits
a TCP splice engine. In the single-host compose deployment it runs as a new
`relay` service next to `api`; in the target topology it runs wherever a
public IP lives (naturally the API host, which is already public because
Workers dial into it).

The relay is **stateless** in the Section-3 terminology sense: its only state
is in-flight TCP sessions and a short-lived status cache. Restarting it drops
active player connections (players reconnect) and loses nothing else.

---

## 3. Hostnames: slugs and DNS

### DNS layout

One wildcard record, created once at deployment setup:

```
*.<relay.base_domain>    A/AAAA    <relay public IP>
```

e.g. `*.mc.example.com → 203.0.113.7`. Server create/delete/rename never
touches DNS; the hostname → server mapping lives entirely in the database.
The relay's game listener binds the Minecraft default port **25565**, which is
what makes the join port-less — no SRV records needed.

### The `slug` column

`server.slug` — `TEXT NOT NULL UNIQUE` (deployment-wide, unlike `name` which
is unique per community, because the hostname namespace is global).

- **Charset**: a valid lowercase DNS label — `^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$`
  (1–63 chars, no leading/trailing hyphen).
- **Auto-generated on create** (owner decision): `<word>-<word>-<NN>` from a
  small embedded wordlist plus two digits (e.g. `amber-falcon-42`). Retry on
  the (unlikely) uniqueness collision. Generation does not derive from
  `server.name` — names are free-form display text (often non-ASCII) and do
  not slugify reliably.
- **Renameable** by anyone holding `server:update`, via the server update
  endpoint. Validation: charset, global uniqueness (friendly 409), and a
  reserved-word list (`www`, `api`, `mail`, `relay`, `admin`, `ns1`, `ns2`,
  `mc`, …) to keep operational hostnames usable under the same domain.
- **Released on delete or rename and immediately reusable** (owner decision —
  no cooldown). Accepted risk: a stale hostname (old invite link) can resolve
  to a different owner's new server if the slug is re-claimed. Revisit if it
  bites; a cooldown table is a small additive change.
- **Backfill**: the migration generates slugs for existing rows.

### Matching at the relay

The relay normalizes the handshake's `server_address`: lowercase, strip a
trailing dot, strip Forge's `\0FML…\0` suffix markers. If the result ends with
`.<base_domain>`, the remaining label is the slug; anything else (raw IP,
unknown domain, multi-label prefix) is treated as unknown — and unknown
hostnames are **dropped silently** (no protocol response), so internet
scanners learn nothing.

---

## 4. Join sequence

The login path (`next_state = 2`), end to end:

```
player        relay              api                worker            mc server
  │ TCP :25565 │                  │                   │                  │
  ├───────────►│ ① handshake +    │                   │                  │
  │            │   Login Start    │                   │                  │
  │            │   (parse, buffer)│                   │                  │
  │            ├─ ② ResolveJoin ─►│                   │                  │
  │            │   (slug, ip,     │ ③ validate,       │                  │
  │            │    intent=LOGIN) │   mint token,     │                  │
  │            │                  ├─ TunnelDial ─────►│                  │
  │            │◄─ {TUNNEL,token}─┤   {token,endpoint,│                  │
  │            │                  │    tls}           │                  │
  │            │◄────── ④ TLS dial-back + token ──────┤                  │
  │            │   (tunnel listener)                  ├── ⑤ TCP dial ───►│
  │            │                  │                   │   127.0.0.1:port │
  │            ├─ ⑥ replay buffered bytes ───────────►├─────────────────►│
  │◄═══════════╪═ ⑦ byte splice ═════════════════════╪═════════════════►│
  │            ├─ ⑧ SessionStart ►│                   │                  │
  │            │   … play …       │                   │                  │
  │            ├─ ⑨ SessionEnd ──►│                   │                  │
```

1. The player's client connects (wildcard DNS lands on the relay). The relay
   reads the handshake packet and, for logins, the Login Start packet —
   both are always plaintext and uncompressed (Section 7) — and buffers the
   raw bytes. Read caps: 5 s, 1 KiB.
2. The relay calls `ResolveJoin(slug, player_ip, intent)` on the API.
3. The API validates: slug exists → server `observed_state = running` →
   assigned Worker online. On success it mints a **single-use token**
   (128-bit random, 10 s TTL), dispatches a `TunnelDial` command to that
   Worker over the existing control-plane stream, and returns
   `{decision: TUNNEL, token}` to the relay. On a stopped server it returns
   `{decision: STOPPED}` (relay answers in-protocol, Section 7); on no such
   slug, `{decision: NOT_FOUND}` (relay drops silently).
4. The Worker dials the relay's **tunnel listener** (TLS, outbound — NAT-safe)
   and presents the token (Section 5). The relay matches it to the waiting
   player connection. If no dial-back arrives within 10 s, the relay
   disconnects the player with an in-protocol reason.
5. The Worker connects its other end to the server container's published game
   port on loopback (`127.0.0.1:<game_port>`; when `driver.container.game_bind_ip`
   is `0.0.0.0` the loopback dial still works).
6. The relay replays the buffered handshake + Login Start bytes into the
   tunnel, so the Minecraft server sees a pristine client byte stream.
7. From here both directions are spliced verbatim. Minecraft's own encryption
   handshake and Mojang session auth run end-to-end through the splice —
   `online-mode` is untouched, and the relay could not read game traffic if it
   wanted to.
8. / 9. The relay batches `SessionStart` / `SessionEnd` reports to the API
   (Section 8).

Join latency cost: one relay→API round trip plus one Worker→relay TLS dial —
small messages, tens of milliseconds; imperceptible next to a Minecraft login.

The status-ping path (`next_state = 1`) follows the same resolve flow but is
relay-mediated and cached rather than blind-spliced — see Section 7.

---

## 5. Tunnel contract

**Model: one dial-back connection per player session** (frp-style), not a
multiplexed shared tunnel.

Rationale: a single multiplexed tunnel (yamux or hand-rolled framing) needs
userspace flow control and suffers TCP head-of-line blocking between players
on the same Worker. Per-session connections let the kernel's TCP stack do
flow control and congestion handling per player, isolate failures, and reduce
the relay and Worker to dumb `io.Copy` splices. The cost — one extra TLS
handshake per join, and one outbound NAT entry per active player — is
negligible at this system's scale (NFR-SCALE-1). There is **no persistent
Worker↔relay connection**; the only always-on channel remains the existing
Worker↔API gRPC stream.

**`TunnelDial`** — new `ApiCommand` payload on the existing control-plane
stream (CONTROL_PLANE.md Section 5):

| Field | Meaning |
|---|---|
| `server_id` | Which local server the session is for. The Worker resolves it to the running instance's published loopback game port; if the server is not running locally, it returns a `CommandResult` error. |
| `endpoint` | The relay tunnel endpoint to dial, `host:port` (from the relay's registration, Section 6). |
| `token` | The single-use session token to present. |
| `tls_ca_pem` | Optional PEM bundle to verify the relay's tunnel certificate against; empty means system roots (public CA). Delivered in-band so the **Worker needs zero new configuration** — it already trusts the API over the authenticated control channel. |

`TunnelDial` is a quick command: it **bypasses the Worker's slow-lane
concurrency cap** exactly like `ServerCommand` (issue #169) — a join must not
queue behind a hydrate.

**Dial-back handshake** (Worker → relay tunnel listener, after TLS):

```
"MCSD-TUNNEL/1\n" + token + "\n"
```

The relay matches the token against its table of waiting player connections
(consuming it — tokens are single-use), responds with `"OK\n"`, and starts the
splice. Unknown, expired, or reused tokens: the relay closes the connection
without a response. Tunnel connections that send nothing within 5 s are
dropped.

Idle policy: the relay applies no idle timeout of its own to spliced sessions
— the Minecraft protocol has keep-alives and the server kicks dead clients;
the relay just propagates the close from either side and half-closes the
other.

---

## 6. Relay-to-API contract

A new gRPC service in a new buf package (`mcsd.relay.v1`), served on the
API's **existing gRPC listener** (`server.grpc_port`) alongside
`WorkerService`. Same transport posture as the Worker (CONTROL_PLANE.md
Section 2): server-side TLS, shared-secret auth in call metadata — but a
**separate credential** (`relay.credential` on the API side), so relay and
Worker credentials rotate independently.

Unary RPCs (no persistent stream — the relay has no command inbox; everything
is relay-initiated):

```
service RelayService {
  rpc Register(RegisterRequest) returns (RegisterResponse);
  rpc ResolveJoin(ResolveJoinRequest) returns (ResolveJoinResponse);
  rpc ReportSessions(ReportSessionsRequest) returns (ReportSessionsResponse);
}
```

- **`Register`** — called at relay startup and on reconnect. Carries the
  relay's advertised tunnel endpoint (`tunnel.public_endpoint`), its tunnel
  CA PEM (if self-signed), and the set of session IDs still active on the
  relay. The API stores the endpoint/CA for use in `TunnelDial` commands and
  **closes any `game_session` rows that are open but absent from the active
  set** — this is how sessions orphaned by a relay crash get an `ended_at`.
  One relay per deployment at M-this; a second `Register` from a different
  relay instance simply replaces the stored endpoint (last-writer-wins).
- **`ResolveJoin`** — the per-connection routing decision described in
  Section 4. Request: `{slug, player_ip, intent: STATUS|LOGIN}`. Response:
  `{decision: TUNNEL|STOPPED|NOT_FOUND, token, server_id}` (`server_id` set
  on `TUNNEL`, carried into `SessionStart`) plus, for
  `STOPPED`, the display name to embed in the synthesized response. The API
  dispatches `TunnelDial` as a side effect of a `TUNNEL` decision; it does
  not wait for the Worker's `CommandResult` (the Worker's "result" is the
  dial-back arriving at the relay; the `CommandResult` is still reported and
  logged for diagnostics).
- **`ReportSessions`** — batched session lifecycle events, flushed every ~5 s
  or 100 events: `SessionStart {session_id, server_id, slug, player_ip,
  username, player_uuid?, started_at}` and `SessionEnd {session_id,
  ended_at}`. Idempotent upserts keyed on the relay-minted `session_id`
  (UUID), so retries after transient API errors are safe.

---

## 7. Minecraft protocol handling at the relay

The relay implements a deliberately tiny slice of the Java protocol — only
packets that are **always plaintext and uncompressed** (everything before
compression/encryption is negotiated):

| Packet | Direction | Use |
|---|---|---|
| Handshake (`0x00`): protocol version, server address, port, next state | read | routing (the hostname) and protocol-version awareness |
| Login Start (`0x00`, login state): name, UUID (version-dependent) | read + forward | session identity capture (Section 8) |
| Status Request / Response (`0x00`, status state), Ping/Pong (`0x01`) | speak | server-list pings |
| Login Disconnect (`0x00`, login state): JSON text reason | write | friendly errors |

Parsing is hardened: ≤1 KiB total before a routing decision, 5 s read
deadline, VarInt bounds checks; anything malformed is dropped without a
response.

**Status pings are relay-mediated, not blind-spliced.** Clients ping every
server in their saved list on every multiplayer-screen refresh; tunneling
each ping would hammer Workers with dial-backs. Instead the relay keeps a
per-slug **status cache** (default 5 s TTL):

- Cache hit → answer the Status Response and Pong locally; no API or Worker
  involvement.
- Cache miss, server running → resolve, tunnel, perform the status exchange
  itself (forward the buffered handshake + Status Request, read the Status
  Response), cache the JSON, answer, close. Players see the server's real
  MOTD, player count, and favicon, at most 5 s stale.

**Stopped servers answer in-protocol** (owner decision):

- Status ping → synthesized response: MOTD `"<name> — stopped. Start it from
  the dashboard."`, `version.protocol = -1` (renders as an incompatible
  server with the MOTD visible — the standard offline-placeholder trick),
  players 0/0.
- Join attempt → Login Disconnect with the same reason.

**Unknown slugs get nothing** — silent drop, no information for scanners.

Login Start parsing is **protocol-version-tolerant**: the name is always the
first field (≤16 chars); the UUID field exists only on newer protocols
(required since 1.20.2, optional in the 1.19.x range, absent before). The
relay parses by the handshake's protocol version, best-effort: `username`
recorded always, `player_uuid` when present. Unparseable login packets are
still spliced (routing already succeeded); the session is recorded with a
null username.

---

## 8. Session records and moderation

Behind the relay, the Minecraft server process sees every connection as
coming from the tunnel's local address, so **server-side IP bans stop
working**. UUID/name bans are unaffected (`online-mode` stays on). The epic
requires moderation not to degrade, so the relay becomes the IP-visibility
point (owner decision: record at the relay and surface in the dashboard; no
PROXY-protocol passthrough — it is Paper-only and would not cover vanilla).

New table **`game_session`** (see Section 14) populated via
`ReportSessions`. One row per accepted **login** session (status pings are
not recorded). `username` / `player_uuid` are the values *claimed* in Login
Start — pre-authentication. With `online-mode` on, an impostor fails Mojang
auth seconds later, so a session of meaningful duration implies a verified
identity; the docs and UI should still label the column "claimed identity"
honestly.

**Access control** (owner decision): a new permission **`session:read`**,
granted to the seeded Owner role by default (DATABASE.md role seeding). The
sessions endpoint (Section 15) requires it; members with only `server:read`
do not see session data at all. Player IPs are PII — this keeps them
role-restricted.

**Retention**: `relay.session_retention_days` (API config, default **90**).
A prune loop in the API (the standard background-loop pattern, e.g.
`reconciler_loop`) deletes rows older than the window. Rows also cascade away
when their server is deleted.

---

## 9. Coexistence with the direct path

The relay is **config-selectable, default off** (`relay.enabled`, API side) —
the epic's outcome for new deployments, but a domain + public relay is a real
prerequisite, so single-host operators keep the current behavior with zero
new setup (owner decision: keep the direct path as a fallback).

| | Direct path (today) | Relay path |
|---|---|---|
| `relay.enabled` | `false` (default) | `true` |
| Player address | `<worker host>:<game_port>` | `<slug>.<base_domain>` |
| `driver.container.game_bind_ip` | `0.0.0.0` (compose default) | keep the worker default `127.0.0.1` — no inbound game port at all |
| Host firewall | game-port range open | nothing inbound on the Worker |

The two paths are not mutually exclusive at the protocol level (a server can
be reachable both ways during migration); `relay.enabled` governs whether the
relay control surface (RelayService, slug display in the UI) is active.
`game_port` allocation (`ports.py`) is unchanged in both modes — the relay
path still uses it as the container's published loopback port that the Worker
dials.

**Single-host caveat:** when the relay and the Worker run on the same host, the
relay's `0.0.0.0:25565` bind conflicts with the default game-port range
(`25565..25664`); set `ports.range_start` to `25566` (or higher) to avoid the
collision — see `docs/dev/DEPLOYMENT.md` "Relay — Single-host port collision".

---

## 10. Failure modes and blast radius

| Failure | Effect | Recovery |
|---|---|---|
| **Relay restart/crash** | All active player sessions drop (every session's splice lives in the relay). Worst blast radius in the design — accepted for one relay; multiple relays are future work. | Players reconnect; tunnels re-establish per session. Orphaned open `game_session` rows are closed at the next `Register`. |
| **API down** | Existing sessions unaffected (splices are relay↔worker, no API in the data path). New joins fail: relay disconnects with an in-protocol "try again shortly" reason. Status answered from cache while it lasts; cache-miss pings get the stopped-style response with a "dashboard unavailable" MOTD. | Relay retries gRPC with backoff; service resumes when API returns. |
| **Worker process dies mid-session** | Its tunnel connections die with it, so its players drop — even though the MC containers keep running (tunnel sockets are owned by the Worker process). Same blast-radius class as today's worker-restart behavior. | Players rejoin after the Worker reconnects and the orphan sweep settles. |
| **Worker never dials back** (crashed between command and dial, token expired) | Relay times out at 10 s, player gets Login Disconnect "could not reach the server". The Worker's `CommandResult` error (if any) is logged API-side. | Player retries. |
| **Slug renamed mid-session** | No effect; the hostname is only consulted at join time. | — |
| **Stale hostname after delete + slug re-claimed** | Players using an old link land on the new claimant's server (immediate-reuse decision, Section 3). | Operator-level concern; revisit with a cooldown if it bites. |

---

## 11. Security posture

- **Game listener** is internet-exposed and unauthenticated by nature (the
  Java protocol has nothing before Login). Mitigations: silent-drop for
  unknown hostnames and malformed traffic, parse byte/time caps, per-IP
  concurrent-connection cap (default 32) and per-IP join-rate cap (default
  10/s) — both config. This is hygiene, not DDoS protection; volumetric
  defense is out of scope (Section 17).
- **Tunnel listener** is TLS; a connection must present a valid single-use
  128-bit token within 5 s or it is dropped without a response. Tokens are
  minted by the API, bound to one resolve, and expire in 10 s, so the
  listener offers no unauthenticated surface beyond a TLS handshake. It also
  carries a per-IP concurrent-connection cap (default 64, config) bounding how
  many pre-auth handshake windows one source IP can hold; over the cap is a
  silent close.
- **Relay↔API** matches the Worker posture: server-side TLS on the gRPC
  listener + a dedicated shared secret (`relay.credential`), constant-time
  compared (NFR-SEC-1). mTLS rides the existing deferred plan
  (CONTROL_PLANE.md Section 2) and will cover this channel when it lands.
- **Game payload privacy**: after Login Start, Minecraft's protocol
  encryption is end-to-end client↔server; the relay relays ciphertext and
  never holds key material.
- **PII**: player IPs exist in exactly two places — relay memory (in-flight)
  and the `game_session` table (retention-pruned, permission-gated). The
  relay logs IPs at debug level only.

---

## 12. Operational requirements

**`prevent-proxy-connections=false`** (the default) must remain set in each
backend server's `server.properties` when running behind the relay.

The relay proxies player connections, so the Minecraft server sees the
**relay's IP** as every player's source address, not the player's real IP.
When `prevent-proxy-connections=true`, the server sends that source IP to
Mojang's `hasJoined` session endpoint for verification; Mojang compares it
against the IP the client originally authenticated from. Because these two
IPs differ (relay vs. player), **online-mode authentication fails** and
players are kicked.

The default value is `false`, so most vanilla servers work out-of-the-box.
Operators only hit this issue if they have explicitly enabled the property.

---

## 13. Configuration

Follows CONFIGURATION.md conventions: TOML + env override
(`MCD_RELAY_*` for the new binary), secrets via env, fail-fast on invalid
config, wiring at the edge.

**API — new `[relay]` section:**

| Key | Default | Meaning |
|---|---|---|
| `relay.enabled` | `false` | Master switch: serve RelayService, expose `join_hostname`, run the prune loop. |
| `relay.credential` | — (required when enabled; secret) | Shared secret the relay presents. |
| `relay.base_domain` | — (required when enabled) | e.g. `mc.example.com`; used to build `join_hostname` and validate registration. |
| `relay.session_retention_days` | `90` | `game_session` prune window. |

**Relay binary — `relay.toml`:**

| Key | Default | Meaning |
|---|---|---|
| `api.grpc_endpoint` / `api.credential` / `api.tls.{ca_file,insecure}` | — | Same shape as the Worker's API connection config. |
| `game.listen` | `:25565` | Public player listener. |
| `game.status_cache_seconds` | `5` | Status-ping cache TTL. |
| `game.status_cache_max_entries` | `1024` | Maximum entries in the per-slug status-ping cache; oldest entry is evicted when the limit is exceeded. Must be positive. |
| `game.max_conns_per_ip` / `game.joins_per_ip_per_second` | `32` / `10` | Hygiene caps (Section 11). |
| `tunnel.listen` | `:25665` | Worker dial-back listener (TLS). |
| `tunnel.max_conns_per_ip` | `64` | Per-IP concurrent-connection cap on the tunnel listener (Section 11). |
| `tunnel.public_endpoint` | — (required) | `host:port` advertised to Workers via `Register` → `TunnelDial`. |
| `tunnel.tls.cert_file` / `tunnel.tls.key_file` | — (required) | Tunnel listener TLS material. Self-signed is fine: the matching CA PEM travels `Register` → API → `TunnelDial` → Worker verification. |
| `tunnel.tls.advertised_ca_file` | — (derive from `cert_file`) | CA bundle advertised to Workers for verifying the tunnel cert. Unset → derive from `cert_file` (self-signed). `system` → advertise empty (Workers use system roots; for a publicly-issued cert). A path → advertise that PEM. |
| `bedrock.enabled` | `false` | Master switch for the Bedrock QUIC/UDP tunnel listener (issue #1584). Off by default so a Java-only relay neither binds nor requires the Bedrock UDP ports below on upgrade. Wired from the same operator setting as the API's `relay.bedrock_enabled`, via `MCD_RELAY_BEDROCK_ENABLED` (compose sets it from `MCD_API_RELAY__BEDROCK_ENABLED`). |
| `bedrock.tunnel_listen` | `:25675` | Bedrock QUIC tunnel listener (RFC 9221 DATAGRAM) — see [`BEDROCK_TUNNEL.md`](BEDROCK_TUNNEL.md). Reuses `tunnel.tls.{cert_file,key_file}` with a distinct ALPN; no separate cert/key config. Bound only when `bedrock.enabled` is true. |
| `bedrock.tunnel_max_conns_per_ip` | `64` | Per-IP concurrent cap on unauthenticated handshake windows on the Bedrock QUIC listener, mirroring `tunnel.max_conns_per_ip` (Section 11) — see `BEDROCK_TUNNEL.md` Section 8. |
| `bedrock.max_flows_per_ip` / `bedrock.new_flows_per_ip_per_second` | `32` / `10` | Hygiene caps on a bound Bedrock `bedrock_port`, same posture as `game.max_conns_per_ip` / `game.joins_per_ip_per_second` — see `BEDROCK_TUNNEL.md` Section 8. |
| `log.level` / `log.format` | as Worker | Standard logging keys. |

**Worker: no new configuration.** Everything a `TunnelDial` needs arrives in
the command.

**Compose**: a new `relay` service (image `mcsd-relay:dev`) joining the
`mcsd` network, publishing `25565` and `25665`; with the object-store profile
precedent, gate it behind a `relay` profile so default bring-up is unchanged.

---

## 14. Database changes

Migration `0016_relay_ingress` (shipped with issue #955) adds the slug column.
`game_session`, permission seeding, and further relay infrastructure arrive in
a later migration with issue #957.

- **`server.slug`** — `TEXT NOT NULL`, `UNIQUE` deployment-wide, backfilled
  with generated slugs for existing rows. (Distinct from the per-community
  `UNIQUE(community_id, name)` on display names.)
- **`game_session`** — new table (issue #957):

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | Relay-minted session id (idempotency key). |
| `server_id` | UUID FK → `server.id` `ON DELETE CASCADE` | Indexed; sessions die with the server. |
| `hostname` | TEXT | The slug actually used at join (slugs are renameable; this is the historical value). |
| `player_ip` | INET | The player's source address as seen by the relay. |
| `username` | TEXT NULL | Claimed in Login Start (Section 8). |
| `player_uuid` | UUID NULL | Claimed; present on protocols that send it. |
| `started_at` | timestamptz | |
| `ended_at` | timestamptz NULL | NULL = still open (or relay crashed; healed on `Register`). |

- **Permission seeding**: add `session:read` to the seeded Owner role
  (DATABASE.md role model). Existing deployments: the migration appends
  `session:read` to roles named `Owner` that hold the full M1 permission
  set, mirroring how previous permission additions were rolled out.

---

## 15. HTTP API and Web UI changes

**API surface:**

- `ServerResponse` gains `slug` and `join_hostname` (`<slug>.<base_domain>`
  when `relay.enabled`, else `null` — the UI's display switch).
- Server update (`PATCH`) accepts `slug` (permission `server:update`;
  422 invalid / 409 taken).
- `GET /api/communities/{cid}/servers/{sid}/sessions` — paginated, newest
  first, requires **`session:read`**. Returns hostname, IP, claimed
  username/UUID, start/end.

**Web UI:**

- Server detail header: show `join_hostname` with a copy button when present
  (today it shows only `:game_port`); fall back to the current port display
  otherwise.
- Settings tab: slug rename field with inline validation.
- Players tab: a "Sessions" view (the moderation surface), rendered only for
  holders of `session:read`.

---

## 16. Decision log

Owner decisions, 2026-06-12 (issue #659 comments):

| Decision | Choice | Rejected alternative |
|---|---|---|
| DNS | Single wildcard record; hostname mapping in DB | Per-server DNS records via provider API (propagation waits, more failure modes) |
| Player-IP moderation | Record at relay, surface in dashboard | PROXY protocol passthrough (Paper-only, misses vanilla) |
| Slug origin | Auto-generated, owner-renameable | User-mandatory at create; ID-derived fixed |
| Direct path | Kept, config-selectable; relay default-off | Tunnel-only (forces domain ownership on single-host operators) |
| Relay placement | Separate Go service | In-API asyncio (couples data path to control plane, API restart drops players); separate Python service |
| Stopped-server UX | In-protocol status MOTD + disconnect reason | Silent drop (indistinguishable from a typo) |
| Slug reuse | Immediate on release — accepted stale-link risk | 30-day cooldown; permanent reservation |
| IP visibility | New `session:read` permission, Owner-seeded | Bundled into `server:read` |

Design-level choices made here (not owner-arbitrated, recorded for review):
per-session dial-back over a multiplexed tunnel (Section 5); API-mediated
join signaling over a persistent relay↔worker channel (keeps the Worker's
single control connection and zero new Worker config; cost: API must be up
for *new* joins); relay-mediated status pings with a 5 s cache (Section 7).

## 17. Out of scope / future work

- **Multiple relays / geo placement** — removes the single-relay blast
  radius and shortens player RTTs. The contract already isolates the relay
  (register + resolve + report), so adding instances is additive.
- **Volumetric DDoS protection** — upstream/provider concern.
- **Bandwidth quotas / accounting per server** — the relay is the natural
  metering point; not designed here.
- **PROXY protocol to Paper-family servers** — would restore native IP bans
  on servers that support it; revisit on demand.
- ~~**Bedrock** (UDP/RakNet) — the application targets Java (epic scope).~~
  **In scope as of epic [#1540](https://github.com/mmiura-2351/mc-server-dashboard-v2/issues/1540).**
  Bedrock rides a separate QUIC/DATAGRAM tunnel and public UDP ingress, not
  this document's TCP tunnel contract — see
  [`BEDROCK_TUNNEL.md`](BEDROCK_TUNNEL.md).
- **SRV-based custom domains** (player-owned domains pointing at the relay)
  — possible later; routing already keys on the full hostname.
- **Observability / metrics** — the relay exposes no Prometheus metrics or
  tracing yet; add on demand.
- **Graceful drain on shutdown** — restart drops in-flight sessions rather
  than draining them; a drain window is future work.
