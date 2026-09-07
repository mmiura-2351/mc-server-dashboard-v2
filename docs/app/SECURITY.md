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
> **Scope.** Authentication-hardening behaviour, plus
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
5. [Observability endpoints](#5-observability-endpoints)
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

## 5. Observability endpoints

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
listener, described below.

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

`/api/healthz` and `/api/readyz` are reachable that way, and are accepted as
such on their content. Tightening `/api/readyz` (and the OpenAPI schema/docs
routes, which are exposed the same way) is not implemented.

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
moved its pin to `4.42`, so it describes `4.41`, not the pinned version. Probed
2026-08-25 on the canonical host after a deploy, from the `api` container —
`docker compose exec api python -c "import urllib.request;
urllib.request.urlopen('http://seaweedfs:8181/', timeout=3)"` — and refused
(`Errno 111`); `compose.yaml` pinned `4.41` from 2026-08-20 (#2733) until
2026-08-26 (#2804), so that deploy ran `4.41` too, not the pinned `4.42`. The
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

### What this deliberately does not close

Segmentation removes a class of lateral reach. It is not a complete container
security model, and the following remain true:

- **MC container to MC container.** Inter-container communication is on within
  `mcsd-servers` and the containers keep `CAP_NET_RAW`, so one server's hostile
  plugin can reach another server's RCON port and game port, and can spoof
  traffic on that segment. Impact is bounded to other Minecraft servers rather
  than the object store or the worker credential, but it is not zero. Closing it
  needs a per-server network, or `enable_icc=false` on `mcsd-servers` plus
  dropping `NET_RAW`; neither is implemented — both belong with the rest of the
  container hardening (next item).
- **Outbound internet is unrestricted, on purpose.** `mcsd-servers` is a normal
  bridge, not `internal: true`: Minecraft servers need egress for Mojang
  online-mode authentication and for plugin and mod downloads. A hostile plugin
  can therefore still exfiltrate anything it can read inside its own container
  and fetch a second stage.
- **The host, not the network.** MC containers run as root with the default
  capability set, and the worker holds the Docker socket. Container-level
  hardening — dropping capabilities, non-root execution, TLS on the control and
  data planes — is defence in depth underneath this boundary, not part of it,
  and is not implemented.
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

---

## 7. Related documents

| Doc | Covers |
|---|---|
| [`../REQUIREMENTS.md`](../REQUIREMENTS.md) | FR-AUTH-4 binding bullets; NFR-SCALE-1, NFR-PORT-1 |
| [`CONFIGURATION.md`](CONFIGURATION.md) | Section 7 — the auth-hardening knobs, defaults, and thresholds referenced here |
| [`DATABASE.md`](DATABASE.md) | Section 4 — the core auth model and the note deferring this state to this document |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Port/adapter layering and naming for the `LoginAttemptStore` seam |
