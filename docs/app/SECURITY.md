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
> **Scope.** Authentication-hardening behaviour only. The tunable thresholds and
> defaults are owned by [`CONFIGURATION.md`](CONFIGURATION.md) Section 7 and are
> referenced, not duplicated, here. Token issuance/verification (FR-AUTH-2) and
> password hashing (FR-AUTH-3) are separate concerns owned by the `TokenService`
> and `PasswordHasher` Ports ([`ARCHITECTURE.md`](ARCHITECTURE.md) Section 5.1).
> The proven baseline for the exact values is the legacy
> [`SECURITY.md`](https://github.com/mmiura-2351/mc-server-dashboard-api/blob/master/docs/app/SECURITY.md),
> adopted as-is for M1 (reference only; the FR-AUTH-4 bullets are binding).

## Table of Contents

1. [Password policy](#1-password-policy)
2. [Brute-force protection](#2-brute-force-protection)
3. [Lockout-state home (decision)](#3-lockout-state-home-decision)
4. [Trusted-proxy IP resolution](#4-trusted-proxy-ip-resolution)
5. [Related documents](#5-related-documents)

---

## 1. Password policy

On registration and password change the API rejects a password that fails any
enabled rule. The rules and their defaults are configured under `auth.password.*`
([`CONFIGURATION.md`](CONFIGURATION.md) Section 7.1):

- **Length** — between `min_length` and `max_length`. The upper bound
  is a DoS guard and respects the bcrypt 72-byte cap.
- **Complexity-or-length** — at least 3 of {upper, lower, digit, symbol} **or**
  at least 16 characters (`require_complexity`).
- **Common-password blocklist** — reject passwords on a published list
  (`check_common_list`; legacy baseline: SecLists xato-net top-10,000).
- **User-info rejection** — reject a password containing the username or the
  email local-part (`forbid_user_info`).
- **Simple-pattern rejection** — reject 4+ repeated characters or 4+ sequential
  alphabet/keyboard/numeric runs (`forbid_simple_patterns`).

Policy is pure, deterministic domain logic: it depends on no persistent state and
sits in the domain layer, callable from the registration and password-change use
cases.

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

This algorithm needs **runtime state** that outlives a single request: the
attempt records that the sliding windows count over, and the per-account lockout/back-off
record. Where that state lives is decided in Section 3.

---

## 3. Lockout-state home (decision)

[`DATABASE.md`](DATABASE.md) Section 4 deliberately omits this state from the
core entity model, deferring the storage decision to this document. The
brute-force / lockout state is auth-hardening runtime state, not a core domain
entity; this section decides its home and keeps it consistent with that note.

**Decision.** M1 persists brute-force / lockout state in the **relational
database** (the same PostgreSQL instance as the core model,
[`DATABASE.md`](DATABASE.md) Section 1), in two dedicated auth-hardening tables
kept **separate from the core entity model**, behind a new API-side Port — call
it `LoginAttemptStore` (naming per [`ARCHITECTURE.md`](ARCHITECTURE.md)
Section 6). Business logic depends only on the Port; the M1 adapter is the
DB-backed implementation, bound at the edge.

The two tables follow the legacy proven baseline:

- **`login_attempt`** — append-only record of each authentication attempt
  (username, source IP, success flag, failure reason, timestamp). The sliding
  windows of Section 2 are `COUNT` queries over this table within the window
  bound; an index on `(username, created_at)` and on `(ip, created_at)` serves
  them.
- **`account_lockout`** — at most one row per username, holding the active
  lockout (`locked_until`) and the historic lockout count that drives the
  exponential back-off.

Because these are auth-hardening state and not part of the core graph, they are
specified here rather than in [`DATABASE.md`](DATABASE.md), and they do not
participate in the core cascade rules. Column-level detail lands with epic #4
when the schema is implemented; this document fixes only their existence, purpose,
and the Port seam.

**Cleanup.** `login_attempt` is append-only and grows without bound otherwise, so
the adapter prunes rows older than the longest configured window (a periodic
delete). `account_lockout` is bounded (one row per user) and needs no TTL; expired
lockouts are recognised by `locked_until` in the past and need not be deleted
eagerly.

**Alternatives considered.**

1. **In-memory store inside the API process** — counters and lockouts held in
   process memory. Simplest possible, no schema, no cleanup job. The
   single-API-instance assumption (NFR-SCALE-1) makes in-process state *correct*
   today: there is no second instance to disagree with. **Rejected** because a
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
dependency, and gives the state durability for free — a restart no longer resets
lockouts — at the cost of two small tables and one prune job, which is cheap. This
also matches the legacy system's proven design (the `BruteForceService` over
`login_attempts` / `account_lockouts`), so M1 adopts a known-good shape rather
than inventing one. The in-process option would be simpler but trades away the
one property (surviving restart) that makes lockout meaningful against a
determined attacker. Crucially, the choice is sealed behind the
`LoginAttemptStore` Port (NFR-PORT-1): if M1's single-instance assumption ever
changes, or a deployment prefers in-memory or a cache, the adapter is swapped
without touching the brute-force use case. The in-process correctness note above
is therefore a property of the *current adapter*, not of the design.

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

## 5. Related documents

| Doc | Covers |
|---|---|
| [`../REQUIREMENTS.md`](../REQUIREMENTS.md) | FR-AUTH-4 binding bullets; NFR-SCALE-1, NFR-PORT-1 |
| [`CONFIGURATION.md`](CONFIGURATION.md) | Section 7 — the auth-hardening knobs, defaults, and thresholds referenced here |
| [`DATABASE.md`](DATABASE.md) | Section 4 — the core auth model and the note deferring this state to this document |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Port/adapter layering and naming for the `LoginAttemptStore` seam |
