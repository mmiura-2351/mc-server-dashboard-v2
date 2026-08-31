# Documentation Index

Long-form documentation for the Minecraft Server Dashboard **v2**. The
[root README](../README.md) carries the elevator pitch, philosophy, and
architecture overview; everything that needs more than a paragraph lives here.
[`REQUIREMENTS.md`](REQUIREMENTS.md) is the source of truth for scope. The five
modules each have their own README ([`api/`](../api/README.md),
[`worker/`](../worker/README.md), [`relay/`](../relay/README.md),
[`webui/`](../webui/README.md), [`proto/`](../proto/README.md)); the per-screen
Web UI specs live in [`ui/`](ui/).

Docs are split by intent:

- **`app/`** — how the running system works: architecture,
  persisted data, the HTTP surface, the API↔Worker control-plane contract,
  runtime configuration, cross-cutting behaviour. Read these when reasoning
  about the system itself.
- **`dev/`** — how to work *on* the system: development workflow, testing
  discipline, release procedure, dependency policy. Read these when changing the
  codebase or operating a deployment.

---

## Requirements

| Doc | What it covers |
|---|---|
| [`REQUIREMENTS.md`](REQUIREMENTS.md) | What v2 must do and the architectural constraints it must satisfy: the API/Worker split, pluggable execution, the Community model, two-layer authorization, data/storage lifecycle, and the design decisions. The source of truth for scope. |

## Application docs (`app/`)

