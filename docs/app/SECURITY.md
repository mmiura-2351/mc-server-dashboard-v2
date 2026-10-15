# Security

> Status: **Design** · Audience: contributors to `api/`
>
> This document defines the authentication-hardening behaviour the API enforces
> for [`REQUIREMENTS.md`](../REQUIREMENTS.md) FR-AUTH-4: password-policy
> semantics, the brute-force / lockout algorithm, trusted-proxy client-IP
> resolution, and — the decision this document exists to record — **where the
> brute-force / lockout runtime state lives**. It refines, but does not
> contradict, the requirements, [`ARCHITECTURE.md`](ARCHITECTURE.md),
> [`DATABASE.md`](DATABASE.md), and [`CONFIGURATION.md`](CONFIGURATION.md); where
> they disagree, the requirements win and this document is wrong.
>
> **Scope.** Authentication-hardening behaviour, the unauthenticated endpoints in
> [Section 5](#5-unauthenticated-endpoints) (`/api/healthz`, `/api/readyz`, the
> OpenAPI schema and docs pages, and the separate Prometheus listener), plus
> [Section 6](#6-minecraft-server-container-trust-model), which records what a
> Minecraft server container is trusted to do, which of the two docker networks
> each service is attached to, and what is reachable from where. This document
> specifies the values and their semantics; the tunable keys and their defaults
> are carried by [`CONFIGURATION.md`](CONFIGURATION.md) Section 7 and are
> referenced, not duplicated, here. Token issuance/verification (FR-AUTH-2) and
> password hashing (FR-AUTH-3) are separate concerns owned by the `TokenService`
> and `PasswordHasher` Ports ([`ARCHITECTURE.md`](ARCHITECTURE.md) Section 5.1).
> The FR-AUTH-4 bullets are binding.

## Table of Contents

1. [Password policy](#1-password-policy)
2. [Brute-force protection](#2-brute-force-protection)
3. [Lockout-state home (decision)](#3-lockout-state-home-decision)
4. [Trusted-proxy IP resolution](#4-trusted-proxy-ip-resolution)
5. [Unauthenticated endpoints](#5-unauthenticated-endpoints)
6. [Minecraft server container trust model](#6-minecraft-server-container-trust-model)
7. [Related documents](#7-related-documents)

---

## 1. Password policy

On registration and password change the API rejects a password that fails any
enabled rule. The strength rules are selected by a named **preset** —
`auth.password.policy` is `low`, `middle`, or `high`
([`CONFIGURATION.md`](CONFIGURATION.md) Section 7.1). A preset only changes
*which* rules fire and their thresholds; the reason codes below stay identical
across presets, so the Web UI error mapping is unaffected.

The candidate rules:

- **Length** — at least the preset minimum and at most `max_length` characters.
  When `auth.password.hash=bcrypt` the effective upper bound is the smaller of
  `max_length` and 72 UTF-8 bytes (bcrypt ignores bytes past 72, so a longer
  password is rejected at the policy with reason `too_long_for_bcrypt`); the
  argon2 default has no such byte cap. `max_length` is otherwise a DoS guard and
  is independent of the preset.
- **Complexity-or-length** — at least *N* of {upper, lower, digit, symbol}
  **or** at least 16 characters, where *N* is the preset's class count (`middle`
  = 2, `high` = 3; `low` does not enforce this rule). Whitespace counts toward
  the symbol class, so passphrases with spaces get the credit.
- **Common-password blocklist** — reject passwords on the bundled blocklist:
  SecLists `Passwords/Common-Credentials/10k-most-common.txt` (the
  xato-net-derived 10,000 most common passwords), packaged with the API as
  `common_passwords.txt` and matched case-insensitively.
- **User-info rejection** — reject a password containing the username or the
  email local-part.
- **Simple-pattern rejection** — reject 4+ repeated characters or 4+ sequential
  alphabet/keyboard/numeric runs.

### Strength presets

| Preset | Min length | Complexity-or-length | Common-list | User-info | Simple-pattern |
|---|---|---|---|---|---|
| `low` | 8 | off | on | on | off |
| `middle` *(default)* | 10 | 2 of 4 (or 16+ chars) | on | on | on |
| `high` | 12 | 3 of 4 (or 16+ chars) | on | on | on |

The default is `middle`. The preset applies only to the validation of *newly
set* passwords (registration and password change); existing password hashes are
unaffected by a preset change. Operators who want the stricter posture set
`auth.password.policy=high` explicitly. The preset is deployment configuration
only; no admin-UI selector is provided.

Policy is pure, deterministic domain logic: it depends on no persistent state and
sits in the domain layer, callable from the registration and password-change use
cases.

### Reason codes

A rejected password yields a `422` problem+json response carrying a stable,
machine-readable `reason` (the RFC 9457 body shape and the `reason` extension
member are defined in [`AUTH_API.md`](AUTH_API.md) Section 2). The policy
evaluates the rules in order and reports the **first** rule that fails, so only
one `reason` is returned per request. These codes are emitted by the three
endpoints that run the policy — registration (`POST /users`), self-service
password change (`PUT /users/me/password`), and admin user creation
(`POST /admin/users`):

| `reason` | Trigger |
|---|---|
| `too_short` | Fewer than the preset's minimum length. |
| `too_long` | More than `max_length` characters (the DoS-guard upper bound). |
| `too_long_for_bcrypt` | More than 72 UTF-8 bytes when `auth.password.hash=bcrypt` (bcrypt ignores bytes past 72); never raised under argon2. |
| `insufficient_complexity` | Fewer than the preset's class count of {upper, lower, digit, symbol} **and** fewer than 16 characters. Not raised by `low`. |
| `common_password` | On the common-password blocklist, matched case-insensitively (every preset screens the list). |
| `contains_user_info` | Contains the username or the email local-part, matched case-insensitively. |
| `simple_pattern` | Contains 4+ repeated characters or a 4+-long sequential run. Not raised by `low`. |

### Re-authentication for destructive self-service actions

Both self-service actions that are irreversible or revoke every session require
the caller to re-supply their current password, verified against the stored hash
before the action proceeds: password change (`PUT /users/me/password`, the
`current_password` field) and account deletion (`DELETE /users/me`, the
`password` field). A wrong current password returns the same uniform
`401 invalid_credentials` that login returns — never a distinct "wrong password"
signal — so neither endpoint can be used as a password-confirmation oracle. This
re-auth check sits behind a valid access token and is therefore *not* fed into
the Section 2 brute-force counters (those defend the unauthenticated login
surface against username enumeration).

---

## 2. Brute-force protection

The API counts authentication failures over sliding windows and locks an account
after too many, with exponential back-off (FR-AUTH-4). All values below are
configured under `auth.brute_force.*` ([`CONFIGURATION.md`](CONFIGURATION.md)
Section 7.2); the algorithm is:

1. **Record** every authentication attempt (username, source IP, success flag,
   timestamp).
2. **Per-username window** — count failures for the username within
   `username_window_seconds`. At `username_threshold` the account is
   locked.
3. **Per-IP window** — count failures from the source IP within
   `ip_window_seconds`. At `ip_threshold` the source IP is
   throttled. This depends on a trustworthy client IP (Section 4).
4. **Lockout with exponential back-off** — the lockout duration starts at
   `lockout_base_seconds` and doubles on each repeat lockout of the same
   account, capped at `lockout_max_seconds`. A per-account historic
   lockout count drives the doubling.
5. **Artificial failure delay** — every failed attempt incurs the
   `delay_ms` delay so a caller cannot distinguish "no such user" from
   "wrong password" by timing, denying username enumeration.

A successful authentication clears the active lockout and resets the back-off for
that account.

Two of these checks run *before* the password is verified: step 3's per-IP count,
and a lookup of the lockout record that steps 2 and 4 produced. (Step 2's
per-username counting itself runs after a failure, when deciding whether to lock.)
A login rejected by either pre-verification check keeps the uniform `401` — the
status and body never distinguish it from a wrong password — but adds a
`Retry-After` header telling the client how long to wait: the full
`ip_window_seconds` for the IP throttle, the remaining lockout for a locked
account. See [`AUTH_API.md`](AUTH_API.md) Section 1 for the header contract.

This algorithm needs **runtime state** that outlives a single request: the
attempt records that the sliding windows count over, and the per-account lockout/back-off
record. Where that state lives is decided in Section 3.

---

## 3. Lockout-state home (decision)

[`DATABASE.md`](DATABASE.md) Section 4 deliberately omits this state from the
core entity model, delegating the storage decision to this document. The
brute-force / lockout state is auth-hardening runtime state, not a core domain
entity; this section decides its home and keeps it consistent with that note.

**Decision.** Brute-force / lockout state is persisted in the **relational
database** (the same PostgreSQL instance as the core model,
[`DATABASE.md`](DATABASE.md) Section 1), in two dedicated auth-hardening tables
kept **separate from the core entity model**, behind an API-side Port,
`LoginAttemptStore` (naming per [`ARCHITECTURE.md`](ARCHITECTURE.md) Section 6).
Business logic depends only on the Port; the adapter is the DB-backed
implementation, bound at the edge.

The two tables:

- **`login_attempt`** — append-only record of each authentication attempt
  (username, source IP, success flag, failure reason, timestamp). The sliding
  windows of Section 2 are `COUNT` queries over this table within the window
  bound; an index on `(username, created_at)` and on `(ip, created_at)` serves
  them.
- **`account_lockout`** — at most one row per username, holding the active
  lockout (`locked_until`) and the historic lockout count that drives the
  exponential back-off.

The open-registration per-IP cap
([`CONFIGURATION.md`](CONFIGURATION.md) Section 7.4) reuses the **same**
`login_attempt` table and `(ip, created_at)` index rather than a parallel
mechanism: a registration is recorded as
a row marked so it is isolated from the login failure counts, and the per-IP cap
is a `COUNT` over those marked rows within its window. The same prune triggers age
the rows out, and they fold the registration window into their horizon (below) so a
marked row survives its full window rather than being pruned at the login horizon.

Because these are auth-hardening state and not part of the core graph, they are
specified here rather than in [`DATABASE.md`](DATABASE.md), and they do not
participate in the core cascade rules. Column-level detail lives in the schema
(the API's Alembic migrations); this document fixes only their existence,
purpose, and the Port seam.

**Cleanup.** `login_attempt` is append-only and grows without bound otherwise, so
rows older than the longest configured sliding window are pruned through two
triggers, both using that same bound. The bound is the longest of the enabled
counters' windows — the per-username and per-IP login windows (Section 2) and,
when the open-registration per-IP cap is enabled, its window too — so registration
rows in the shared table are not pruned before their wider window elapses:

- **On a successful login** — the login use case prunes after clearing the
  lockout. Cheap and bounded, but it only fires for accounts that eventually
  succeed.
- **A periodic background loop** — a lifespan task on the API runs the prune on a
  fixed cadence (`auth.brute_force.prune_interval_seconds`,
  [`CONFIGURATION.md`](CONFIGURATION.md) Section 7.2), independent of any login.
  This closes the gap the on-success trigger leaves: a failures-only attack
  against an account that never logs in would otherwise grow the table unbounded.
  The loop drives only the database, so it runs on every API process regardless of
  the control plane.

`account_lockout` is bounded (one row per user) and needs no TTL; expired
lockouts are recognised by `locked_until` in the past and need not be deleted
eagerly.

**Alternatives considered.**

1. **In-memory store inside the API process** — counters and lockouts held in
   process memory. Simplest possible, no schema, no cleanup job. The
   single-API-instance assumption (NFR-SCALE-1) makes in-process state *correct*:
   there is no second instance to disagree with. **Rejected** because a
   process restart clears all state, which hands an attacker a free lockout reset
   — restarting the API (a deploy, a crash, an `OOM`) wipes every active lockout
   and every in-window failure count. For a control surface that can start/stop
   game servers, a restart-clears-lockout window is the wrong default.
2. **External cache (Redis/Memcached)** — natural fit for TTL-keyed counters.
   **Rejected** as overkill at NFR-SCALE-1: it adds a deployment dependency and a
   second data store for state that fits comfortably in the database the service
   already runs. The Port (below) means a deployment that later needs shared
   cross-instance state can add such an adapter without a domain change.

**Rationale.** The deciding factor is durability across restart versus
operational simplicity. The database already exists, is already a hard
dependency, and gives the state durability for free — a restart does not reset
lockouts — at the cost of two small tables and one prune job, which is cheap.
The in-process option would be simpler but trades away the one property
(surviving restart) that makes lockout meaningful against a determined attacker.
Crucially, the choice is sealed behind the `LoginAttemptStore` Port
(NFR-PORT-1): if the single-instance assumption (NFR-SCALE-1) changes, or a
deployment prefers in-memory or a cache, the adapter is swapped without touching
the brute-force use case. The in-process correctness note above is therefore a
property of the *chosen adapter and deployment*, not of the design.

---

## 4. Trusted-proxy IP resolution

The per-IP counter (Section 2) is only as trustworthy as the source IP it counts,
and a forwarded-for header is attacker-controlled unless it arrives from a proxy
the operator runs. The API therefore resolves the client IP as follows
(`auth.proxy.*`, [`CONFIGURATION.md`](CONFIGURATION.md) Section 7.3):

- By default (`trust_forwarded_headers` = false) the **immediate peer** address
  is the client IP; forwarded headers are ignored.
- When `trust_forwarded_headers` is true, the forwarded-for header is honoured
  **only** when the immediate peer is on the `trusted_proxies` allow-list
  (IPs/CIDRs). Otherwise the immediate peer is used.

This denies an unauthenticated caller the ability to spoof its source IP and
thereby evade or poison the per-IP brute-force counter.

---

## 5. Unauthenticated endpoints

The API exposes two unauthenticated probes on its HTTP port, for orchestrators.
Like the rest of the HTTP API they are namespaced under `/api` — the probes
share the `/api` prefix rather than carving a root-level exception out of the
SPA fallback (WEBUI_SPEC 7.7):

- `GET /api/healthz` — liveness; reports the database-connectivity readiness inline.
- `GET /api/readyz` — readiness; 200 with per-component booleans when every critical
  component is ready, 503 with the same shape otherwise.

They are **deliberately unauthenticated** so a probe needs no credential, and
**safe-by-content**: both return only component booleans — never per-user or
per-server identifying data.

The Prometheus exposition is **not** one of them. It is served on a separate
listener, described below. The OpenAPI schema and docs pages are not probes
either; they are off by default (below).

### The port-publishing argument does not cover the HTTP port

Anything mounted on the API's HTTP port is on the internet in this repo's
recommended topology. The `cloudflared` service (`compose.yaml`) forwards the
whole public hostname to `api:8000`, path-scoped by nothing, so the loopback
publish
(`${API_HTTP_BIND_IP:-127.0.0.1}:${API_HTTP_PORT}:${MCD_API_SERVER__HTTP_PORT:-8000}`)
constrains only *host* reachability — not the tunnel, which reaches the API over
the `mcsd` network.
Any statement of the form "Compose publishes only the API port, so X is not
exposed" is therefore false for X on that port: the tunnel returns whatever is
mounted there to an unauthenticated request.

### Reachability posture per endpoint

Each unauthenticated endpoint has a deliberate posture under the tunnel topology
(issue #2568):

| Endpoint | Reachable from the edge | Why |
|---|---|---|
| `/api/healthz` | **Yes, deliberately.** | The compose healthcheck probes it, and its content is two booleans. |
| `/api/readyz` | **Yes, deliberately.** | Component-level readiness (`database`, `control_plane`) is operationally useful and pairs with `/api/healthz`. Accepted cost: during an incident an outside observer can tell *which* dependency is down. |
| `/api/openapi.json`, `/api/docs`, `/api/redoc` (and their `/api/docs-assets/*`) | **Only when `docs.enabled`**, which defaults to `false` (CONFIGURATION.md Section 5.10). | The schema is the complete route inventory: every admin route, every data-plane route, every request and response shape. It leaks no credential, but it is a free map for choosing where to probe. That is useful in development and of little use in production. With the key unset these paths are not mounted and return the ordinary `/api` 404. |
| `/metrics` | **No.** It is on a separate listener. | See the next subsection. |

Turning `docs.enabled` on puts the schema and docs pages on the internet like
every other route on the port. Nothing in the API scopes them to an internal
caller. The webui client generator does not need them: `make openapi-gen` reads
`app.openapi()` in-process. To publish them but keep them away from strangers,
put a Cloudflare Access policy on those paths (DEPLOYMENT.md Section 8).

The docs pages are switched off in place and **not** moved to the metrics
listener. That listener exists for a scrape target with no browser consumer.
The docs pages are meant to be opened in a browser, so they only need a switch.
`/api/readyz` was the one real candidate for the listener, and it stays on the
main app.

### The Prometheus exposition (`metrics.*`)

`GET /metrics` is served on its **own listener**, off by default
(`metrics.enabled`, CONFIGURATION.md Section 5.10). It is not mounted on the
HTTP API app at all, so under the documented tunnel configuration the tunnel
cannot reach it: the public hostname is mapped to `http://api:8000`, and the
exposition is not on `:8000`. No API-side routing or middleware change can undo
that — but it is not an absolute. `cloudflared` is a sibling container on the
same network and would route to `api:9090` just as readily if an operator mapped
a second public hostname to it. The repo cannot enforce that (the mapping lives
in the Cloudflare Zero Trust dashboard), which is why "do not add one" is
documented below. This is the same posture the relay takes for its own
metrics endpoint (RELAY.md Section 13).

The content is aggregates only — no names, ids, emails or IPs, and the label
sets are structurally bounded (route *templates*, a fixed observed-state tuple,
the `WorkerStatus` enum). But it is operational signal an external party has no
need to see: server and worker counts, per-route request volume (whose `route`
and `status` labels make login success/failure rates derivable:
`http_requests_total{route="/api/auth/login"}` split by `status`, 200 against
401 — a live oracle for anyone probing the FR-AUTH-4 brute-force behaviour),
scanning activity as `<unmatched>` 404s, process start timestamps, control-plane
liveness, and latency histograms.

**The bind address is not the control, and which control applies depends on the
topology.** The listener binds `0.0.0.0` by default, because the API's canonical
deployment is a container where loopback would put the endpoint out of reach of
any scraper. What keeps it private differs by deployment:

- **Compose.** The metrics port is not in the `api` service's `ports:` list, so
  the bind happens inside the container's network namespace and the endpoint
  exists only on the `mcsd` network; scrape it from a service on that network
  (`http://api:9090/metrics`). Keep `metrics.host` at `0.0.0.0` here — narrowing
  it to loopback only makes the endpoint unscrapeable, and buys nothing, because
  the missing `ports:` entry is what confines it. `API_HTTP_BIND_IP` does not
  apply: it only interpolates into that `ports:` list, and there is no metrics
  entry to interpolate into. The two things not to do are add one, and map a
  second Cloudflare public hostname to the port.

  **What "only the `mcsd` network" is worth.** The phrase would be worth little
  if the Minecraft server containers shared that network — it would then mean
  "and anything running inside a managed Minecraft server". They do not: the
  Worker attaches every MC server container it creates to a separate
  `mcsd-servers` network, from which `mcsd` is unreachable, so an uploaded
  plugin cannot read the exposition — see
  [Section 6](#6-minecraft-server-container-trust-model), which also states
  what that split does not cover. `mcsd` is first-party only: `db`,
  `seaweedfs`, `relay`, `cloudflared` and the Worker. The `cloudflared` caveat
  above is the live one — it would route a second public hostname to `api:9090`
  as readily as to `api:8000`.
- **Non-compose runs** — bare metal, systemd, or any process started outside
  compose (DEPLOYMENT.md Section 8). Here `0.0.0.0` genuinely is a **second
  network-reachable port**, and a reverse proxy in front of the API's HTTP port
  does not cover it: different port, the proxy never sees it. Set `metrics.host`
  to `127.0.0.1` (same-host scraper) or a private interface, or firewall the
  port. IPv4 and IPv6 literals are both accepted, as for `server.host`.

---

## 6. Minecraft server container trust model

The worker creates each Minecraft server as a sibling Docker container running
operator- and community-supplied JARs, plugins and mods. **Treat every MC
container as hostile.** Plugin ecosystems are exactly where third-party code
arrives, the operator did not write that code and cannot audit it, and the
system creates those containers on purpose — so this is a designed-in untrusted
workload, not an accident.

This is why phrasing matters here. A claim of the form "X is only reachable on
the compose network" describes a *network*, and says nothing on its own about
who is on it. Statements in this repository about reachability should name the
network, enumerate the ports, and say how and when that was established —
rather than assert that something is safe.

### The two networks

`compose.yaml` ships **two** user-defined networks:

| Network | Members | What runs there |
|---|---|---|
| `mcsd` | `api`, `db`, `seaweedfs`, `migrate`, `seaweedfs-lifecycle`, `relay`, `cloudflared`, `worker` | first-party services only. **First-party is not authenticated** — see the residual below |
| `mcsd-servers` | the Minecraft server containers, `worker` | untrusted third-party code |

`worker` is the only service on both, and it has to be: it dials `api:50051`
(control plane) and `api:8000` (data-plane transfers) on `mcsd`, and it resolves
each MC container's name for its RCON and relay game dials on `mcsd-servers`. It
binds no listening socket on either network — both of its sessions are outbound
dials — so being adjacent to it grants no service to talk to.

The `mcsd-servers` row is checked mechanically, not just stated:
`scripts/check_compose_topology.py`, a step of the required `check` job in the
`sanity` workflow, renders `compose.yaml` with every profile enabled and fails
unless the network the worker attaches MC containers to has exactly one compose
service on it, `worker`. It also fails if any service sets `network_mode` or if
either network name stops deriving from the project name. It checks the
compose file, not a running host: a container attached by hand, or a server
started before a network change (see
[`../dev/DEPLOYMENT.md`](../dev/DEPLOYMENT.md) Section 9), is outside what it
sees.

### What an MC container can reach

Enumerated 2026-08-02 by TCP connect from inside a **booted** Minecraft server
container on `mcsd-servers`, by service name and by raw control-plane IP, with a
positive control from a container on `mcsd` confirming each target was live. Two
entries, marked (†) and (‡), are closed by a bind or a flag rather than by the
network split; their control column reports the post-deploy probe of 2026-08-25
from the `api` container (an `mcsd` peer), not the 2026-08-02 enumeration:

| Target | From `mcsd` (control) | From `mcsd-servers` |
|---|---|---|
| `seaweedfs` `8333` S3, `8888` filer, `9333` master, `8080` volume, `18333` S3 gRPC, `18080` volume gRPC, `18888` filer gRPC, `19333` master gRPC; `8181` Iceberg REST (‡) | all open; `8181` refused — disabled by flag (‡) | all blocked |
| `api` `8000`, `api` `50051` | open | blocked |
| `db` `5432` | open | blocked |
| `cloudflared` `20241` | refused — loopback bind (†) | blocked |
| `grpcurl -plaintext seaweedfs:18333 list` | lists `SeaweedS3IamCache`, `SeaweedS3LifecycleInternal` | dial fails |

Blocked, not refused: the packets are dropped between bridges, so this holds by
raw IP as well as by name — a plugin that hardcodes the control-plane subnet
gets the same result as one that resolves `api`.

(†) **`cloudflared` `20241` — a loopback bind, independent of topology.** That
listener carries cloudflared's own `/metrics`, `/debug/pprof/` and `/diag/*`,
all unauthenticated, and with no `--metrics` argument cloudflared binds it on
`0.0.0.0` — open to every peer on `mcsd`. `compose.yaml` passes
`--metrics 127.0.0.1:20241` to the `tunnel` command, so the listener binds the
container's own loopback interface and no peer on either network has a path to
it. That is a control independent of topology: segmentation removes the
`mcsd-servers` path, the loopback bind removes the `mcsd` path as well, and
neither depends on the other holding. Probed 2026-08-25 on the canonical host
after a deploy, from the `api` container — `docker compose exec api python -c
"import urllib.request;
urllib.request.urlopen('http://cloudflared:20241/metrics', timeout=3)"` — and
refused (`Errno 111`). The same command re-checks it after any change to the
`cloudflared` command line.

(‡) **`seaweedfs` `8181` — disabled by flag.** `compose.yaml`'s `weed server`
command passes `-s3.port.iceberg=0`, which `weed server -h` on
`chrislusf/seaweedfs` `4.41` documents as `Iceberg REST Catalog server listen
port (0 to disable)`. Verified 2026-08-21 against `chrislusf/seaweedfs:4.41`,
outside compose, on a throwaway internal docker network: with the flag, `8181`
is absent from `netstat -lnt` in the container and a peer container's
`GET http://<container>:8181/v1/config` is refused; without it, the same
request answers 200. The other eight listeners are unaffected, and `8333` and
`8888` answer from a peer. That probe was not repeated when `compose.yaml`
moved its pin off `4.41`, so it describes `4.41`, not the pinned version. Probed
2026-08-25 on the canonical host after a deploy, from the `api` container —
`docker compose exec api python -c "import urllib.request;
urllib.request.urlopen('http://seaweedfs:8181/', timeout=3)"` — and refused
(`Errno 111`); `compose.yaml` pinned `4.41` from 2026-08-20 (#2733) until
2026-08-26 (#2804), so that deploy ran `4.41` too, not the pinned version. The
same command re-checks it after an image bump or a change to the `weed server`
command line.

**This covers docker-network paths only.** A port **published to the host** on a
non-loopback interface is reachable from `mcsd-servers` through the bridge
gateway (`172.17.0.1`, each bridge's own gateway address, or the host's LAN
address), because Docker DNATs published ports from every interface.
Segmentation removes the docker-network path; it does not remove host-published
ports. Two cases:

- **The API is loopback by default, and refused from both networks as shipped.**
  `API_HTTP_BIND_IP` defaults to `127.0.0.1`. But `API_HTTP_BIND_IP=0.0.0.0` and
  `API_HTTP_BIND_IP=<lan-ip>` are documented, supported configurations
  ([`../dev/DEPLOYMENT.md`](../dev/DEPLOYMENT.md) Section 8), and either one
  opens `api:8000` to every Minecraft container on the host.
- **The relay publishes its player ports on every interface; only the Worker
  dial-back is bind-scoped.** With the `relay` profile active,
  `compose.yaml` publishes `25565/tcp`, `25675/udp` and `19132-19231/udp` with
  **no host IP**, and `25665/tcp` on `${RELAY_CONTROL_BIND_IP:-127.0.0.1}`. So on
  any relay-enabled deployment a Minecraft container reaches the three
  no-host-IP publications via the bridge gateway — `25565` always, the two UDP
  sets only when `MCD_RELAY_BEDROCK_ENABLED=true`, since the relay publishes
  those unconditionally but binds them only under that flag (a probe against an
  unbound one gets a refusal, not a service).

  **`25565` and the Bedrock UDP sets reaching a plugin is accepted, not
  scheduled.** They are the product: a relay that players cannot reach relays
  nothing, and Docker's DNAT does not distinguish a packet from a Minecraft
  container from one off the internet. What a plugin gains there is what any
  internet client already has — the relay's own player listener, behind its
  per-IP hygiene caps ([`RELAY.md`](RELAY.md) Section 11) — not a control-plane
  surface. `25675/udp` is the Bedrock **Worker** dial-back and by that argument
  would be bind-scoped like `25665`; it is not, because the bind variable is a
  single one, scoped to the Java control port by decision. It is a recorded
  residual: reachable from a Minecraft container whenever the Bedrock gate is
  on.

  **`25665`, the Worker dial-back, is closed by the bind.** Why the bind is the
  control, and not the network split: a relay-shaped listener (published with
  no host IP) on a container attached to `mcsd`, TCP-connect probed from the
  servers network, shows

  ```text
  172.20.0.1:<port>      servers-bridge gateway           OPEN
  172.17.0.1:<port>      docker0                          OPEN
  192.168.0.254:<port>   host LAN address                 OPEN
  172.19.0.7:<port>      same container, direct on mcsd   blocked
  ```

  The last line is the finding: the container is unreachable at its `mcsd`
  address and reachable at three host addresses at the same time. Segmentation
  blocks the docker-network path to a service while the host-published path to
  the *same* service stays open. `RELAY_CONTROL_BIND_IP` defaults to
  `127.0.0.1`, which removes all three — Docker's DNAT rule then matches only
  packets destined to loopback, the same mechanism that makes the API refuse
  from both networks in the case above. Probed 2026-08-25 on the canonical host
  after a deploy, from inside a **booted** MC server container, by every host
  address — gateways from `docker network inspect mcsd-servers` / `mcsd`, the
  rest from `ip -4 addr` on the host — with `25665` refused at all four and
  `25565` OPEN at all four. The probe, for re-checking after a compose change:

  ```sh
  docker exec <mc-container> sh -c '
    for a in 172.19.0.1 172.18.0.1 172.17.0.1 192.168.0.254; do
      for p in 25665 25565; do
        nc -z -w3 "$a" "$p" && echo "$a:$p OPEN" || echo "$a:$p blocked"
      done
    done'
  # images without nc (most Java server images) — bash has /dev/tcp built in:
  docker exec <mc-container> bash -c '
    for a in 172.19.0.1 172.18.0.1 172.17.0.1 192.168.0.254; do
      for p in 25665 25565; do
        timeout 3 bash -c "echo > /dev/tcp/$a/$p" 2>/dev/null \
          && echo "$a:$p OPEN" || echo "$a:$p blocked"
      done
    done'
  ```

  Expected: `25665` **refused** at every host address, `25565` OPEN at every
  one of them. Refused, not blocked — the loopback bind leaves no DNAT
  rule to match and no listener to answer, so the connect gets an RST, which is
  the API's mechanism above and not the inter-bridge packet drop this section
  calls "blocked"; the probe above prints `blocked` for any failed connect, so
  read its output with that in mind. The second expectation is as load-bearing
  as the first — it is what distinguishes the bind from a relay that stopped
  serving.

If you publish anything off loopback, firewall it at the host or accept that
plugins can reach it.

### Container hardening

Independently of the topology, the worker creates every container that runs
server-controlled code — the launch container and the supervised Forge install
container, first attempt or retry — with these restrictions (issues #2600,
#3343, #3369):

- **An unprivileged user**, never root in the container: `driver.container.user`
  ([`CONFIGURATION.md`](CONFIGURATION.md) Section 6.3), `25565:25565` under the
  shipped root worker. The worker sets it on the create request, so it overrides
  whatever user the image declares.
- **No capabilities** (`CapDrop: ["ALL"]`, and nothing added back). The
  container's bounding set is empty, so no process in it can hold a capability,
  whatever uid it reaches. Among the ones gone is `CAP_NET_RAW`: no raw or
  packet socket, which removes ARP spoofing and sniffing on `mcsd-servers`.
- **`no-new-privileges`** (`SecurityOpt: ["no-new-privileges"]`). An `execve`
  in the container never raises privileges: a setuid or setgid bit and a file
  capability are ignored.
- **A task limit** (`PidsLimit: 4096`). Processes and threads together; a fork
  or thread bomb in server code stops at its own container instead of using up
  the host's pids under the worker and the other servers. A modded server runs
  a few hundred threads. The limit is fixed, with no setting.
- **A read-only root filesystem** (`ReadonlyRootfs`), with `/tmp` as the one
  writable path outside the working set: a 256 MiB tmpfs mounted
  `rw,exec,nosuid,nodev`. `/tmp` is the JVM's `user.home` and
  `java.io.tmpdir`, so Java user preferences, temp files and the native
  libraries JNA, Netty and profilers extract go there; loading those libraries
  needs `exec`, which Docker's tmpfs default (`noexec`) would deny. What a
  server keeps in `/tmp` is memory and counts against its memory limit. In the
  shipped images the run-as user could already write nothing else on the root
  filesystem apart from `/var/tmp`, `/run/lock` and an empty pebble state
  directory, which neither the JVM nor the Forge installer uses (measured in
  all seven images, with a JVM probe of each under these settings). A plugin
  or mod that writes anywhere else outside `/data` and `/tmp` gets `EROFS`.

**This is the premise the worker's own defences rest on**, stated exactly:
server-controlled code runs as one unprivileged uid, with an empty capability
bounding set, under `no-new-privileges`. It therefore cannot create a device
node (`CAP_MKNOD`), give a file away or take one over (`CAP_CHOWN`,
`CAP_FOWNER`), bypass file permissions (`CAP_DAC_OVERRIDE`), or change its uid
(`CAP_SETUID`) — and it cannot get any of that back. "Runs as
non-root" alone does not say this: with only the user set, the effective set is
empty but the bounding set is Docker's default, and one setuid-root binary in
the image returns it (the shipped Java images carry `su`, `mount`, `umount`,
`passwd` and others, setuid root).

What keeps server-controlled code from giving a file outside its working set a
second name inside it is not a capability but the mount: the working set is a
bind mount, the only part of the host the container has, and `link(2)` does not
cross a mount — it fails with `EXDEV` for any caller, privileged or not (seen
at runtime in both directions as the run-as user, PR #3370). Within the mount
the kernel's protected hard links (`fs.protected_hardlinks=1`) further limit
that code to files it owns or can read and write, which it cannot get around
without `CAP_FOWNER`; no defence here depends on that setting.

The premise depends on the worker's container creation and on nothing in the
image: all of these restrictions are fields of the create request, every create
the driver makes goes through the one function that sets them, and the worker
never runs `docker exec` against these containers. A test guards that single
path against regression, within limits: it reads the driver package's source
and every file of the worker module that imports it, the e2e harnesses
included, and matches names, not types, so it catches a new create added inside
the driver or made from another worker package, but not one made through a
function value an allowed helper hands out. Each existing container kind is covered
independently by tests on its create request. The premise also assumes a host
that lets a container start under `no-new-privileges`: with SELinux enforcing,
the container policy must allow that domain transition
([`../dev/DEPLOYMENT.md`](../dev/DEPLOYMENT.md), the caveat on unprivileged
Minecraft containers). The image is operator configuration —
`driver.container.images` maps a Java major to an image, and a server's
Minecraft version only selects among those — so a tenant cannot bring one, but
the restrictions would hold for an image that ships setuid binaries or declares
`USER root` all the same. An image whose entrypoint has to start as root and
drop privileges itself is not supported: it would start as the run-as user and
fail. What the premise does not cover is the host side of the Docker socket: an
operator's own `docker exec --privileged`, or a container someone else creates.

Nothing a Minecraft server or the Forge installer does needs a capability. They
write only to the working set, which the run-as user owns, and to `/tmp`; they bind the game,
RCON and Geyser ports inside the container's own network namespace, where
Docker makes every port unprivileged (`net.ipv4.ip_unprivileged_port_start=0`),
so a game port below 1024 needs no `CAP_NET_BIND_SERVICE`; and JVM tooling that
attaches to the server (`jcmd`, `jattach`) attaches to a process of the same
uid.

The worker hands the bind-mounted working set to that user before each
container create, and still reads, snapshots and deletes everything in it
afterwards — as root in the shipped topology, or as the very same user when it
runs unprivileged. Because that hand-over is root re-owning a tree hostile code
wrote, it is done only once no container of that server — launch or install —
is alive (a live or unaccounted-for one refuses the start, with no ownership
change), and it walks the tree by directory descriptor without following
links, so a directory swapped for a symlink cannot lead it outside the working
set. Staying inside the tree does not settle a file with more than one name (a
hard link), because an ownership change acts on the file, under every name it
has, and one of them may be outside the working set. The hand-over therefore
counts the names of each such file it meets and re-owns the file only once it
has met as many as the file has links — every name is then inside the working
set, as with a link a server made between two of its own files (a backup mod
that keeps hard-linked copies of the world). A file with a name it never met
keeps its owner, and one `WARN` per hand-over gives their number and an example
path with the `server_id`; the start goes on, and the server may be unable to
write that file until the operator breaks the link or re-owns the file. The
rule does not rely on `fs.protected_hardlinks`, nor on who made the link: the
container can only link within its own bind mount, and a hydrate refuses a
link member and writes every file anew, so such a link is the doing of something
else on the host — a hard-linked copy of a working set, say — and is treated
the same whatever made it (issue #3338). The count proves what it says only
while no name of the file moves under the walk: a name that moves from where it
was counted to where the walk has yet to look is counted twice, at an unchanged
link count. Three things keep names still. No container of the server is alive.
A file whose link count changes during the walk is left alone. And the worker's
own writes that publish by a rename — a file edit, which renames a file it
staged outside the working set onto a name inside it, and the write of its
generation marker, which renames a temporary name in the working-set root —
hold a per-server lock from before the staged or temporary name exists until
after the rename, which the hand-over holds for the length of its walk: an
edit waits for a walk in progress and a walk for an edit, so no name the walk
counts is replaced under it (issue #3371). A wait for that lock is not open-ended: a command gives up
when its stream is gone or after 20 seconds, and then writes nothing (an edit
fails; a marker is simply not written, which costs one extra hydrate). Those 20
seconds are the worker's own, counted from when the command starts waiting, not
from when the API dispatched it, and the API's command budget is separate and
configurable: an edit that was queued, or that runs under a shorter budget, can
time out at the API first and still be published by the worker, if the walk
ends within the worker's wait. The
walk a Forge install runs in the background before its retry and before its
launch container is ended by a stop of that server. The lock is never taken
from a writer or a walk that is still running. Its limits are these. It exists
only inside the worker process, so it excludes the worker's own writers and
nothing else: a rename made inside a working set by anything else on the host
during a walk is not held off. And the worker's writers that replace or remove
a whole working set under the per-server command reservation — a hydrate, the
snapshot after a stop, the scratch clean-up — do not take it; they move no name
within a tree, only add or remove names, which the link-count rule above
covers. What the count cannot account for is a mount inside a working
set. A host that mounts part of one a second time inside it shows the same
directory entries twice with no second link behind them, and a file named
outside could then be counted complete. Neither the worker nor a server creates
a mount there, and a working set that holds a host-made one is outside the
supported topology.
The Forge install log (`logs/forge-install.log`) is the one thing the worker
creates in a working set while the installer runs, and it never removes an
entry there: the installer can replace any name at any moment, and a removal
acts on a name (issue #3338). So the log is created without a name, given its
directory's owner, and only then linked in, and a missing `logs/` is created
only after an unnamed file could be given that owner; a worker that cannot
change ownership (root without `CAP_CHOWN`) then creates nothing. Every refusal
below is a `WARN` naming the server and the install continues without the log.
The limits: on a filesystem without unnamed files (`O_TMPFILE`: NFS, some FUSE
filesystems) the entries are named first, and one that cannot be handed over is
left in place, owned by the worker, and the `WARN` says so; the same holds for
a `logs/` whose ownership change fails after the trial succeeded. An entry
found in place is never re-owned, with one qualification: the directory the
worker opens after creating `logs/` may be one the installer put there in
between, and that directory — one inside the working set, reached without
following a link — is given its parent's owner. Linking the log in needs an
accessible `/proc/self/fd`, and under the kernel's protected hard links a
worker without `CAP_FOWNER` and `CAP_DAC_OVERRIDE` is denied it once the file
is no longer its own; in both cases no log is named.
A snapshot — which is also taken while the server runs — reads the tree
the same way (issue #3334): each directory and file is opened relative to its
parent's descriptor without following links, and what goes into the archive is
decided on the opened descriptor, so an entry swapped for a symlink or a FIFO
mid-snapshot is skipped instead of being packed from outside the working set or
blocking the worker. The walk holds one descriptor per directory level and
refuses a working set nested deeper than 128 directories. The region check
that precedes a snapshot walks the tree the same way, under the same depth
bound, and reads a fixed number of fixed-size pieces of each region file
whatever its size (issue #3335). An entry that disappears or changes kind under
it is skipped and counted rather than failing the whole check — a snapshot goes
ahead without a check that failed, so failing would let a server turn the check
off by flipping any one name (issue #3350). The snapshot's own walk skips such
an entry by the same rule but on its own, later look, so the two need not leave
out the same entries: a region file the check skipped can be back in place by
the time the snapshot reads it, and is packed unchecked. The API's publish gate,
which judges every region file in the upload, is what refuses it if it is torn.
The worker's own reads of `server.properties`
— for the RCON password and port, the ports it publishes, and the port a relay
tunnel dials — go through one reader that opens the file relative to the
working-set root without following links or blocking, accepts only a regular
file, and reads at most 4 MiB (the cap on a file edit, so a file the dashboard
accepted is never refused for its size): a `server.properties` replaced by a link to
another server's file is refused rather than read, so that server's RCON
password is never sent to the container that planted the link, and the refusal
fails the operation (a start, a tunnel dial, an RCON open) instead of falling
back to a default port or password.
The worker's generation marker (`.mcsd_generation`, in the working-set root —
its record of which store generation the set is at) is touched the same way
(issue #3336). It is read through that same reader with a 32-byte cap (a
generation is at most 20 digits): a marker replaced by a link, a FIFO, a
directory, an oversized file or anything that is not one number is refused
with a `WARN` and read as generation 0, the same as an absent marker, which
makes the API hydrate that server whenever the store holds a published
generation (against a store still at generation 0 there is nothing to hydrate
and the start is not preceded by one). The worker reads every held server's marker
at boot and before each registration, so the refusal is per server: one
server's planted marker neither blocks nor fails the scan for the others. A
launch is refused, and replayed with a hydrate, unless the marker is a regular
file. The marker is written to an exclusively created temporary file and
renamed into place, both relative to the working-set root's descriptor, so
whatever was planted at either name is replaced or refused, never written
through. A directory at the marker's name cannot be renamed over, so every
write fails while it stays; each failure removes its temporary file and logs a
`WARN`, and the worker leaves the directory alone rather than remove, as root,
a tree the server is still writing. Once the server has a published
generation this costs it one hydrate: the set reads as generation 0, and that
hydrate replaces the working set whole, directory included (issue #3350). With
nothing published (store generation 0) the hydrate transfers nothing and
leaves the working set, directory included, in place, so the directory goes on
failing the launch guard and the server does not start until it is removed. What this does not make trustworthy is the marker's *value* — see "The
generation marker's value" below.
A file the worker writes into a running server's working set (a file edit
through the API, and any parent directory it has to create) is given its
directory's owner on the open descriptor, so the server keeps access to it.
A file edit never creates a name in the server's directory other than the one
it publishes (issue #3359). The new content is written to a file with no name
(`O_TMPFILE`) on the target's directory and fsynced. Only then is the file
named, under a worker-reserved `.edit-<random>` name in the scratch root, which
is outside every working set and visible to no container. That name is then
renamed onto the target. The file is fsynced before the rename and both
directories after it; if only those directory syncs fail, the edit, already
visible, still succeeds and the worker logs a `WARN`. Nothing a server does can replace the name between
the write and the rename, so the published file is the one the edit wrote. A
worker crash before the link leaves nothing behind. A crash between the link
and the rename leaves the staged name in the scratch root. No snapshot packs
it and no held-set scan counts it, and the next worker boot removes it. That
boot removal touches only non-directory entries of the scratch root with that
prefix, never a server's file. The limits: the scratch root and the working
sets must be one mount, as the hydrate's swap already requires, and the edit
fails otherwise (`EXDEV`). Naming the unnamed file needs an accessible
`/proc/self/fd`. On a filesystem without unnamed files the file is created
under its staged name from the start, in the same private directory, so a
crash while writing also leaves a staged name for the boot to remove.

Measured 2026-10-08 on a probe stack (its own compose project and networks),
inside a booted Paper 1.21.4 container and a Forge 1.20.1 install and launch
container, when only `CAP_NET_RAW` was dropped (issue #2600):

```text
id -u: 25565
CapEff: 0000000000000000
CapBnd: 00000000a80405fb      # bit 13 (CAP_NET_RAW) clear; a80425fb before the change
socket(AF_PACKET, SOCK_RAW)        operation not permitted
socket(AF_INET, SOCK_RAW, ICMP)    operation not permitted
```

The two socket calls fail the same way from a `docker exec --user 0` into the
same container: the bounding set denies them, not just the uid. From one of
those containers a TCP connect to the other's RCON port still succeeds — the
residual stated below.

Measured 2026-10-13 (issue #3343) in a container of each of the seven Java
images `compose.yaml` configures (`eclipse-temurin` 8, 11, 17, 21 and 25
`-jre`, `eclipse-temurin:16-jdk`, `azul/zulu-openjdk:7`) created with the user and the two options above, each of
which then ran `java -version`:

```text
id: 25565:25565
CapPrm: 0000000000000000
CapEff: 0000000000000000
CapBnd: 0000000000000000      # 00000000a80405fb with only NET_RAW dropped
NoNewPrivs: 1                 # 0 before
net.ipv4.ip_unprivileged_port_start: 0
```

### What this deliberately does not close

Segmentation removes a class of lateral reach. It is not a complete container
security model, and the following remain true:

- **MC container to MC container.** Inter-container communication is on within
  `mcsd-servers`, so one server's hostile plugin keeps plain TCP to another
  server's RCON port and game port. What it has lost is the raw-socket
  primitive: the worker creates every MC container without `CAP_NET_RAW` (see
  "Container hardening" above), so it can no longer ARP-spoof or sniff that
  segment to read another server's RCON password off the wire — it has to guess
  it. Impact is bounded to other Minecraft servers rather than the object store
  or the worker credential, but it is not zero. Closing it needs a per-server
  network or `enable_icc=false` on `mcsd-servers`; neither is implemented.
- **Outbound internet is unrestricted, on purpose.** `mcsd-servers` is a normal
  bridge, not `internal: true`: Minecraft servers need egress for Mojang
  online-mode authentication and for plugin and mod downloads. A hostile plugin
  can therefore still exfiltrate anything it can read inside its own container
  and fetch a second stage.
- **The host, not the network.** The worker holds the Docker socket, and
  container-level hardening is defence in depth underneath this boundary, not
  part of it. Five items of it are in place (issues #2600, #3343 and #3369,
  "Container hardening" above): MC containers run as an unprivileged user, with
  no capabilities, under `no-new-privileges`, with a task limit, on a read-only
  root filesystem. The rest is not implemented — there is no custom seccomp or
  AppArmor profile (Docker's defaults apply) or user namespace.
  **TLS on the control and data planes is deferred** (owner decision on issue
  #2600): with MC containers off `mcsd`, the remaining observers of those
  plaintext planes are the first-party services and the host itself, and a
  compromised host-level observer is outside this trust model. Revisit it if a
  worker is ever deployed across an untrusted network
  ([`../dev/DEPLOYMENT.md`](../dev/DEPLOYMENT.md) Section 8).
  Neither is a substitute for the other: hardening is a checklist where every
  item must land, segmentation removes the class in one topology choice.
- **`mcsd` itself is not hardened; that is an accepted residual.** Everything
  on it has unauthenticated read, write and delete over **every tenant's**
  worlds, snapshots and JARs: the SeaweedFS
  filer (`8888`), master (`9333`) and volume (`8080`) ports take no credential,
  and the S3 gRPC port (`18333`) serves reflection uncredentialed, handing out
  `SeaweedS3IamCache` and `SeaweedS3LifecycleInternal` — and behind that
  reflection sit the S3 IAM RPCs (`PutIdentity`, `RemoveIdentity`, `PutPolicy`,
  …), so a caller on `mcsd` can mint itself an S3 identity; `-s3.iam.readOnly`
  does not guard that path. Only the S3 gateway (`8333`) enforces on every
  data-path call (its `/status` probe answers 200 uncredentialed, which is what
  the compose healthcheck uses). The ninth listener, the Iceberg REST port
  (`8181`), does not bind — see the (‡) footnote above. Who reaches the
  remaining eight: the long-running members of `mcsd` are
  `api`, `db`, `worker`, `relay` and `cloudflared` (the table above names three
  more — `seaweedfs` itself, and the one-shots `migrate` and
  `seaweedfs-lifecycle`, which run to completion and exit). Membership of `mcsd`
  is therefore equivalent to object-store admin for those five, two of which,
  `relay` and `cloudflared`, terminate internet traffic.

  **That is accepted, not scheduled.** Each of the five is a first-party
  control-plane service, and the only control that would close the surface is
  cluster-wide mTLS: `weed server` exposes no `-jwt.*` flags (SeaweedFS carries
  JWT in `security.toml`), and `security.toml`'s `grpc.s3` mTLS cannot be
  scoped to the S3 gRPC port — turning it on escalates to mTLS across the whole
  cluster, all-or-nothing. The one reduction available without that escalation
  is applied instead: `-s3.port.iceberg=0`, above. So read "first-party" in the
  table above as a statement about *who is
  attached*, never about what an attached process would have to prove — a
  compromise of `relay` or `cloudflared` lands on a network where every storage
  listener but the S3 gateway answers with no credential, and `18333`'s IAM RPCs
  mint an identity the gateway itself then accepts.
- **Two members of `mcsd` terminate internet traffic.** `relay` accepts arbitrary
  inbound connections — players on `25565` and `19132-19231/udp`, Worker
  dial-back tunnels on `25665` and `25675/udp` — and `cloudflared` terminates a
  public tunnel. Compromising either puts an attacker on `mcsd`, with the
  unauthenticated storage surface above in reach. Segmentation raises the bar
  for a hostile *plugin*; it does not raise it for a hostile *packet* arriving
  at the relay. And the relay is reachable from **both** directions: its player
  publications carry no host IP, so a Minecraft container reaches them through
  the host gateway even though `relay` is not on `mcsd-servers` — the plugin
  path to the relay's *player* listener survives segmentation. The plugin path
  to the **Worker dial-back** does not: `25665` is published on loopback by
  default (see "What an MC container can reach" above).
- **Host-published ports bypass the split entirely.** See the note at the end of
  "What an MC container can reach": publishing the API off loopback, and the
  relay's `25565` / `25675/udp` / `19132-19231/udp`, are reachable from every
  Minecraft container on the host. The bind default on the relay's `25665`
  removes that one port from the list; it does not change the mechanism for the
  rest.
- **A running server keeps the network it was started on.** An MC container is
  attached to its network when the Worker starts it and is never re-attached; a
  container already running when the server network changes keeps the earlier
  network's full reach until it is restarted. The reachability table above
  describes a server started on the current topology. See
  [`../dev/DEPLOYMENT.md`](../dev/DEPLOYMENT.md) Section 9.
- **The server's own working directory.** Everything bind-mounted into an MC
  container — its world, its `server.properties`, its RCON password — is
  readable and writable by the code running there. That is inherent to running
  the server at all; the boundary protects *other* tenants' data, not the
  contents of the container that hosts the hostile plugin.
- **The generation marker's value.** The marker lives in that same directory,
  which the server's user owns, so the code running there can replace it with a
  regular file holding any number, and the worker cannot tell that file from
  one it wrote (issue #3336). The worker reports the number in two places, and
  the API only ever *compares* it:
  - as the held generation, on registration. The API skips the hydrate before a
    start when `held >= store`
    ([`CONTROL_PLANE.md`](CONTROL_PLANE.md) Section 5.1). A number forged **at or
    above** the store's makes the worker start the server on the working set it
    already holds instead of the store's copy; forged **below**, or made
    unreadable (which reads as 0), it forces a hydrate that replaces that
    working set (the replaced tree is parked aside,
    [`STORAGE.md`](STORAGE.md) Section 4.6). The comparison is `>=`, so 0 is
    below only a store that has published: `held == store == 0` skips too.
  - as the base generation of a snapshot. Before staging, the publish is
    refused only when all of these hold: a base is declared (the worker sends
    none for 0), it is *below* the store's current generation, both the
    publishing worker and the worker that published current are known and
    differ, and the publishing worker is not the one currently assigned to the
    server. A forged high base is never below, so that guard cannot fire; a
    forged low one can at most get the server's own publish refused, and not
    even that while its worker is the assigned one.

  None of this reaches another server or the store's bookkeeping: both
  comparisons are scoped to the one server, and the generation a publish
  produces is always the store's own `current + 1`, never the declared number
  (the store's current is read before staging and re-checked at commit). The
  remaining publish fence does not read the marker: at commit, a publish from
  a known worker is refused when a *different* worker is assigned to the
  server — and allowed when the server has no assignment at all, which is the
  window a stopped server's final snapshot uses. What a server gains is over **its own lineage**: it can keep
  running the working set it holds where the store has moved on without it, and
  then publish that set as the newest generation. The store moves on without
  the worker when a backup is restored, a file is edited while the server is
  stopped, or the JAR changes — each relies on the next start hydrating — and
  only if the working set outlived the stop, which it does when the final
  snapshot was refused (a server can cause that: the region check refuses a
  torn world). So a hostile plugin can make a backup restore or an at-rest edit
  of its own server silently not take effect. Closing that needs the marker's
  value to come from somewhere the container cannot write; that is an open
  decision (issue #3349), not an accepted residual, and nothing of it is
  implemented.

---

## 7. Related documents

| Doc | Covers |
|---|---|
| [`../REQUIREMENTS.md`](../REQUIREMENTS.md) | FR-AUTH-4 binding bullets; NFR-SCALE-1, NFR-PORT-1 |
| [`CONFIGURATION.md`](CONFIGURATION.md) | Section 7 — the auth-hardening knobs, defaults, and thresholds referenced here |
| [`DATABASE.md`](DATABASE.md) | Section 4 — the core auth model and the note deferring this state to this document |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Port/adapter layering and naming for the `LoginAttemptStore` seam |
