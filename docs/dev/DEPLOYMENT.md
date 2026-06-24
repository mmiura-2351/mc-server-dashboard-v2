# Deployment

How to run mc-server-dashboard v2 on a single host with Docker Compose. This is
the minimum container-first deployment (issue #189): one `db`, one `api`, one
`worker`, and the Minecraft server containers the worker creates at runtime. It
covers the in-compose single-host topology; multi-host workers are out of scope
here. TLS for the browser UI plane is covered in
[Section 8](#8-tls-guidance) (HTTPS is required for the default cookie
configuration); the cross-host gRPC control-plane TLS requirement is also
there.

## 1. Architecture in one paragraph

`db` is PostgreSQL, the API's authoritative metadata store. `api` is the FastAPI
app plus the gRPC control-plane server. `worker` is the execution agent: it dials
the API's control plane, and in this deployment it runs the **container driver
only** — it creates each Minecraft server as a sibling container via the host
Docker daemon, mounting the server's working directory and publishing its game
port. The worker attaches every MC container to the pinned compose network
(`mcsd`) and reaches each server's RCON by container name over that network, so
**RCON never leaves the docker network** (the host RCON publication is dropped;
see Section 7). The `migrate` service is a one-shot that applies the database
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
| `COMPOSE_PROFILES` | Active compose profiles; ships as `object` to run the SeaweedFS service. Set empty to drop it for the fs backend | leave as `object` (default) |
| `MCD_API_STORAGE__OBJECT__ACCESS_KEY` | S3 access key for the object backend (required when `COMPOSE_PROFILES=object`) | `openssl rand -hex 16` |
| `MCD_API_STORAGE__OBJECT__SECRET_KEY` | S3 secret key for the object backend (required when `COMPOSE_PROFILES=object`) | `openssl rand -hex 16` |
| `MCSD_SCRATCH_DIR` | Absolute host path for the worker scratch dir | choose a path, e.g. `/opt/mcsd/scratch` |
| `DOCKER_GID` | GID of the host `docker` group | `getent group docker \| cut -d: -f3` |
| `API_HTTP_PORT` | Published host port for the API HTTP surface | default `8000` |

`POSTGRES_USER` and `POSTGRES_DB` default to `mcsd`; `MCD_API_CONTROL__WORKER_CREDENTIAL`
is reused by the worker as its `MCD_WORKER_API_CREDENTIAL` (wired in
`compose.yaml`), so both sides share the one secret. The two
`MCD_API_STORAGE__OBJECT__*` keys are the S3 credentials for the **default object
storage backend**; they are required only while `COMPOSE_PROFILES=object` (the
default) and unused after the fs opt-out — see
[Section 5](#5-storage-backend-object-on-seaweedfs-default).

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

## 5. Storage backend: `object` on SeaweedFS (default)

The shipped deployment stores all server working sets, snapshots, and backups in
the **`object` storage backend** (`storage.backend: object`, STORAGE.md
[Section 7.3](../app/STORAGE.md#73-object-object-storage)), realized over the
in-compose **SeaweedFS** S3 gateway. SeaweedFS is Apache-2.0 and
designed for many small files, which fits this workload — a Minecraft world is
thousands of small `region/`/`poi/`/`entities/` `.mca` objects, and each publish
server-side-copies them into a fresh snapshot prefix and flips one pointer object.

### Quick start (the default — nothing extra to do)

The `seaweedfs` service is gated behind the `object` compose profile, which
`.env.example` ships active via `COMPOSE_PROFILES=object`; with that default,
`docker compose up` provisions it alongside `db`/`api`/`worker`. You only need to
set the two S3 credential keys in `.env` (Section 3):

```sh
# in .env (COMPOSE_PROFILES=object is already the .env.example default)
MCD_API_STORAGE__OBJECT__ACCESS_KEY=<openssl rand -hex 16>
MCD_API_STORAGE__OBJECT__SECRET_KEY=<openssl rand -hex 16>
```

Blank keys fail loudly at boot: the seaweedfs entrypoint refuses a blank-key
identities file, and the api refuses to start the object backend with blank creds
(naming the missing variables). SeaweedFS writes its S3 identities file from these
at startup (so the secrets live only in `.env`, matching the database password),
and auto-creates the `mcsd` bucket on first write. The `api` service waits for the
`seaweedfs` healthcheck before it boots (a `required: false` dependency, so the api
still starts cleanly after the fs opt-out drops the service). No bucket
pre-creation or init job is required: on a fresh store the bucket does not yet
exist, every **read** against it returns `NoSuchBucket`, and the adapter treats
that as empty/not-found so the API's startup sweep boots cleanly — the first
publish then creates the bucket (issue #946). A **non-SeaweedFS** S3 backend that
does not auto-create buckets must have the bucket **pre-provisioned** before the
API starts.

The data lives in the `seaweedfs-data` volume — include it in your backups
(Section 10).

### Operational trade-off: cost/perf scales with operation count

The object backend's cost and latency are driven by **operation count**, not
storage size or egress: every snapshot **server-side-copies each world file**
(CopyObject) into a fresh prefix and uploads new members via multipart, so the
work per snapshot is `O(number of world files)`. A busy world with tens of
thousands of region files multiplies that by your **snapshot frequency**.

Guidance: keep the periodic snapshot interval coarse enough that a snapshot
completes well within the interval (the publish copy is the long pole). If you
push snapshot frequency up, watch the SeaweedFS volume server's CPU and the
publish duration in the API logs rather than the bucket size. The app implements
its own snapshot/version logic, so S3 versioning / object-lock / lifecycle are
**not** used — SeaweedFS's lack of them is a non-issue, except for the orphan
multipart sweep below.

### Orphan multipart parts

A hard crash mid-upload can leave in-progress multipart parts. The API's startup
sweep reclaims them via `ListMultipartUploads` + `AbortMultipartUpload`, aborting
only uploads older than a 1h age threshold so a live upload is never touched.

SeaweedFS 4.33 returns `ListMultipartUploads` **without** the per-upload
`Initiated` timestamp, so the sweep cannot read the age directly. It instead
derives the effective age from the upload's parts via `ListParts` (SeaweedFS does
return a per-part `LastModified`), using the newest part's timestamp — so a
genuine crash-orphan with parts **is** reclaimed on SeaweedFS, not just on real
S3/MinIO. One residual gap: an upload that crashes after `CreateMultipartUpload`
but **before** its first part has no `Initiated` and no part timestamp, so the
sweep treats it as just-started and leaves it. Such an entry holds no part bytes.
If you want to reclaim those on a schedule too, `weed shell s3.clean.uploads` is
the SeaweedFS-native operator-side cleanup (it removes incomplete uploads older
than its default 24h); it is optional and complementary to the API sweep.

### Opting back to the fs backend

To run the local-volume **`fs`** backend instead (the previous default; STORAGE.md
Section 2), set both of these in `.env` — clear `COMPOSE_PROFILES` and pin the
backend:

```sh
# in .env
COMPOSE_PROFILES=
MCD_API_STORAGE__BACKEND=fs
```

Clearing `COMPOSE_PROFILES` drops the `seaweedfs` service entirely (it is gated
behind the `object` profile), so the stack no longer runs — or waits on — a
SeaweedFS instance it does not use; the api's dependency on it is `required: false`,
so the api starts cleanly. With the service gone, the S3 credential keys
(`MCD_API_STORAGE__OBJECT__*`) are **not required** and may stay blank.

The fs root (`MCD_API_STORAGE__FS__ROOT=/data/storage`) and its `api-storage`
volume stay wired in `compose.yaml`, so no other change is needed. Recreate the
stack to apply:

```sh
docker compose up -d --remove-orphans
```

`--remove-orphans` clears the now-deselected `seaweedfs` container if it was
running before the opt-out (its `seaweedfs-data` volume is left intact).

### Caveat: switching an existing deployment is a data cutover

Changing the backend on an **already-running** deployment (fs → object, or back)
does **not** migrate existing data. Each backend stores into its own place — the
`api-storage` volume for fs, the `seaweedfs-data` volume (S3 bucket) for object —
and there is no automatic copy between them. After a switch, the API sees an empty
store: existing servers have no published snapshot until they are re-hydrated or
re-created, and existing backups are not visible. Migration tooling between
backends is out of scope. Treat a backend switch on a deployment that holds real
data as a deliberate cutover, and back up both volumes first.

### Running the live SeaweedFS contract tests

`api/tests/storage/test_object_live_seaweedfs.py` exercises the load-bearing
object-store assumptions (read-after-write on the pointer overwrite PUT,
server-side CopyObject, multipart + prefix list, and the startup sweep) against a
real endpoint. It is skipped unless `MCD_TEST_S3_ENDPOINT` is set, so `make check`
and CI stay green without an S3 instance. To run it against a throwaway SeaweedFS:

```sh
docker run -d --name swfs-test -p 8333:8333 \
  -e AK=testak -e SK=testsk --entrypoint sh chrislusf/seaweedfs:4.33 -c \
  'mkdir -p /etc/seaweedfs && printf "{\"identities\":[{\"name\":\"t\",\"credentials\":[{\"accessKey\":\"%s\",\"secretKey\":\"%s\"}],\"actions\":[\"Admin\",\"Read\",\"Write\",\"List\",\"Tagging\"]}]}" "$AK" "$SK" > /etc/seaweedfs/s3.json && exec weed server -dir=/data -s3 -s3.config=/etc/seaweedfs/s3.json'

cd api && MCD_TEST_S3_ENDPOINT=http://localhost:8333 \
  MCD_TEST_S3_ACCESS_KEY=testak MCD_TEST_S3_SECRET_KEY=testsk \
  uv run pytest tests/storage/test_object_live_seaweedfs.py

docker rm -f swfs-test
```

## 6. First-run bootstrap (create the platform admin)

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

## 7. How Minecraft server ports reach clients

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

## 8. TLS guidance

### HTTPS requirement for the browser UI

The browser UI **must be reached over HTTPS** for the default configuration to
work. The refresh cookie is issued with `Secure; HttpOnly; SameSite=Strict;
Path=/api/auth`
(`auth.py`, `config.py` `refresh_cookie_secure=True`). Over plain HTTP the
browser refuses to store a `Secure` cookie, so the silent token refresh always
fails and the user is forced to re-login when the access token expires (~900 s /
15 minutes). This is not an idle timeout — there is no such feature; the user is
hard-logged-out because the refresh cookie was never stored.

#### Cloudflare Tunnel (recommended)

The `cloudflared` service in `compose.yaml` (issue #1090) is the supported
HTTPS path. It is gated behind the `tunnel` compose profile (the relay uses a
separate `relay` profile).

How it works: the browser reaches the Cloudflare edge over HTTPS (the public
hostname is configured in the Cloudflare Zero Trust dashboard); `cloudflared`
runs inside the compose network and forwards traffic to `api:8000` over plain
HTTP on the internal Docker network. No inbound port, no TLS certificate, and
no reverse proxy are needed on the host.

To enable:

1. Add `tunnel` to `COMPOSE_PROFILES` in `.env`:

   ```sh
   # example: object backend + Cloudflare Tunnel
   COMPOSE_PROFILES=object,tunnel
   ```

2. Create a tunnel in the Cloudflare Zero Trust dashboard, add a public
   hostname pointing to `http://api:8000`, and copy the tunnel token.

3. Set the token in `.env`:

   ```sh
   CLOUDFLARE_TUNNEL_TOKEN=<token from the dashboard>
   ```

4. Rebuild:

   ```sh
   docker compose up -d --build
   ```

The browser now reaches the UI over HTTPS at the public hostname, the `Secure`
cookie is stored, and silent refresh works.

#### Reverse proxy + Let's Encrypt (alternative)

For deployments that do not use Cloudflare, any TLS-terminating reverse proxy
(Caddy, nginx, Traefik, etc.) in front of the API's HTTP port achieves the same
result. The proxy terminates TLS with a certificate from Let's Encrypt (or
another CA) and forwards to `http://localhost:${API_HTTP_PORT}`. This is a
standard reverse-proxy setup and is not detailed here.

#### HTTP-only fallback (LAN / development)

For plain-HTTP deployments (local network, development) where HTTPS is not
available, set:

```sh
MCD_API_AUTH__TOKEN__REFRESH_COOKIE_SECURE=false
```

This drops the `Secure` attribute from the refresh cookie so the browser stores
it over HTTP and silent refresh works. **Security caveat:** the cookie is then
sent over plaintext, exposing the refresh token to network observers. Use this
only on trusted networks.

### gRPC control-plane TLS (cross-host worker)

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

## 9. Upgrade

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

> **Breaking change — the default storage backend is now `object` (SeaweedFS).**
> Before this revision the shipped default was the local-volume `fs` backend. The
> `api` service now resolves `MCD_API_STORAGE__BACKEND` to `object` **unless your
> `.env` pins it to `fs`**. An existing fs deployment that simply `git pull`s and
> rebuilds will start the API against an **empty** SeaweedFS store — its servers,
> snapshots, and backups live in the `api-storage` (fs) volume and do **not**
> migrate automatically (Section 5 "data cutover" caveat). To keep your existing
> fs data, opt back to fs **before** rebuilding — pin the backend and drop the
> SeaweedFS service (Section 5 "Opting back to the fs backend"):
>
> ```sh
> printf 'COMPOSE_PROFILES=\nMCD_API_STORAGE__BACKEND=fs\n' >> .env
> ```
>
> To deliberately adopt the object backend on an existing deployment, treat it as
> a cutover: back up both volumes first, then expect an empty store until servers
> are re-hydrated/re-created (Section 5).

> **Upgrade note — the `seaweedfs` service is now profile-gated.** The service is
> behind the `object` compose profile, which `.env.example` activates with
> `COMPOSE_PROFILES=object`. An existing **object** deployment whose `.env` predates
> this revision has no `COMPOSE_PROFILES` line; after `git pull` the next
> `docker compose up` would **not** start `seaweedfs` (an unset `COMPOSE_PROFILES`
> selects no profiles); the api boots and serves but all storage operations
> (snapshot, backup, file reads/writes) error at runtime because the S3 client
> cannot reach the object store. Add the line once before rebuilding:
>
> ```sh
> echo 'COMPOSE_PROFILES=object' >> .env
> ```
>
> (fs deployments set `COMPOSE_PROFILES=` empty instead — see the breaking-change
> box above.)

Stacks that were first deployed before the `api` image pre-created the storage
mount point have an `api-storage` volume owned by root, so the non-root app
(uid 10001) cannot write to it. Fix the ownership once, then bring the stack up:

```sh
docker run --rm -v mc-server-dashboard-v2_api-storage:/fix \
  debian:bookworm-slim chown 10001:10001 /fix
```

(The volume name is `<project>_api-storage`; `docker volume ls` shows the exact
names for your project directory.)

## 10. Backups

Two pieces of persistent state matter, both Docker named volumes:

- `db-data` — the PostgreSQL data (all metadata).
- The storage volume holding server files, backups, and snapshots — which volume
  depends on the active backend (Section 5): `seaweedfs-data` for the default
  `object` backend, or `api-storage` for the `fs` backend
  (`MCD_API_STORAGE__FS__ROOT`).

Back up the database with a logical dump and archive the storage volume. For the
default object backend:

```sh
docker compose exec db pg_dump -U mcsd -d mcsd > backup-db.sql
docker run --rm -v mc-server-dashboard-v2_seaweedfs-data:/data \
  -v "$PWD":/backup debian:bookworm-slim \
  tar czf /backup/backup-storage.tar.gz -C /data .
```

For the `fs` backend, archive the `api-storage` volume instead:

```sh
docker run --rm -v mc-server-dashboard-v2_api-storage:/data \
  -v "$PWD":/backup debian:bookworm-slim \
  tar czf /backup/backup-storage.tar.gz -C /data .
```

(The volume name is `<project>_api-storage`; `docker volume ls` shows the exact
names for your project directory.) The worker scratch dir
(`MCSD_SCRATCH_DIR`) is a working set rebuilt from the API on demand and does not
need backing up beyond the persisted `worker-id`.

## 11. Server export / import (ZIP)

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

## 12. Relay (game ingress, epic #659)

The relay lets players join at `<slug>.<base_domain>` (e.g.
`amber-falcon-42.mc.example.com`) with no port number and no client mods, and
it keeps the Worker's IP off the internet — including when the Worker runs
behind NAT. See `docs/app/RELAY.md` for the full design.

The relay is **opt-in**: the `relay` compose profile is inactive by default, so
all existing deployments are unaffected. Enable it only when you have a public
IP for the relay host and a wildcard DNS record in place.

### DNS setup

Create one wildcard `A`/`AAAA` record pointing to the relay host's public IP:

```
*.<base_domain>    A    <relay public IP>
```

Example: `*.mc.example.com → 203.0.113.7`. Server create/rename/delete never
touches DNS; the hostname-to-server mapping lives entirely in the database.
The relay's game listener binds port **25565** — which makes player joins
port-less.

### TLS material (tunnel listener)

The relay's tunnel listener (port 25665) always requires TLS. A self-signed
certificate is fine: the relay advertises the CA PEM to Workers in-band via the
`Register` → `TunnelDial` flow, so Workers need no extra configuration.

Generate a self-signed cert once on the host and place both files in a
directory you own (set `MCD_RELAY_TLS_DIR` in `.env` to this path):

```sh
mkdir -p /etc/mcsd/relay
chmod 755 /etc/mcsd/relay    # must be traversable by the container user (uid 10001)
# Replace <tunnel-host> with the hostname part of MCD_RELAY_TUNNEL_PUBLIC_ENDPOINT
# (e.g. relay.example.com). The SAN must match the host the Worker dials — Go
# ignores CN and requires a matching DNS or IP SAN.
# For a raw-IP endpoint use: -addext "subjectAltName=IP:<addr>"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 \
  -keyout /etc/mcsd/relay/tunnel-key.pem \
  -out    /etc/mcsd/relay/tunnel-cert.pem \
  -days 3650 -nodes \
  -subj "/CN=mcsd-relay-tunnel" \
  -addext "subjectAltName=DNS:<tunnel-host>"
# The relay container runs as non-root (uid 10001), so the key must be
# world-readable. The key is self-signed and scoped to intra-cluster tunnel
# traffic, so 644 is acceptable.
chmod 644 /etc/mcsd/relay/tunnel-key.pem /etc/mcsd/relay/tunnel-cert.pem
```

**Directory permissions matter.** The relay container runs as uid 10001 (`USER
app` in the Dockerfile). The host directory bind-mounted at `/etc/mcsd/` must
be **traversable** by that uid — `chmod 755` (or at least `a+rx`). A directory
with mode `0700` (common when created by `mktemp -d` or under a
security-hardened default umask) blocks the non-root container user from
reaching the files inside, even if the files themselves are `644`.

The cert and key are bind-mounted read-only into the relay container at
`/etc/mcsd/` by `compose.yaml`.

If you have a publicly-issued tunnel certificate (signed by a public CA), set
`tunnel.tls.advertised_ca_file = "system"` in `relay.toml` (or
`MCD_RELAY_TUNNEL_TLS_ADVERTISED_CA_FILE=system` in the environment) so the
relay advertises an empty CA bundle and Workers fall back to their system roots.

### Enabling the relay profile

1. Add `relay` to `COMPOSE_PROFILES` in `.env`:

   ```sh
   # to run both object backend and the relay:
   COMPOSE_PROFILES=object,relay
   ```

2. Fill the relay keys in `.env` (see `.env.example` for descriptions):

   | Variable | How to get it |
   |---|---|
   | `MCD_API_RELAY__CREDENTIAL` | `openssl rand -base64 48` |
   | `MCD_API_RELAY__ENABLED` | set to `true` |
   | `MCD_API_RELAY__BASE_DOMAIN` | e.g. `mc.example.com` |
   | `MCD_RELAY_TUNNEL_PUBLIC_ENDPOINT` | e.g. `<host>:25665` |
   | `MCD_RELAY_TLS_DIR` | host path to `tunnel-cert.pem` / `tunnel-key.pem` |

3. Rebuild and bring the stack up:

   ```sh
   docker compose up -d --build
   ```

### Single-host port collision

The relay binds `0.0.0.0:25565` for the game listener. The API's default
game-port allocator range is `25565..25664`, so the first server created on a
single host that also runs the relay will fail to publish its game port — even a
`127.0.0.1:25565` publish conflicts with an existing `0.0.0.0:25565` bind.

**Fix:** shift the allocator range up by setting `MCD_API_PORTS__RANGE_START`
(e.g. `25566`) in `.env`, or run the relay on a separate host.

### Reconciler grace after an API restart

When a worker or the `api` container is recreated (e.g. a UI-only redeploy), the
startup reset / worker orphan sweep marks the bounced servers `observed=unknown`;
the reconciler then waits for the divergence to outlast a **grace window** before
re-dispatching the start. The grace is per-action (issue #999):

- **Fast held-restart path** — a same-worker restart where the worker is back
  online **and** still holds a fresh-enough working set (its persistent scratch is
  at least as new as the last published snapshot) skips the destructive hydrate, so
  the re-dispatched start is command-only. This is the common single-host worker/API
  restart, and it recovers after the **short** `reconciler.held_start_grace_seconds`
  (default **90 s**): worker-reconnect (seconds) + ~90 s ≈ under 2 min.
- **Slow hydrate / cross-worker path** — a `place_and_start` (orphan, may land on a
  different worker, always hydrates) or a same-worker start whose worker does *not*
  hold a fresh working set (hydrate will run) waits the full
  `reconciler.grace_seconds` (default **660 s ≈ 11 min**). That long grace is
  dominated by the hydrate budget and keeps the reconciler from racing an in-flight
  first dispatch and spawning a duplicate live instance on another worker (#822), so
  it is **not** safe to shorten on these paths.

During the grace window the relay maps `observed=unknown` → `STOPPED` and players
get a "server stopped" MOTD even though the MC containers are healthy (issue #985);
the fast held path keeps that window short for routine single-host restarts without
the operator lowering `grace_seconds` below its safety floor.

Both knobs have boot-time safety floors (a warning, not fatal): `grace_seconds`
must exceed `max(hydrate_timeout + command_timeout, snapshot_timeout)` (#822/#847),
and `held_start_grace_seconds` must exceed `command_timeout_seconds` (it only covers
a command-only start). Lowering `grace_seconds` below its floor reopens the
duplicate-start / stale-snapshot races; prefer the (already short by default) held
path over shrinking the full grace.

The reconciler knobs (`INTERVAL_SECONDS`, `GRACE_SECONDS`, `BACKOFF_BASE_SECONDS`,
`BACKOFF_MAX_SECONDS`) are forwarded via `compose.yaml`; see `.env.example` for
their defaults and `api/src/mc_server_dashboard_api/config.py` for constraints
(`backoff_max_seconds` must be ≥ 600 to keep crash-loop damping effective).
`held_start_grace_seconds` defaults to 90 in the application and is set via
`MCD_API_RECONCILER__HELD_START_GRACE_SECONDS` (forwarded through
`compose.yaml` like the other reconciler knobs).

### Direct path vs relay path

| | Direct path (today) | Relay path |
|---|---|---|
| `relay.enabled` (API) | `false` (default) | `true` |
| Player address | `<worker host>:<game_port>` | `<slug>.<base_domain>` |
| `driver.container.game_bind_ip` | `0.0.0.0` (compose default) | `127.0.0.1` — no inbound game port needed |
| `MCD_WORKER_GAME_BIND_IP` in `.env` | unset (defaults to `0.0.0.0`) | `127.0.0.1` |
| Host firewall (worker) | game-port range open | nothing inbound on the Worker |

When the relay is enabled, set `MCD_WORKER_GAME_BIND_IP=127.0.0.1` in `.env`
so game ports bind only on loopback — the Worker dials its own loopback game
port into the tunnel, and no inbound game-port range is needed on the worker
host. The relay takes all inbound player traffic on port 25565. On a single
host the loopback bind still allocates from `25565..`, so the first server's
`127.0.0.1:25565` publish collides with the relay's `0.0.0.0:25565` bind; shift
the allocator range as described under
[Single-host port collision](#single-host-port-collision).

The two paths are not mutually exclusive at the protocol level (a server is
reachable both ways during migration); `relay.enabled` governs whether the
relay control surface is active.

### Firewall summary (relay host)

| Port | Protocol | Direction | Purpose |
|---|---|---|---|
| 25565 | TCP | inbound | player game connections |
| 25665 | TCP | inbound | Worker dial-back (TLS tunnel) |
| 50051 | TCP | internal (compose network only) | gRPC control plane (not published) |
