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

## 2. Prerequisites

- A Linux host with Docker Engine and the Compose plugin (`docker compose`).
- The host user in the `docker` group (or run compose with sufficient
  privileges). The worker container needs access to the Docker socket.
- Outbound network access from the host: the API fetches Minecraft/Paper version
  manifests and JARs, and the worker pulls the per-Java base images.

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

```sh
docker compose up -d --build
```

This builds the `api` and `worker` images, starts `db`, runs `migrate` to apply
the schema, then starts `api` and `worker`. Check status and logs:

```sh
docker compose ps
docker compose logs -f api worker
```

The API HTTP surface is then on `http://<host>:${API_HTTP_PORT}` (default 8000);
`GET /healthz` returns the liveness + database-reachability probe.

## 5. First-run bootstrap (create the platform admin)

There is no seeded admin. Register the first user over HTTP, then promote it to
platform admin directly in the database (the only out-of-band step).

1. Register the user:

   ```sh
   curl -X POST http://localhost:8000/users \
     -H 'Content-Type: application/json' \
     -d '{"username": "admin", "password": "<a-strong-password>"}'
   ```

2. Promote it to platform admin:

   ```sh
   docker compose exec db \
     psql -U mcsd -d mcsd \
     -c "UPDATE \"user\" SET is_platform_admin = true WHERE username = 'admin';"
   ```

   (`user` is a reserved word in SQL, hence the quotes. Use the `POSTGRES_USER` /
   `POSTGRES_DB` values from your `.env` if you changed them.)

### Accepting the Minecraft EULA on first run

Mojang's server refuses to start until you accept its EULA: a fresh server writes
`eula.txt` with `eula=false` and exits. The primary path is to accept the EULA at
creation — pass `accept_eula: true` on `POST /communities/{cid}/servers`, which
seeds `eula.txt` with `eula=true` into the server's initial working set so the
first start does not crash. Acceptance is recorded as part of the audited create.

If you create a server without `accept_eula`, the first start still crashes on the
default `eula=false`; recover by editing `eula.txt` to `eula=true` (the file API)
and starting again.

## 6. How Minecraft server ports reach clients

The worker's container driver reads each server's `server.properties` and
publishes its `server-port` (Minecraft default 25565) from the MC container to
the host. Players connect to the host on the server's game port. Because these MC
containers are created at runtime — not declared in `compose.yaml` — the host
firewall must allow inbound traffic to whichever game ports your servers use.

**Distinct ports are now automatic at create.** The API tracks each server's game
port (`server.game_port`, DATABASE.md Section 7) and, at create, assigns the
lowest free in-range port (configurable via `ports.range_start`/`ports.range_end`,
default `25565..25664`; CONFIGURATION.md Section 5.7), unique deployment-wide, and
seeds `server-port=<port>` into the new server's `server.properties`. So
operator-created servers no longer need manual `server-port` editing to avoid
host-port collisions. An operator may still pass an explicit `game_port` at create
(rejected 422 out of range, 409 taken); a delete frees the port for reuse.

For an **imported or legacy server** whose row predates port tracking
(`game_port` is `NULL`), nothing is auto-assigned — set its `server-port` manually
in `server.properties` (via the files API) and keep distinct values per server, as
before.

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
- **Unset** (bare-metal worker / host-process parity): RCON is published to the
  host loopback (`127.0.0.1`) and dialed there, the historical behavior.

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

```sh
git pull
docker compose up -d --build
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