| Doc | What it covers |
|---|---|
| [`app/ARCHITECTURE.md`](app/ARCHITECTURE.md) | The Hexagonal (Ports & Adapters) layering, the `api/` / `worker/` / `proto/` module boundaries and dependency direction, the catalog of domain Ports per side, and the architecture-level decisions that [`REQUIREMENTS.md`](REQUIREMENTS.md) Section 9.1 delegates to design (execution backend fixed for a server's lifetime, file access over the control plane, JAR source on the API and Java runtime on the Worker, the API framework stack). |
| [`app/AUTH_API.md`](app/AUTH_API.md) | The `/auth/*` endpoint contract: per-endpoint status codes (a refresh with no token in either transport is a uniform 401; a logout with no token is an idempotent 204), the cookie-only `/auth/session` bootstrap, the RFC 9457 problem+json error shape and auth reason codes, body-vs-cookie transport precedence and `Set-Cookie` gating, the refresh reuse grace window with Web UI session guidance, the CSRF posture, audit events, and the `/users/me/sessions` session-management endpoints. |
| [`app/BEDROCK.md`](app/BEDROCK.md) | Feature-level overview of Bedrock (Geyser) support: activation (installing Geyser/Floodgate as normal plugins), the address model (`<base_domain>:<bedrock_port>`, no SRV), and the known limitations (no real-client-IP passthrough to the server, Floodgate's auth model versus Java-identity moderation, Geyser/server version skew, jar-declared Geyser detection). |
| [`app/BEDROCK_TUNNEL.md`](app/BEDROCK_TUNNEL.md) | The Bedrock (RakNet/UDP) relay tunnel: the Worker-dialed QUIC DATAGRAM tunnel and its handshake, the per-server public UDP ingress and per-client flow table, datagram framing and the MTU budget, the ingress abuse caps, and the config-drift decision. Companion to `RELAY.md` (the Java path). |
| [`app/CONFIGURATION.md`](app/CONFIGURATION.md) | Runtime configuration for `api/` and `worker/`: sources and precedence, secret handling, config-driven adapter selection (Storage backend, token service, execution drivers), the authentication-hardening knobs and defaults, and snapshot-cadence settings. |
| [`app/CONTROL_PLANE.md`](app/CONTROL_PLANE.md) | The API↔Worker control-plane contract: the single gRPC bidirectional-stream service, its connect/register/heartbeat/reconnect lifecycle, the command and event messages, error reporting, and how each maps to the requirements. The binding contract is the `proto/` buf module. |
| [`app/DATABASE.md`](app/DATABASE.md) | The persistence model for the core entities (`REQUIREMENTS.md` Appendix B): tables, keys, relationships, the desired/observed-state split on `Server`, cascade behavior, and the persistence-technology decision (PostgreSQL behind the persistence Port, schema applied by Alembic). Metadata only — bulk artifacts live in `Storage`. |
| [`app/RELAY.md`](app/RELAY.md) | The game ingress relay: per-server hostnames under a wildcard domain, the public relay and Worker dial-back tunnel that let NAT'd Workers serve players without exposing their IP, hostname routing via the Minecraft handshake, status-ping caching, session recording for moderation, and the relay↔API gRPC contract. |
| [`app/SECURITY.md`](app/SECURITY.md) | Authentication-hardening behaviour for `REQUIREMENTS.md` FR-AUTH-4: password-policy semantics, the brute-force/lockout algorithm, trusted-proxy client-IP resolution, and the decision on where the brute-force/lockout runtime state lives (DB-backed, behind a Port). Also the Minecraft-server-container trust model: the two-network split that keeps user-supplied plugins off the control plane, and what it deliberately leaves open. |
| [`app/STORAGE.md`](app/STORAGE.md) | The API-side authoritative store: the `Storage` Port contract, the authoritative data layout, atomic snapshot publish, file version retention, path-traversal protection, the fs / remote-fs / object adapter families, and the HTTP data plane. |

## Development docs (`dev/`)

| Doc | What it covers |
|---|---|
| [`dev/CONTRIBUTING.md`](dev/CONTRIBUTING.md) | The change workflow: issues, branch naming, commits, pull requests, review hygiene, and squash-merge. |
| [`dev/AGENTS.md`](dev/AGENTS.md) | Agent-facing operational manual complementing `CONTRIBUTING.md`: the primary-checkout / live-deploy ground rules, worktree lifecycle, the commands that fail silently (the mutation-revert trap and the commit-first ordering that avoids it, the shared scratchpad's generically named files, `--no-verify`, self-matching `pgrep -f`, the gate orphaned by a killed `git push`, `uv run --active`), gh/account quirks, and the pre-PR checklist. |
| [`dev/TESTING.md`](dev/TESTING.md) | The test-driven development discipline (Kent Beck): the red/green/refactor cycle, working disciplines, Tidy First, and what a good test looks like. Concrete tooling is per-component (`make test`; see the module READMEs). |
| [`dev/RELEASING.md`](dev/RELEASING.md) | Versioning policy (a single repository-wide SemVer version), tag naming, and generated release notes (no hand-maintained CHANGELOG). The tag-driven release workflow; the git tag is the version source of truth. A release does not build component artifacts — deployment builds them from the checked-out revision. |
| [`dev/DEPENDENCIES.md`](dev/DEPENDENCIES.md) | Pinning style, the 7-day supply-chain cooldown, security-update handling, and the automated-update policy across the Python, Go, and Node ecosystems. |
| [`dev/DEVELOPMENT.md`](dev/DEVELOPMENT.md) | Day-to-day developer workflow: prerequisites and first-time setup, the common command table (root unified commands + per-module READMEs), where code lives, the import-direction rules and how to run them, and the proto regeneration loop. |
| [`dev/DEPLOYMENT.md`](dev/DEPLOYMENT.md) | Single-host Docker Compose deployment: the `db` / `api` / `worker` stack (with the SeaweedFS object-storage backend and the optional `relay` profile), `.env` setup, bring-up and first-run admin bootstrap, how Minecraft server ports reach clients, TLS guidance for the browser and control planes, and the upgrade, backup, export/import, relay, and Bedrock procedures. |

---

## Conventions

- **Language**: all documentation is English.
- **Naming**: *v2* is the product name (the repository is
  `mc-server-dashboard-v2`), not a version number; release versions are SemVer
  tags (`dev/RELEASING.md`). Never write "v1".
- **Filenames**: `UPPERCASE_SNAKE_CASE.md`. The subdirectory names (`app/`,
  `dev/`) are lowercase.
- **Section references**: write `Section 4.3` (or `section 4.3` mid-sentence).
  Do not use the section-mark glyph — it is uncommon on US keyboards and noisy
  to search for.
- **Cross-links**: use relative paths (`[RELEASING.md](RELEASING.md)` within
  the same subdirectory, `[REQUIREMENTS.md](../REQUIREMENTS.md)` across them).
