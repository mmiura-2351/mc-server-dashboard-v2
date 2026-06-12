# Deployment

How to run mc-server-dashboard v2 on a single host with Docker Compose. This is
the minimum container-first deployment (issue #189): one `db`, one `api`, one
`worker`, and the Minecraft server containers the worker creates at runtime. It
covers the in-compose single-host topology; a reverse proxy, TLS termination for
the public HTTP surface, and multi-host workers are out of scope here (see the
TLS section for the cross-host control-plane requirement).

## 1. Architecture in one paragraph

`db` is PostgreSQL, the API's authoritative metadata store. `api` is the FastAPI
app plus the gRPC control-plane server. `worker` is the execution agent: it dials
the API's control plane, and in this deployment it runs the **container driver
only** — it creates each Minecraft server as a sibling container via the host
Docker daemon, mounting the server's working directory and publishing its game
port. The worker attaches every MC container to the pinned compose network
(`mcsd`) and reaches each server's RCON by container name over that network, so
**RCON never leaves the docker network** (the host RCON publication is dropped;
see Section 6). The `migrate` service is a one-shot that applies the database
schema before `api` starts.

### CPU priority for game-server containers

The worker creates every Minecraft container with an elevated CPU weight
(`CpuShares` 2048, double the Docker default of 1024), so a game server wins CPU
contention against batch workloads — CI builds, test suites, image builds —
sharing the same host. This was added after a running server starved under heavy
host load: the MC server thread stalled tens of seconds ("Running 837 ticks
behind") and players keepalive-dropped. Shares are a **relative weight, not a
cap** (the Engine translates them to `cpu.weight` on cgroup v2): they only
arbitrate who wins when the CPU is saturated. They do **not** raise absolute
capacity, so running heavy builds on the game host still degrades a server's
throughput — the weight just keeps the game ahead of the batch work. Do not rely
on it as a substitute for keeping heavy build pipelines off the game host.

## 2. Prerequisites

- A Linux host with Docker Engine and the Compose plugin (`docker compose`).
- The host user in the `docker` group (or run compose with sufficient
  privileges). The worker container needs access to the Docker socket.
- Outbound network access from the host: the API fetches Minecraft/Paper version
  manifests and JARs, and the worker pulls the per-Java base image on first use.
  The first start of a given Java tier therefore needs outbound network and may
  take minutes while the base image downloads (hundreds of MB); the image is
  cached on the host afterwards. On an offline host the start fails with an
  `image_missing` error.

To warm the cache up front on a production host so the first start of each tier
is instant, pre-pull the base images (the set comes from the worker's
`driver.container.images` config; the defaults are below):

```sh
for img in eclipse-temurin:8-jre eclipse-temurin:11-jre eclipse-temurin:16-jdk \
           eclipse-temurin:17-jre eclipse-temurin:21-jre eclipse-temurin:25-jre \
           azul/zulu-openjdk:7; do
  docker pull "$img"
done
```

## 3. Configure `.env`

Copy the template and fill every value:

```sh
cp .env.example .env
```

| Variable | What it is | How to get it |
|---|---|---|
| `POSTGRES_PASSWORD` | Database password | `openssl rand -base64 32` |
| `MCD_API_AUTH__TOKEN__SIGNING_KEY` | JWT signing key (HS256, >= 32 bytes) | `openssl rand -base64 48` |
| `MCD_API_CONTROL__WORKER_CREDENTIAL` | Shared secret authenticating the worker | `openssl rand -base64 48` |
| `MCSD_SCRATCH_DIR` | Absolute host path for the worker scratch dir | choose a path, e.g. `/opt/mcsd/scratch` |
| `DOCKER_GID` | GID of the host `docker` group | `getent group docker \| cut -d: -f3` |
| `API_HTTP_PORT` | Published host port for the API HTTP surface | default `8000` |

`POSTGRES_USER` and `POSTGRES_DB` default to `mcsd`; `MCD_API_CONTROL__WORKER_CREDENTIAL`
is reused by the worker as its `MCD_WORKER_API_CREDENTIAL` (wired in
`compose.yaml`), so both sides share the one secret.

The scratch directory must exist on the host before the first `up` so the bind
mount resolves; create it as the user the worker runs as:

```sh
mkdir -p /opt/mcsd/scratch    # must match MCSD_SCRATCH_DIR
```

### Why the scratch dir is bind-mounted at an identical path

The worker tells the Docker daemon to bind each server's working directory
(`<MCSD_SCRATCH_DIR>/<server-id>`) into the Minecraft container. The daemon
resolves bind sources against **host** paths, not the worker container's
filesystem. Mounting `${MCSD_SCRATCH_DIR}:${MCSD_SCRATCH_DIR}` makes the worker's
in-container path identical to the real host path, so the binds it requests are
valid. The worker's stable id is persisted at `<MCSD_SCRATCH_DIR>/worker-id` on
first boot; because the scratch dir is a host bind mount, that id survives worker
container recreation, so the worker re-registers under the same identity after a
restart or rebuild.

## 4. Bring the stack up

`docker compose` builds the images from this checkout (the repo root is the
deploy source), so the checkout must be on a clean `main` — a stray branch or
dirty tree silently ships the wrong ref (CONTRIBUTING.md Section 3, issue #432).
Run the preflight, which refuses (exit 1) when the checkout is not on `main` or
is dirty, then build:

```sh
./scripts/deploy_preflight.sh && docker compose up -d --build
```

This builds the `api` and `worker` images, starts `db`, runs `migrate` to apply
the schema, then starts `api` and `worker`. Check status and logs:

```sh
docker compose ps
docker compose logs -f api worker
```

The API HTTP surface is then on `http://<host>:${API_HTTP_PORT}` (default 8000);
the entire HTTP API is namespaced under `/api` (issue #498), so `GET
/api/healthz` returns the liveness + database-reachability probe.

### How the Web UI ships

The browser UI is served by the `api` container itself — there is **no separate
UI service and no reverse proxy** (WEBUI_SPEC 7.7, issue #386). The `api` image
is multi-stage: a Node stage builds the React SPA (`webui/dist`, Node major pinned
by `webui/.nvmrc`, npm pinned by `webui/package.json` `engines`), and the runtime
stage copies that build in. `compose.yaml` points the API at it with
`MCD_API_WEBUI__DIST_DIR=/app/webui/dist`, so the SPA is served on the **same
origin** as the API at `http://<host>:${API_HTTP_PORT}/`.

The entire HTTP API is namespaced under `/api` (issue #498), so `/api/*` is the
API (REST, WebSocket, the OpenAPI schema/docs, and the health/readiness/metrics
probes) and `/assets/*` is the built SPA chunks — both are excluded from the SPA
fallback. A `/api/*` miss is a wrong/removed route and an unmatched `/assets/*`
request returns 404 (a stale/renamed chunk, never a client-side route; issue
#634); *every other unmatched path* falls back to the SPA's `index.html` so
client-side routing works on deep links and reloads with no path ever colliding. Same-origin
serving is why the API ships **no CORS** and the refresh cookie is
`SameSite=Strict; Path=/api/auth` — do not add CORS or split the origin (WEBUI_SPEC
7.7). The build context for the `api` image is therefore the repo **root** (so the
build can reach `webui/`), not `api/` — see `compose.yaml` and `api/Dockerfile`.

When `MCD_API_WEBUI__DIST_DIR` is unset (the default outside compose), the API
mounts nothing and serves only the API surface — that is the development posture,
where Vite serves the UI and proxies the API (WEBUI_SPEC 7.7).

## 5. First-run bootstrap (create the platform admin)

There is no seeded admin and **no manual database step**. The first user
registered over HTTP on a fresh database automatically becomes the platform
admin (issue #909); just register it:

```sh
curl -X POST http://localhost:8000/api/users \
  -H 'Content-Type: application/json' \
  -d '{"username": "admin", "email": "admin@example.com", "password": "<a-strong-password>"}'
```

The response carries `"is_platform_admin": true` for this first account. The
auto-grant is race-safe (concurrent first registrations produce exactly one
admin) and recorded in the audit log (a `user:platform_admin_grant` entry). It
is keyed on *no users existing yet*, not *no admin existing*: once any user
exists, a later registration is never auto-promoted, so deleting or demoting
admins cannot silently re-open the bootstrap.

**Closed registration**: if you run with `auth.registration.open=false` (the
admin-provisioned posture, CONFIGURATION.md Section 7.4), the *first* registration
on an empty database is still allowed — it is the only way to create the bootstrap
admin and shares the same trust model as the old manual step. The open flag is
enforced normally for every registration after the first user exists.

From here on, that admin manages all further accounts through the authenticated,
audited admin API (granting/revoking the admin flag, deactivating/reactivating,
deleting, and listing users; `PUT /api/users/{id}/platform-admin`,
`POST /api/users/{id}/deactivate` and friends; issue #278) — for example,
promoting an additional admin:

```sh
curl -X PUT http://localhost:8000/api/users/<user-id>/platform-admin \
  -H 'Authorization: Bearer <admin-access-token>' \
  -H 'Content-Type: application/json' \
  -d '{"grant": true}'
```

### Accepting the Minecraft EULA on first run

Mojang's server refuses to start until you accept its EULA: a fresh server writes
`eula.txt` with `eula=false` and exits. The primary path is to accept the EULA at
creation — pass `accept_eula: true` on `POST /api/communities/{cid}/servers`, which
seeds `eula.txt` with `eula=true` into the server's initial working set so the
first start does not crash. Acceptance is recorded as part of the audited create.

If you create a server without `accept_eula`, the first start still crashes on the
default `eula=false`; recover by editing `eula.txt` to `eula=true` (the file API)
and starting again.

### Forge servers install on first start

A Forge server type resolves to the Forge **installer** JAR (not a directly
launchable server JAR): the API ships it into the working set at `server.jar`, and
the worker runs the supervised `--installServer` step the first time the server
starts (the `forge-argsfile` launch mode). The installer produces the Forge
libraries tree and the generated args file; the worker then launches via that args
file. Subsequent starts skip the install (the args file is already present).

The first start therefore takes noticeably longer than a vanilla/Paper start while
the installer downloads Forge's libraries. The installer's combined output is
written to `logs/forge-install.log` in the server's working set, readable through
the file API — check it if a Forge first start fails or stalls.

The Forge installer forks Java grandchild processes that can outlive their parent
and re-parent to the worker. In the `compose.yaml` deployment this is handled by
`init: true` on the worker service (Docker injects tini as PID 1). If you run the
worker outside Compose — as a bare-metal process or in a container launched by hand
— ensure it is started under an init process (e.g. `tini -- ./worker`) or pass
`--init` to `docker run`. Without an init, these grandchildren become zombies that
accumulate until the worker process exits.

## 6. How Minecraft server ports reach clients

The worker's container driver reads each server's `server.properties` and
publishes its `server-port` (Minecraft default 25565) from the MC container to
the host. Players connect to the host on the server's game port. Because these MC
containers are created at runtime — not declared in `compose.yaml` — the host
firewall must allow inbound traffic to whichever game ports your servers use.

**Distinct ports are now automatic at create.** The API tracks each server's game
port (`server.game_port`, DATABASE.md Section 7) and, at create, assigns the
lowest free in-range port (configurable via `ports.range_start`/`ports.range_end`,
default `25565..25664`; CONFIGURATION.md Section 5.8), unique deployment-wide, and
seeds `server-port=<port>` into the new server's `server.properties`. So
operator-created servers no longer need manual `server-port` editing to avoid
host-port collisions. An operator may still pass an explicit `game_port` at create
(rejected 422 out of range, 409 taken); a delete frees the port for reuse.

**Changing a server's port after create.** A stopped server can be re-ported via
`PATCH /api/communities/{id}/servers/{id}` with a `game_port` field (issue #311). It
validates the new port like create (422 out of range, 409 taken), rewrites
`server-port` in the at-rest `server.properties`, and updates `server.game_port`
together, so under normal operation the DB and the real bind port stay in sync.
The server must be at rest (a running server is 409 `server_not_stopped`). This is
the preferred way to re-port — it keeps the tracked port and the file aligned,
unlike editing `server.properties` by hand.

The file write and the DB commit are not atomic: if a concurrent
`UNIQUE(game_port)` race loses at commit (response 409 `port_taken`),
`server.properties` may already hold the new port while the row keeps the old —
the only residual drift mode. It is recoverable: retry the PATCH, which rewrites
both to a consistent state.

For an **imported or legacy server** whose row predates port tracking
(`game_port` is `NULL`), nothing is auto-assigned. Prefer the update-port API
above to set its port (it backfills `game_port` and rewrites `server.properties`
together); the manual SQL backfill below is the fallback when you have already set
`server-port` directly in the file.

### Backfilling legacy `game_port` rows

A row with `game_port = NULL` is **invisible to port auto-assignment**: it is
excluded from the deployment-wide taken-port set, so the next auto-assigned
server can be handed the very host port the legacy server already binds (via its
`server.properties`) — a guaranteed host-port collision when both run. To make
the gap discoverable, the API logs a **startup WARN** listing the count and ids
of every `game_port = NULL` server. When you see it, backfill those rows so the
taken-set math becomes correct again.

The **preferred fix** is the update-port API (`PATCH .../servers/{id}` with
`game_port`), which sets `game_port` and rewrites `server-port` in one validated,
in-sync step. Use the manual SQL below only when you have already set the port
directly in `server.properties` and just need the DB row to match: read each
listed server's current bind port from `server.properties` (the
`server-port=<port>` line, via the files API) and write it into `game_port`:

```sql
UPDATE server SET game_port = <port-from-its-server.properties> WHERE id = '<id>';
```

The `game_port` column is `UNIQUE` deployment-wide, so a backfill that would
duplicate another server's port fails loudly — resolve the duplicate before
retrying. After backfilling, the WARN stops on the next restart.

The host interface the **game port** binds to is configurable via
`driver.container.game_bind_ip` (env `MCD_WORKER_DRIVER_CONTAINER_GAME_BIND_IP`).
The in-code default is `127.0.0.1` (loopback-only); this `compose.yaml` overrides
it to `0.0.0.0` so a started server accepts players out of the box, leaving the
host firewall to govern which game ports are actually exposed.

The **RCON port** is the worker's control channel and is never exposed off-host.
Its handling depends on `driver.container.network` (env
`MCD_WORKER_DRIVER_CONTAINER_NETWORK`):

- **Set** (this `compose.yaml`, value `mcsd`): the worker attaches each MC
  container to that user-defined network and dials RCON at the container's name
  over the network. The host RCON publication is **dropped** — RCON never leaves
  the docker network. This is required for the containerized worker, whose own
  loopback is not the host loopback where a published RCON port would land
  (issue #218). The compose default network's name is pinned to `mcsd` so the
  worker (a compose service) and the sibling MC containers it creates share the
  same network with container-name DNS. The network **must be user-defined**
  (a `docker network create` network, as the pinned `mcsd` is): the default
  `bridge` has no container-name DNS, so pointing this at `bridge` lets the
  attach succeed but the RCON dial silently fails.
- **Unset** (bare-metal worker): RCON is published to the host loopback
  (`127.0.0.1`) and dialed there, the historical behavior.

## 7. TLS guidance

The in-compose deployment runs the control plane in plaintext on the private
compose network: `api` sets `MCD_API_CONTROL__TLS__INSECURE=true` and `worker`
sets `MCD_WORKER_API_TLS_INSECURE=true`. This is acceptable only because the
gRPC control listener is not published to the host and the traffic stays on the
internal Docker network.

A **multi-host** worker (a worker on a different machine dialing this API over a
real network) must not use the insecure posture, and the gRPC control plane must
not be exposed off-host while it is still plaintext. Reaching a cross-host worker
requires **both** of the following together — never publish the gRPC port
without first putting TLS on the listener:

1. Configure control-plane TLS:
   - On the API, serve the control listener over TLS: set
     `MCD_API_CONTROL__TLS__CERT_FILE` and `MCD_API_CONTROL__TLS__KEY_FILE`
     (both are required together) and drop `MCD_API_CONTROL__TLS__INSECURE`.
   - On the remote worker, set `MCD_WORKER_API_TLS_CA_FILE` to the CA bundle
     that verifies the API's certificate, and drop `MCD_WORKER_API_TLS_INSECURE`.
2. Only then publish or route the gRPC port to the remote worker by adding a
   `50051` entry to the `api` service's `ports` in `compose.yaml` (the single-host
   stack deliberately omits it). With TLS in place this exposes an authenticated,
   encrypted listener rather than the plaintext one.

Mount the certificate, key, and CA files into the respective containers and point
the variables at the in-container paths.

## 8. Upgrade

Pull the new revision and rebuild; `migrate` re-runs `alembic upgrade head`
before the new `api` starts, so the schema is brought current automatically:

### Deploy-order rule: API before (or with) worker when new CommandErrorCodes are added

`compose.yaml` brings `api` up before `worker`, so the default `docker compose
up -d --build` already applies the correct order. However, if you update
containers individually, always update `api` first (or together with `worker`).
An old API receiving a `CommandErrorCode` it does not recognise falls back to
`INTERNAL` and its compensation logic may orphan a live instance — the #866 BUSY
precedent: an old API that had no BUSY handling treated it as INTERNAL and
unassigned the server, stranding the running instance. Updating `api` first (or
atomically via `docker compose up`) ensures the API's handler for any new code
is in place before the worker starts emitting it.

```sh
git pull
./scripts/deploy_preflight.sh && docker compose up -d --build
```

Stacks that were first deployed before the `api` image pre-created the storage
mount point have an `api-storage` volume owned by root, so the non-root app
(uid 10001) cannot write to it. Fix the ownership once, then bring the stack up:

```sh
docker run --rm -v mc-server-dashboard-v2_api-storage:/fix \
  debian:bookworm-slim chown 10001:10001 /fix
```

(The volume name is `<project>_api-storage`; `docker volume ls` shows the exact
names for your project directory.)

## 9. Backups

Two pieces of persistent state matter, both Docker named volumes:

- `db-data` — the PostgreSQL data (all metadata).
- `api-storage` — the authoritative file storage (`MCD_API_STORAGE__FS__ROOT`),
  including server files, backups, and snapshots.

Back up the database with a logical dump and archive the storage volume. For
example:

```sh
docker compose exec db pg_dump -U mcsd -d mcsd > backup-db.sql
docker run --rm -v mc-server-dashboard-v2_api-storage:/data \
  -v "$PWD":/backup debian:bookworm-slim \
  tar czf /backup/backup-storage.tar.gz -C /data .
```

(The volume name is `<project>_api-storage`; `docker volume ls` shows the exact
names for your project directory.) The worker scratch dir
(`MCSD_SCRATCH_DIR`) is a working set rebuilt from the API on demand and does not
need backing up beyond the persisted `worker-id`.

## 10. Server export / import (ZIP)

A whole server moves in and out as a single ZIP archive:

- **Export** — `GET /api/communities/{community_id}/servers/{server_id}/export`
  streams a ZIP of the server's authoritative working set plus an
  `export_metadata.json` descriptor. Export is at-rest only: a running server is
  refused (409) because the authoritative copy is only well-defined when stopped.
- **Import** — `POST /api/communities/{community_id}/servers/import` takes a multipart
  ZIP upload, creates a fresh server (auto-assigned game port; EULA is **not**
  implied — the imported working set carries its own `eula.txt` if any), and
  publishes the archive contents as the new server's initial working set. The new
  server's `name` and `execution_backend` come from the request, not the archive.

### Export format (`format: 1`)

`export_metadata.json` lives at the root of the ZIP and carries:

| field         | meaning                                              |
| ------------- | ---------------------------------------------------- |
| `format`      | the format version — currently `1`                   |
| `name`        | the source server's name (informational; import uses the request name) |
| `mc_edition`  | the Minecraft edition (`java`)                       |
| `mc_version`  | the Minecraft version                                |
| `server_type` | the server type (`vanilla` / `paper` / `fabric` / `forge`) |
| `exported_at` | the export timestamp (ISO 8601, UTC)                 |

On import the `format` field must equal `1`, and `server_type` / `mc_version` are
re-validated against the version catalog (the same check `create` runs), so an
unsupported type — e.g. `spigot` — is rejected. The `export_metadata.json` member
itself is never written into the new working set.

**Legacy incompatibility (one honest line):** archives produced by the legacy
system carry a different metadata shape and are **not** importable here; a
converter to the `format: 1` shape can be written later against this spec.
