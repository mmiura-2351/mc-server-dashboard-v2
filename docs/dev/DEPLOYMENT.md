# Deployment

How to run mc-server-dashboard v2 on a single host with Docker Compose. This is
the minimum container-first deployment: one `db`, one `api`, one
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
port. The worker attaches every MC container to **`mcsd-servers`**, a second
pinned network kept separate from the control-plane network `mcsd`, and reaches
each server's RCON by container name over it, so **RCON never leaves the docker
network** (RCON is not published to the host; see Section 7). The `migrate`
service is a one-shot that applies the database schema before `api` starts.

**Two networks, split by trust.** `mcsd` carries the control and
data planes — `api`, `db`, `seaweedfs`, the two one-shots, `relay`, `cloudflared`
and the worker. `mcsd-servers` carries the Minecraft server containers, which run
operator- and community-supplied JARs, plugins and mods. The `worker` is the only
service on both, because it has to be: it dials `api:50051` and `api:8000` on one
side and resolves container names for its RCON and relay game dials on the other.

Over the docker networks, a Minecraft container reaches none of the API, database
or object-store ports (enumerated by probe from a booted server —
[`../app/SECURITY.md`](../app/SECURITY.md) Section 6). Two things that split does
**not** cover, both of which an operator controls: a port **published to the
host** on a non-loopback interface stays reachable from `mcsd-servers` through
the bridge gateway, so `API_HTTP_BIND_IP=0.0.0.0` re-opens `api:8000` to every
Minecraft container (Section 8); and a server that is **already running** when
the server network changes keeps the network it was started on until it is
restarted (Section 9). A Minecraft
container also keeps outbound internet — Mojang online-mode authentication and
plugin/mod downloads need it — and can still reach the *other* Minecraft
containers. `SECURITY.md` Section 6 states the full residual, including what
`mcsd` still carries unauthenticated.

### CPU priority for game-server containers

The worker creates every Minecraft container with an elevated CPU weight
(`CpuShares` 2048, double the Docker default of 1024), so a game server wins CPU
contention against batch workloads — CI builds, test suites, image builds —
sharing the same host. Without it, a server starves under heavy host load: the
MC server thread stalls for tens of seconds (the server logs "Running … ticks
behind") and players keepalive-drop. Shares are a **relative weight, not a
cap** (the Engine translates them to `cpu.weight` on cgroup v2): they only
arbitrate who wins when the CPU is saturated. They do **not** raise absolute
capacity, so running heavy builds on the game host still degrades a server's
throughput — the weight just keeps the game ahead of the batch work. Do not rely
on it as a substitute for keeping heavy build pipelines off the game host.

## 2. Prerequisites

- A Linux host with **Docker Engine 28.0+** and the **Compose plugin
  (`docker compose`) v2.34.0+**. **Check this before deploying — below the floor
  the stack comes up clean and the setting is silently dropped:**

  ```sh
  docker compose version --short && docker version --format '{{.Server.Version}}'
  ```

  The floor comes from one feature: `compose.yaml` sets `gw_priority` on the
  `worker` service's network attachments to pin which of its two networks
  provides the default route (Section 1). `GwPriority` arrived in
  Engine API `v1.48` (Engine 28.0), and Compose honours it from v2.34.0
  (docker/compose#12574). What an under-floor host does:

  | Compose | Engine | Result |
  |---|---|---|
  | < 2.33.0 | any | **Loud.** The compose-spec schema rejects the unknown key and `up -d` fails |
  | 2.33.0 - 2.33.1 | any | **Silent.** Validates, drops the key, leaves `GwPriority` at 0 |
  | 2.34.0+ | < 28.0 | **Silent.** The daemon ignores endpoint fields it does not know |
  | 2.34.0+ | 28.0+ | Pinned, as intended |

  Only the first band fails loudly, so **do not rely on a failed `up -d` to tell
  you**. In the two silent bands the worker's default route falls back to
  Docker's own endpoint ordering, which may happen to pick the right network —
  that is what makes it hard to notice. Confirm it took:

  ```sh
  docker inspect "$(docker compose ps -q worker)" \
    --format '{{range $n, $e := .NetworkSettings.Networks}}{{$n}} {{$e.GwPriority}}{{println}}{{end}}'
  # expect: mcsd 100   /   mcsd-servers 0
  ```
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
| `API_HTTP_PORT` | Published host port for the API HTTP surface | default `8000` |
| `API_HTTP_BIND_IP` | Host interface for the API port; `127.0.0.1` (default) binds loopback only, `0.0.0.0` binds all interfaces | default `127.0.0.1` |

Those two are the **host** side of the API's HTTP port — which host interface
and host port compose publishes it on. The **container** side is
`MCD_API_SERVER__HOST` / `MCD_API_SERVER__HTTP_PORT`, the settings the API
process actually binds (CONFIGURATION.md Section 5.1); `compose.yaml` forwards
both from `.env` and defaults them to `0.0.0.0` and `8000`, and derives the
publish target, the healthcheck and the internal `http://api:…` base URLs from
the port, so moving it moves them together. Leave both at their defaults unless
you have a reason: `MCD_API_SERVER__HOST=127.0.0.1` in particular makes the
published port unreachable from the host while the healthcheck still reports the
service healthy.

`POSTGRES_USER` and `POSTGRES_DB` default to `mcsd`; `MCD_API_CONTROL__WORKER_CREDENTIAL`
is reused by the worker as its `MCD_WORKER_API_CREDENTIAL` (wired in
`compose.yaml`), so both sides share the one secret. The two
`MCD_API_STORAGE__OBJECT__*` keys are the S3 credentials for the **default object
storage backend**; they are required only while `COMPOSE_PROFILES=object` (the
default) and unused after the fs opt-out — see
[Section 5](#5-storage-backend-object-on-seaweedfs-default).

`MCD_API_SERVER__PUBLIC_BASE_URL` is not in the table above — compose
defaults it to `http://api:8000`, which resolves only on the control-plane
network `mcsd` (not on `mcsd-servers`, where the Minecraft containers run, and
not off-host) — but it is mandatory to set in `.env` for any real deployment:
player-facing links (e.g. resource-pack download URLs) are rendered from this
value, so leaving the compose default renders those links unreachable to every
browser and game client. Set it to this deployment's externally reachable origin
(see [Cloudflare Tunnel](#cloudflare-tunnel-recommended) below for a worked
example).

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
dirty tree silently ships the wrong ref ([`AGENTS.md`](AGENTS.md) Section 1).
Run the preflight, which refuses (exit 1) when the checkout is not on `main` or
is dirty, then build:

```sh
./scripts/deploy_preflight.sh && docker compose up -d --build
```

**Automated path:** `make deploy` (`scripts/deploy.sh`) wraps the above with
interactive `.env` generation (first run), a `git pull --ff-only origin main` to
fetch the latest revision, image builds, a `.last-deploy-sha` stamp for
selective rebuilds, and a post-deploy `/api/healthz` check whose verdict lands
in `.last-deploy-health` (Section 9, "The two deploy records"). Prefer it over
the manual commands; use `make update` for subsequent upgrades (Section 9).

This builds the `api` and `worker` images, starts `db`, runs `migrate` to apply
the schema, then starts `api` and `worker`. Check status and logs:

```sh
docker compose ps
docker compose logs -f api worker
```

The API HTTP surface is then on `http://127.0.0.1:${API_HTTP_PORT}` (default
8000) — the port binds to loopback by default (`API_HTTP_BIND_IP`, Section 3);
the entire HTTP API is namespaced under `/api`, so `GET
/api/healthz` returns the liveness + database-reachability probe.

### Compose healthcheck

The `api` service carries a compose `healthcheck` that hits `GET /api/healthz`
(the same endpoint the deploy/update scripts curl-loop). It uses the image's
Python interpreter (`urllib.request`), so no extra binary is needed. The
`worker`, `relay`, and `cloudflared` services depend on `api` with
`condition: service_healthy`, so they do not start until the API is actually
serving (not merely "started"). `db` and `seaweedfs` already have their own
healthchecks; `api` waits for both (`service_healthy` / `service_completed_successfully`
for `migrate`).

### How the Web UI ships

The browser UI is served by the `api` container itself — there is **no separate
UI service and no reverse proxy** (WEBUI_SPEC 7.7). The `api` image
is multi-stage: a Node stage builds the React SPA (`webui/dist`, Node major pinned
by `webui/.nvmrc`, npm pinned by `webui/package.json` `engines`), and the runtime
stage copies that build in. `compose.yaml` points the API at it with
`MCD_API_WEBUI__DIST_DIR=/app/webui/dist`, so the SPA is served on the **same
origin** as the API at `http://127.0.0.1:${API_HTTP_PORT}/`.

The entire HTTP API is namespaced under `/api`, so `/api/*` is the
API (REST, WebSocket, the OpenAPI schema/docs, and the health/readiness probes)
and `/assets/*` is the built SPA chunks — both are excluded from the SPA
fallback. The Prometheus exposition is **not** on this port at all; it has its
own listener (Section 8). A `/api/*` miss is a wrong/removed route
and an unmatched `/assets/*` request returns 404 (a stale/renamed chunk, never
a client-side route); *every other unmatched path* falls back to
the SPA's `index.html` so
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
publish then creates the bucket. A **non-SeaweedFS** S3 backend that
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

### Bucket lifecycle rule (AbortIncompleteMultipartUpload)

Behind the API sweep, a bucket-level `AbortIncompleteMultipartUpload` lifecycle
rule on the `mcsd` bucket is the storage-layer backstop for the residual gap
above — the partless orphan the sweep cannot age-gate. It is
applied by a one-shot compose service, `seaweedfs-lifecycle`, that mirrors
`migrate`: gated behind the `object` profile, it waits for the SeaweedFS S3
gateway to be healthy and then runs `scripts/provision_object_lifecycle.py` (sync
boto3 from the reused `mcsd-api:dev` image — no extra dependency). The api service
blocks on it (`service_completed_successfully`) so the rule is in place before the
app takes traffic; in `fs` mode the service is out of the active profile set and
the dependency is ignored.

The script creates the bucket if absent, applies the rule with
`DaysAfterInitiation: 2` — safely above the sweep's 1h/24h age thresholds so the
lifecycle rule never races the app-level abort — then **self-verifies** with
`GetBucketLifecycleConfiguration` and exits non-zero if SeaweedFS did not honor
the rule, so the one-shot fails loudly rather than silently no-op'ing. Self-verify
proves the config was accepted and persisted; because `DaysAfterInitiation` is
integer-days, actual abort execution is not observable at deploy time.

### Opting back to the fs backend

To run the local-volume **`fs`** backend instead (STORAGE.md Section 2), set
both of these in `.env` — clear `COMPOSE_PROFILES` and pin the
backend:

```sh
# in .env
COMPOSE_PROFILES=
MCD_API_STORAGE__BACKEND=fs
```

Clearing `COMPOSE_PROFILES` drops the `seaweedfs` service entirely (it is gated
behind the `object` profile), so the stack neither runs nor waits on a
SeaweedFS instance it does not use; the api's dependency on it is `required: false`,
so the api starts cleanly. With the service gone, the S3 credential keys
(`MCD_API_STORAGE__OBJECT__*`) are **not required** and may stay blank.

The fs root (`MCD_API_STORAGE__FS__ROOT=/data/storage`) and its `api-storage`
volume stay wired in `compose.yaml`, so no other change is needed. Recreate the
stack to apply:

```sh
docker compose up -d --remove-orphans
```

`--remove-orphans` clears the deselected `seaweedfs` container if it was
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
real endpoint. `api/tests/servers/test_resource_pack_store_contract.py` runs its
`live-s3` parametrization against the same endpoint (the resource pack store's
`size()` == `open()` byte-count invariant). Both are skipped unless
`MCD_TEST_S3_ENDPOINT` is set, so `make check` and the main `check` CI job stay
green without an S3 instance. CI runs them in the api workflow's separate
`live-s3` job, which starts a SeaweedFS container and supplies the endpoint;
that job fails if either module skips. To run them locally against a
throwaway SeaweedFS, run this from the repository root — the first line reads
the image out of `compose.yaml`'s `seaweedfs` pin instead of restating it, so a
local run exercises the deployed version and there is no second copy of the tag
to keep in sync:

```sh
SWFS_IMAGE=$(sed -n 's/^ *image: \(chrislusf\/seaweedfs:.*\)$/\1/p' compose.yaml)

docker run -d --name swfs-test -p 8333:8333 \
  -e AK=testak -e SK=testsk --entrypoint sh "$SWFS_IMAGE" -c \
  'mkdir -p /etc/seaweedfs && printf "{\"identities\":[{\"name\":\"t\",\"credentials\":[{\"accessKey\":\"%s\",\"secretKey\":\"%s\"}],\"actions\":[\"Admin\",\"Read\",\"Write\",\"List\",\"Tagging\"]}]}" "$AK" "$SK" > /etc/seaweedfs/s3.json && exec weed server -dir=/data -s3 -s3.config=/etc/seaweedfs/s3.json -volume.max=24'

cd api && MCD_TEST_S3_ENDPOINT=http://localhost:8333 \
  MCD_TEST_S3_ACCESS_KEY=testak MCD_TEST_S3_SECRET_KEY=testsk \
  uv run pytest tests/storage/test_object_live_seaweedfs.py \
    tests/servers/test_resource_pack_store_contract.py

docker rm -f swfs-test
```

One `swfs-test` serves as many pytest runs as you like — the suite drops the
bucket it creates, and `-volume.max=24` gives the store room for the buckets it
creates while it runs. Both halves are needed, and neither is optional.
SeaweedFS backs every S3 bucket with a *collection* and grows a batch of
up to seven volumes into it out of the `-volume.max` budget, which it does not
reclaim while the collection lives; three collections are in play here (the
`mcsd` bucket, the bucketless-store test's own bucket, and the filer's internal
metadata log), so the stock budget of 8 cannot hold them: after a few runs the
filer's metadata log claims the last free volume, and every run from then on
fails with `No writable volumes and no free volumes left`. At 24 repeated runs
pass with the volume count flat. The deployment itself is unaffected and keeps
the stock budget — it only ever has the one bucket.

### Vacuum tuning and manual recovery

SeaweedFS reclaims the space left behind by overwrites and deletes (every
snapshot's CopyObject churn and pointer overwrites generate this garbage) by
**vacuuming** — compacting a volume into a fresh copy that drops the dead data.
A built-in vacuum runs periodically and fires only once a volume's garbage ratio
crosses `-master.garbageThreshold`. `compose.yaml` sets this to **`0.1`** (10%),
below SeaweedFS's `0.3` (30%) default, so compaction kicks in earlier and
runs more often against smaller amounts of garbage.

The lower threshold exists because compaction is not free space, it *needs*
free space: it writes the compacted volume alongside the original before
swapping, so once headroom is gone the auto-vacuum cannot complete. The failure
the value guards against looks like this: garbage at ~63% of a volume — well
past the `0.3` default, so the threshold tripped long ago — on a host that has
climbed to 97% disk, with no room left to write the compacted copy. Triggering
compaction earlier (at 10%) keeps the garbage — and the peak disk needed to
compact it — small enough that headroom is always available.

If a deployment has run out of headroom and the auto-vacuum cannot keep up,
reclaim space **on demand** from the SeaweedFS shell:

```sh
docker compose exec seaweedfs weed shell
> volume.vacuum -garbageThreshold=0.1
```

This forces a vacuum pass immediately instead of waiting for the next periodic
run. It is an **incident-recovery lever, not a second scheduled mechanism** — the
`-master.garbageThreshold=0.1` flag already handles the steady state; run
`volume.vacuum` only to recover a store that has fallen behind.

### Storage-capacity incident runbook (disk-full diagnosis and recovery)

When the SeaweedFS store fills the host disk, publishes and backups start
failing and the deployment can spiral. This is the diagnose-then-recover
procedure for that class of incident; the steady-state mechanisms that keep it
from happening are cross-linked at the end. Host disk-usage **alerting** is not
provided — see the note below.

#### Diagnosis

**Confirm the signature.** A store with no room left surfaces as S3 write
failures in the api container log; the signature is `InternalError`
raised on the `UploadPart` operation (a multipart part that could not land):

```sh
docker compose logs api | grep InternalError
```

**Measure the garbage.** From the SeaweedFS shell, `volume.list` reports
per-volume byte counts. Read each volume's `DeletedByteCount`
(deleted-but-not-yet-reclaimed bytes — the vacuum backlog) against its total
size: a large `DeletedByteCount` relative to the total means most of the space is
reclaimable garbage rather than live data.

```sh
docker compose exec seaweedfs weed shell
> volume.list
```

**Measure the live footprint.** `fs.du` reports the actual live size under the
bucket, which separates a genuine capacity problem (live data is large) from a
reclaimable-garbage problem (live data is small but the volumes are bloated):

```sh
docker compose exec seaweedfs weed shell
> fs.du /buckets/mcsd
```

#### Recovery

Work from the cheapest, most-reversible step to the most invasive, and re-check
`df -h` after each one.

1. **Free host build-cache first.** Docker's build cache can hold gigabytes
   unrelated to SeaweedFS; reclaiming it buys the headroom that vacuum compaction
   *needs* — compaction writes a fresh copy of a volume before swapping it in, so
   it cannot complete on an already-full disk:

   ```sh
   docker builder prune
   ```

2. **Force an immediate vacuum.** With headroom restored, compact the volumes on
   demand instead of waiting for the built-in periodic auto-vacuum:

   ```sh
   docker compose exec seaweedfs weed shell
   > volume.vacuum -garbageThreshold=0.1
   ```

3. **Restart the `seaweedfs` service** to clear any wedged state and let it
   re-open the compacted volumes:

   ```sh
   docker compose restart seaweedfs
   ```

4. **Clean incomplete uploads.** Remove incomplete multipart uploads that the
   app-level abort did not catch — the SeaweedFS-native cleanup, complementary to
   the API sweep (`Orphan multipart parts` above):

   ```sh
   docker compose exec seaweedfs weed shell
   > s3.clean.uploads
   ```

#### Steady-state prevention

Three steady-state mechanisms keep the store from filling in normal operation; this
runbook is the fallback for when they have already fallen behind. Do not re-tune
them here — each has its own subsection:

- **Incomplete-upload lifecycle rule.** A bucket-level
  `AbortIncompleteMultipartUpload` rule with `DaysAfterInitiation: 2` aborts
  orphaned multipart uploads at the storage layer. See
  [Bucket lifecycle rule (AbortIncompleteMultipartUpload)](#bucket-lifecycle-rule-abortincompletemultipartupload)
  above.
- **Auto-vacuum threshold.** `compose.yaml` sets `-master.garbageThreshold=0.1`,
  so SeaweedFS's built-in periodic vacuum compacts a volume once its garbage
  ratio crosses 10% — early enough that the headroom to compact is still
  available. See [Vacuum tuning and manual recovery](#vacuum-tuning-and-manual-recovery)
  above.
- **App-side periodic sweep.** The API reclaims orphan staging/snapshot prefixes
  and aborts orphan in-progress multipart uploads on a loop (daily by default,
  `storage_sweep.interval_seconds`). See CONFIGURATION.md
  [Section 5.15](../app/CONFIGURATION.md#515-crash-recovery-storage-sweep).

#### Not provided: disk-usage alerting

There is **no automated early warning** before the disk fills — host disk-usage
alerting is not provided. Check capacity by hand on a regular cadence: `df -h`
on the host for overall free space, and `volume.list` from the SeaweedFS shell
(as in Diagnosis above) for the per-volume garbage backlog.

## 6. First-run bootstrap (create the platform admin)

There is no seeded admin and **no manual database step**. The first user
registered over HTTP on a fresh database automatically becomes the platform
admin; just register it:

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
on an empty database is allowed even so — it is the only way to create the
bootstrap admin. The open flag is
enforced normally for every registration after the first user exists.

From here on, that admin manages all further accounts through the authenticated,
audited admin API (granting/revoking the admin flag, deactivating/reactivating,
deleting, and listing users; `PUT /api/users/{id}/platform-admin`,
`POST /api/users/{id}/deactivate` and friends) — for example,
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

An **absent** `server.properties` takes the 25565 default. A file that
exists but cannot be read to the end — a line longer than 64 KiB, a permission
problem — fails the start with an error naming the file instead: 25565 is the
relay's port, so a silent fallback turns a correctly tracked server into a
host-port collision that never starts.

**Distinct ports are assigned at create.** The API tracks each server's game
port (`server.game_port`, DATABASE.md Section 7) and, at create, assigns the
lowest free in-range port (configurable via `ports.range_start`/`ports.range_end`,
default `25565..25664`; CONFIGURATION.md Section 5.8), unique deployment-wide, and
seeds `server-port=<port>` into the new server's `server.properties`. So
operator-created servers need no manual `server-port` editing to avoid
host-port collisions. An operator may pass an explicit `game_port` at create
(rejected 422 out of range, 409 taken); a delete frees the port for reuse.

**Changing a server's port after create.** A stopped server can be re-ported via
`PATCH /api/communities/{id}/servers/{id}` with a `game_port` field. It
validates the new port like create (422 out of range, 409 taken), rewrites
`server-port` in the at-rest `server.properties`, and updates `server.game_port`
together, so under normal operation the DB and the real bind port stay in sync.
The server must be at rest (a running server is 409 `server_not_stopped`). This is
the preferred way to re-port — it keeps the tracked port and the file aligned,
unlike editing `server.properties` by hand.

**Import and restore re-apply the tracked port too.** A whole-server import and a
backup restore both republish a `server.properties` that came from somewhere else
— an export archive, or the working set as it was when the backup was taken — so
each re-applies the platform-managed keys (`server-port`, the RCON triple, the
resource-pack keys) from the DB afterwards. An archive carrying
`server-port=25565` therefore publishes the importing/restoring server's own
tracked port, not the archive's. A restore whose backup carries no
`server.properties` at all gets one holding just those keys, the same file a
create seeds; if that rewrite fails, the restore answers 503 `seed_failed` (the
world data is restored, the seed is not — retry the restore).

**Residual drift modes.** Three remain, all recoverable:

- A `server.properties` edited outside the API — on the worker host, or inside the
  running container — is invisible to the platform until the next write path runs.
  The DB keeps the tracked port and the file keeps the edit; the next port `PATCH`,
  import, or restore overwrites it.

- The port `PATCH`'s file write and DB commit are not atomic. The file is
  rewritten *after* the commit, so the drift is one-way: a storage failure in
  that rewrite (response 503 `seed_failed`) leaves the row on the new port while
  `server.properties` keeps the old one. A commit that fails never reaches the
  rewrite, so the file cannot run ahead of the row.
- A row with `game_port = NULL` tracks no port, so nothing is re-applied to its
  file (a restore leaves `server-port` as it found it); backfill it as
  described below.

A drift you find in the field is fixed by a `PATCH` that actually **changes**
the port, which rewrites the row and the file together. When the row already
holds the port you want and only the file is stale, that `PATCH` is a no-op —
re-port to a spare in-range port and back, or run an import or restore, each of
which re-applies the tracked port to the file.

Every API path — create and import alike — assigns a port, so a row with
`game_port = NULL` arises only outside the API (a direct SQL write). For such a
**row without a tracked port**, nothing is auto-assigned. Prefer the update-port
API above to set its port (it backfills `game_port` and rewrites
`server.properties` together); the manual SQL backfill below is the fallback
when you have already set `server-port` directly in the file.

### Backfilling rows without a tracked `game_port`

A row with `game_port = NULL` is **invisible to port auto-assignment**: it is
excluded from the deployment-wide taken-port set, so the next auto-assigned
server can be handed the very host port that untracked server already binds (via
its `server.properties`) — a guaranteed host-port collision when both run. To make
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

- **Set** (this `compose.yaml`, rendering `mcsd-servers` — the value is
  `${COMPOSE_PROJECT_NAME:-mcsd}-servers`, the same expression as
  `networks.servers.name`, so a stack brought up under another project name
  keeps the two in step): the worker attaches each
  MC container to that user-defined network and dials RCON at the container's
  name over the network. There is **no host RCON publication** — RCON never
  leaves the docker network. This is required for the containerized worker, whose
  own loopback is not the host loopback where a published RCON port would land.
  `mcsd-servers` is pinned as a second network, separate from the
  control-plane `mcsd`, and the `worker` service is attached to both so it shares
  container-name DNS with the MC containers it creates while also reaching
  `api:50051` / `api:8000`. The network **must be user-defined**
  (a `docker network create` network, as the pinned `mcsd-servers` is): the
  default `bridge` has no container-name DNS, so pointing this at `bridge` lets
  the attach succeed but the RCON dial silently fails.
- **Unset** (bare-metal worker): RCON is published to the host loopback
  (`127.0.0.1`) and dialed there.

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

The `cloudflared` service in `compose.yaml` is the supported
HTTPS path. It is gated behind the `tunnel` compose profile (the relay uses a
separate `relay` profile).

How it works: the browser reaches the Cloudflare edge over HTTPS (the public
hostname is configured in the Cloudflare Zero Trust dashboard); `cloudflared`
runs on the `mcsd` network and forwards traffic to `api:8000` over plain
HTTP on that network. No inbound port, no TLS certificate, and
no reverse proxy are needed on the host. The default loopback bind
(`API_HTTP_BIND_IP=127.0.0.1`) is correct for this topology — `cloudflared`
reaches the API over the `mcsd` network, not the host port, so the
API does not need to be published on all interfaces.

To enable:

1. Add `tunnel` to `COMPOSE_PROFILES` in `.env`:

   ```sh
   # example: object backend + Cloudflare Tunnel
   COMPOSE_PROFILES=object,tunnel
   ```

2. Create a tunnel in the Cloudflare Zero Trust dashboard, add a public
   hostname pointing to `http://api:8000`, and copy the tunnel token.

   That target lives in the Cloudflare dashboard, not in this repository, so it
   is the one thing `MCD_API_SERVER__HTTP_PORT` (Section 3) does not move: if you
   change the container-side port, update the public hostname's target to match
   or the tunnel forwards to a closed port.

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

**Edge body-size caps and the data-plane URL:** if
`MCD_API_SERVER__PUBLIC_BASE_URL` is overridden to the tunnel's public
hostname (e.g. so a public-facing link is externally reachable), do not let
that also become the URL the Worker uses for hydrate/snapshot transfers.
Cloudflare Tunnel caps request bodies at ~100 MB and rejects larger ones; a
co-located Worker's working-set upload routinely exceeds that (a booted Paper
server alone is ~200+ MB), so every snapshot would 413 and world progression
would be silently lost on every stop. `compose.yaml` pins
`MCD_API_SERVER__DATA_PLANE_BASE_URL` to the internal compose-network address
(`http://api:8000`) independently of `PUBLIC_BASE_URL` for exactly this
reason; any other topology with a co-located Worker behind a body-size-capped
edge must set `server.data_plane_base_url` to an internal address the Worker
can reach directly (CONFIGURATION.md Section 5.1).

**The tunnel publishes every path on `api:8000`.** `cloudflared`
forwards the whole hostname to `api:8000` and path-scopes nothing, so the
loopback publish in `compose.yaml` constrains only *host* reachability — the
tunnel reaches the API over the `mcsd` network, past that bind. Treat
anything you mount on the API's HTTP port as internet-facing. This is why the
Prometheus exposition is not on that port (see the metrics subsection at the end
of this section); the OpenAPI schema, the docs routes and `/api/readyz`
are reachable from the internet. To restrict a
path, use a Cloudflare Access policy on the public hostname; the repo ships no
such rule.

#### Reverse proxy + Let's Encrypt (alternative)

For deployments that do not use Cloudflare, any TLS-terminating reverse proxy
(Caddy, nginx, Traefik, etc.) in front of the API's HTTP port achieves the same
result. The proxy terminates TLS with a certificate from Let's Encrypt (or
another CA) and forwards to `http://localhost:${API_HTTP_PORT}`. The default
loopback bind (`API_HTTP_BIND_IP=127.0.0.1`) works when the proxy runs on the
same host; set `API_HTTP_BIND_IP=0.0.0.0` in `.env` if the proxy is on a
different host — which also exposes `api:8000` to every Minecraft container on
this host, so firewall the port to the proxy's address
([`../app/SECURITY.md`](../app/SECURITY.md) Section 6). This is a standard
reverse-proxy setup and is not detailed here.

#### HTTP-only fallback (LAN / development)

For plain-HTTP deployments (local network, development) where HTTPS is not
available, set:

```sh
MCD_API_AUTH__TOKEN__REFRESH_COOKIE_SECURE=false
API_HTTP_BIND_IP=0.0.0.0
```

`API_HTTP_BIND_IP=0.0.0.0` is required here because there is no tunnel or
same-host reverse proxy — clients on the LAN must reach the API directly. It
also puts `api:8000` back within reach of every Minecraft container on this
host, via the docker bridge gateway ([`../app/SECURITY.md`](../app/SECURITY.md)
Section 6) — so this topology suits a trusted LAN or a development box, not a
deployment whose community can upload plugins.

This drops the `Secure` attribute from the refresh cookie so the browser stores
it over HTTP and silent refresh works. **Security caveat:** the cookie is then
sent over plaintext, exposing the refresh token to network observers. Use this
only on trusted networks.

### Prometheus metrics: a separate, unpublished listener

The exposition is served on its own HTTP listener and is **off by default**
(`metrics.enabled`, CONFIGURATION.md Section 5.10). Enabling it binds a second
port that serves `GET /metrics`; the API's HTTP port serves no exposition at
all. The content is aggregates only, but it is operational signal — server and
worker counts, per-route request volume including login success/failure rates,
process start timestamps, control-plane liveness (SECURITY.md Section 5).

**Compose (whichever of the three options above you run).** Enable it in `.env`:

```sh
MCD_API_METRICS__ENABLED=true
```

The port is deliberately **not** in the `api` service's `ports:` list, so the
bind happens inside the container's own network namespace and the endpoint
exists only on the `mcsd` network — not on `mcsd-servers`, where the Minecraft
containers run. Scrape it from a service on `mcsd` (`http://api:9090/metrics`) —
a Prometheus container you add to `compose.yaml`, for example; the repo ships no
scraper.

Leave `MCD_API_METRICS__HOST` at its `0.0.0.0` default here. Narrowing it to
loopback makes the endpoint unscrapeable by any sibling container and protects
nothing: inside the container namespace, the missing `ports:` entry is what
confines it. `API_HTTP_BIND_IP` does not apply either — it only interpolates
into that `ports:` list, and there is no metrics entry for it to interpolate
into, so raising it to `0.0.0.0` exposes no second port. The two things not to
do are add a `ports:` entry for it, and map a second Cloudflare public hostname
to it.

**Non-compose runs (bare metal, systemd).** Here `0.0.0.0` genuinely is a
**second network-reachable port**, and a reverse proxy in front of the API's
HTTP port does not cover it: different port, the proxy never sees it. Either
scope the bind or firewall the port:

```sh
# scraper on the same host
MCD_API_METRICS__HOST=127.0.0.1
# or a private interface the scraper can reach (IPv6 literals work too)
MCD_API_METRICS__HOST=10.0.0.5
MCD_API_METRICS__PORT=9090
```

**Port 9090 is also the relay's metrics default** (RELAY.md Section 13). Under
compose they are separate containers (`api:9090`, `relay:9090`) and do not
collide. On a single host running both **natively**, they do: whichever binds
second logs `metrics listener bind failed; continuing without the metrics
endpoint` and serves nothing, while the process itself keeps running. Move one
of them (`MCD_API_METRICS__PORT` / `MCD_RELAY_METRICS_LISTEN`) in that topology.

### gRPC control-plane TLS (cross-host worker)

The in-compose deployment runs the control plane in plaintext on the private
`mcsd` network: `api` sets `MCD_API_CONTROL__TLS__INSECURE=true` and `worker`
sets `MCD_WORKER_API_TLS_INSECURE=true`. This rests on two conditions, both of
which have to keep holding: the gRPC control listener is not published to the
host, and the traffic stays on `mcsd`, whose only members are first-party
services. The Minecraft containers — the one place third-party code runs — are
on `mcsd-servers` and cannot reach `mcsd` at all
([`../app/SECURITY.md`](../app/SECURITY.md) Section 6). Attaching anything that
runs untrusted code to `mcsd`, or moving the MC containers onto it,
invalidates the plaintext posture: the worker credential rides this channel in
the clear.

A **multi-host** worker (a worker on a different machine dialing this API over a
real network) must not use the insecure posture, and the gRPC control plane must
not be exposed off-host while it is plaintext. Reaching a cross-host worker
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

TLS is not the only thing a cross-host worker needs: it also has to be able to
reach the HTTP **data plane**, which is a separate port and a separate setting —
see the next subsection.

### The data-plane URL for a cross-host worker

A cross-host worker **must** be given an `MCD_API_SERVER__DATA_PLANE_BASE_URL`
that the worker itself can reach. On a deployment running the shipped
`compose.yaml` that means **overriding** the variable in `.env`, not merely
setting one: compose already sets it, to the compose-internal `http://api:8000`.
Both ways of getting this wrong — leaving the variable unset, and keeping
compose's default — break this topology silently.

The control plane above is only how the API tells a worker *to* transfer. The
transfer itself is plain HTTP on the API's HTTP port (`/api/data-plane/...`): the
worker pulls the working set and the resolved JAR on hydrate (`GET`) and pushes
it back on snapshot (`POST`). The API advertises where to do that in each
trigger, and the address it advertises is `server.data_plane_base_url` — falling
back to `server.public_base_url` when that is unset (CONFIGURATION.md Section
5.1). Neither of the two values that work on a single host works here:

- `compose.yaml` pins the variable to `http://api:8000`, which resolves only on
  the `mcsd` network. A worker on another machine cannot resolve it at all.
- The fallback hands the worker `PUBLIC_BASE_URL`. If that is a Cloudflare
  Tunnel hostname, every snapshot larger than the tunnel's ~100 MB body cap is
  rejected (a booted Paper server alone is ~200+ MB), so world
  progression is lost on every stop. Registration still succeeds and the control
  plane still looks healthy; the failure appears at the first snapshot of a
  non-trivial server, on the worker's host, with nothing pointing back at the
  unset variable.

Set it to an address the worker can reach that is **not** behind a body-size-capped
edge — the API host's LAN/VPN address, not the tunnel hostname:

```sh
# in .env on the API host
MCD_API_SERVER__DATA_PLANE_BASE_URL=http://10.0.0.5:${API_HTTP_PORT}
# the HTTP port is published to loopback by default; a remote worker needs it on
# an interface it can reach (Section 3). This also makes api:8000 reachable from
# every Minecraft container on the API host -- firewall it to the worker's
# address (docs/app/SECURITY.md Section 6)
API_HTTP_BIND_IP=10.0.0.5
```

The API warns at startup when `server.data_plane_base_url` is unset while
`server.public_base_url` is set, naming the URL workers will be handed. If your
public URL genuinely is directly reachable by workers, set
`data_plane_base_url` to that same value to record the intent and silence the
warning. Deployments using the shipped `compose.yaml` never see it — compose
always sets the variable.

Which leaves the case this subsection opens with — compose's default kept, a
worker added on a second host — with **no boot-time signal at all**: the
variable is set, so the warning has nothing to fire on, and at startup the API
cannot know that a worker on another host is going to register. The first
signal is the first failed hydrate or snapshot, and it names
the URL the worker was handed, so the misconfiguration is read off that failure
rather than inferred. Both sides log it — the worker's `command failed` WARN on
the worker host (with the true `kind`, `HydrateTrigger` / `SnapshotTrigger`),
and the API's `command ... failed for server <id>` on the API host (where a
hydrate inside a start is labelled `StartServer`):

```text
instancemanager: hydrate: datatransfer: hydrate request: Get
"http://api:8000/api/data-plane/communities/.../working-set":
dial tcp: lookup api on 127.0.0.11:53: no such host
```

That message is a single line, wrapped above to fit. Reading `http://api:8000`
back out of a transfer failure means exactly this misconfiguration: the worker
was handed compose's internal default. Set `MCD_API_SERVER__DATA_PLANE_BASE_URL`
in `.env` on the **API** host — nothing changes on the worker.

**Protect this port like the control plane — the bearer token on it *is* the
control credential.** Data-plane requests carry the shared worker credential as
an `Authorization: Bearer` header, and the working set is the server's world
data; over a real network both are in the clear on plain HTTP. That bearer token
is `MCD_API_CONTROL__WORKER_CREDENTIAL` — the same secret the gRPC control plane
authenticates with — so leaking it off this port is not scoped to world data: it
compromises the control channel the TLS subsection above protects. The
control-plane TLS does not cover this port — different port, different protocol.
Put the data plane on a private network (VPN/WireGuard, or a private interface)
or terminate TLS in front of it and point
`MCD_API_SERVER__DATA_PLANE_BASE_URL` at the `https://` address, keeping in mind
that whatever terminates it must not impose a body-size cap. Note also that the
`API_HTTP_BIND_IP` set above is not route-scoped: it publishes the **entire**
HTTP surface on that interface, not only the data-plane routes (one bind serves
the whole REST API, `compose.yaml`), so the non-loopback-bind caveats elsewhere
in this section apply here in full.

### The relay tunnel port for a cross-host worker

A cross-host worker on a **relay-enabled** deployment needs one more thing: the
relay's Worker dial-back port, `25665`, published on an address it can reach.
`compose.yaml` publishes it on `127.0.0.1` by default —
`${RELAY_CONTROL_BIND_IP:-127.0.0.1}:25665:25665` — because the in-compose
worker does not use the host port at all. It dials whatever
`MCD_RELAY_TUNNEL_PUBLIC_ENDPOINT` advertises, and on a single host that is
`relay:25665`, the relay's address on the `mcsd` network. The loopback default
is what keeps the dial-back port out of reach of the Minecraft containers, which
otherwise reach every interface-published port through the bridge gateway
([`../app/SECURITY.md`](../app/SECURITY.md) Section 6).

For a worker on another host, set **both** — the bind and the advertised
endpoint — to the same reachable address on the relay host:

```sh
# in .env on the relay/API host
RELAY_CONTROL_BIND_IP=10.0.0.5
MCD_RELAY_TUNNEL_PUBLIC_ENDPOINT=10.0.0.5:25665
```

Setting only the endpoint leaves the port on loopback and the remote worker's
dial is refused; setting only the bind leaves the worker still dialling
`relay:25665`, which does not resolve off the `mcsd` network. The tunnel
certificate's SAN must match the hostname part of the endpoint you advertise —
regenerate it when you change that value (see the relay TLS material
subsection in [Section 12](#12-relay-game-ingress)); for a raw-IP
endpoint the SAN has to be an IP SAN.

This is a real interface, not a private one: `25665` carries the TLS tunnel the
worker dials, so unlike the gRPC control plane above it is safe to expose on a
network the worker can reach. It is still an inbound listener on the relay host
— firewall it to the worker's address rather than to the whole network.

## 9. Upgrade

Pull the new revision and rebuild; `migrate` re-runs `alembic upgrade head`
before the new `api` starts, so the schema is brought current automatically:

### Deploy-order rule: API before (or with) worker when new CommandErrorCodes are added

`compose.yaml` brings `api` up before `worker`, so the default `docker compose
up -d --build` already applies the correct order. However, if you update
containers individually, always update `api` first (or together with `worker`).
An API at the older revision receiving a `CommandErrorCode` it does not
recognise falls back to `INTERNAL` and its compensation logic may orphan a live
instance — for example, an API with no `BUSY` handling treats `BUSY` as
`INTERNAL` and unassigns the server, stranding the running instance. Updating
`api` first (or atomically via `docker compose up`) ensures the API's handler
for any new code is in place before the worker starts emitting it.

```sh
./scripts/deploy_preflight.sh && git pull --ff-only origin main && docker compose up -d --build
```

**Automated path:** `make update` (`scripts/update.sh`) runs the preflight, pulls,
detects which components changed since the last deploy (via `.last-deploy-sha`),
rebuilds only what changed (api before worker — worker restart bounces running MC
servers), starts the stack, stamps the started revision, and runs a post-deploy
`/api/healthz` check. Use `FORCE=1` to rebuild all components unconditionally.

### The two deploy records

`make deploy` and `make update` write two gitignored files in the repo root.
They record **different facts**, and conflating them makes change detection
diff from a revision that has stopped running:

| File | Means | Written |
|---|---|---|
| `.last-deploy-sha` | the revision the running stack was **built and started from** | as soon as `docker compose up -d` succeeds — *before* the healthcheck |
| `.last-deploy-health` | whether that revision then passed `/api/healthz` — `ok` or `failed` | after the healthcheck resolves |

The stamp is written before the healthcheck deliberately: once compose has
recreated the containers they are running the new revision whether or not the
API answers in time, and that is the base the next run has to compute "what
changed" from. A stamp written only on the verified path would lag the
containers, and the next `make update` would diff from the wrong base and
could skip rebuilding a component that had genuinely changed.

What the records drive on the next `make update`:

- **Stamp at `HEAD`, health `ok`** — nothing to do; exits without touching the
  stack.
- **Stamp at `HEAD`, health `failed` or absent** — the stack was started from
  `HEAD` but never confirmed healthy. Nothing is rebuilt (the tree has not
  moved, so no MC servers bounce); the stack is re-started and re-checked. Fix
  whatever the API was failing on, then just re-run `make update`.
- **Stamp behind `HEAD`** — normal selective rebuild of everything that changed
  between the two.
- **Stamp absent, empty, or naming a commit this checkout does not have** —
  the running revision is **unknown**. There is no base to diff from, so the
  run prints a warning to stderr and rebuilds **all** components. A deployment
  ever brought up with the plain `docker compose up -d --build` of Section 4
  has no stamp and lands here; so does one whose `docker compose up -d` failed
  part-way, because a stack that is half-recreated is not honestly described by
  either revision and both records are cleared rather than left claiming one.

A failed build (before `docker compose up`) changes neither record: no
container was replaced, so the existing stamp still describes what is running.

> **Caveat — the default storage backend is `object` (SeaweedFS); an fs
> deployment must pin it.** The `api` service resolves
> `MCD_API_STORAGE__BACKEND` to `object` **unless your `.env` pins it to
> `fs`**. An fs deployment whose `.env` does not carry that pin and simply
> `git pull`s and rebuilds starts the API against an **empty** SeaweedFS store —
> its servers, snapshots, and backups live in the `api-storage` (fs) volume and
> do **not** move across automatically (Section 5 "data cutover" caveat). To
> keep the fs data, pin the backend and drop the SeaweedFS service **before**
> rebuilding (Section 5 "Opting back to the fs backend"):
>
> ```sh
> printf 'COMPOSE_PROFILES=\nMCD_API_STORAGE__BACKEND=fs\n' >> .env
> ```
>
> To deliberately adopt the object backend on an existing fs deployment, treat
> it as a cutover: back up both volumes first, then expect an empty store until
> servers are re-hydrated/re-created (Section 5).

> **Caveat — the `seaweedfs` service is profile-gated.** The service is
> behind the `object` compose profile, which `.env.example` activates with
> `COMPOSE_PROFILES=object`. An **object** deployment whose `.env` has no
> `COMPOSE_PROFILES` line (one not derived from `.env.example`) does **not**
> start `seaweedfs` on `docker compose up` (an unset `COMPOSE_PROFILES` selects
> no profiles); the api boots and serves but all storage operations (snapshot,
> backup, file reads/writes) error at runtime because the S3 client cannot reach
> the object store. Add the line once before rebuilding:
>
> ```sh
> echo 'COMPOSE_PROFILES=object' >> .env
> ```
>
> (fs deployments set `COMPOSE_PROFILES=` empty instead — see the caveat box
> above.)

> **Caveat — Bedrock is opt-in on the relay.** The relay's
> Bedrock QUIC/UDP tunnel listener is gated on `bedrock.enabled` (`relay.toml`) /
> `MCD_RELAY_BEDROCK_ENABLED` (env), default **false** — `compose.yaml` feeds it
> from the same `MCD_API_RELAY__BEDROCK_ENABLED` flag the API reads (see
> `.env.example`), so operators toggle Bedrock in one place for both services. A
> Java-only / Bedrock-off relay binds neither the Bedrock tunnel port
> (`25675/udp`) nor the per-server `19132-19231/udp` window, so it cannot fail
> to start on a host-port conflict there. `compose.yaml` does, however,
> *publish* both Bedrock UDP port entries unconditionally (Compose has no clean
> per-port conditional syntax on a single service) — if a host already has
> something else bound to `25675/udp` or a port in `19132-19231/udp`, the relay
> container can fail Docker's own port allocation even with Bedrock disabled;
> free the conflicting port or move the relay to a host without one. See
> [`../app/RELAY.md`](../app/RELAY.md) Section 13 and
> [`../app/BEDROCK_TUNNEL.md`](../app/BEDROCK_TUNNEL.md) Section 9 for the
> `bedrock.enabled` key, and "Bedrock (Geyser)" below for turning Bedrock on.

> **Caveat — a running Minecraft container keeps the network it was started
> on.** `compose.yaml` declares a second pinned network, `mcsd-servers`,
> attaches `worker` to both it and `mcsd`, and points
> `MCD_WORKER_DRIVER_CONTAINER_NETWORK` at it, so an MC container is attached to
> `mcsd-servers` at the moment the worker starts it — and never re-attached
> afterwards.
>
> **Restart every running server after a network change.** `docker compose up
> -d` creates or renames networks and recreates `worker`. A Minecraft container
> that is already running is **not** moved: it stays attached to the network it
> was started on and keeps that network's full reach — a container started on
> `mcsd` (by an override that points the worker there) reaches everything on
> it; `grpcurl -plaintext seaweedfs:18333 list` answers from inside it. Each
> server lands on the configured network only at its next start, so until you
> cycle them the segmentation is not in effect for them. Whether anything breaks
> in the meantime depends on the old network. If the worker is still attached
> to it (as it is to `mcsd`), the dual-homed worker resolves the containers by
> name and the change is non-disruptive — but "non-disruptive" and "segmented"
> are different states, and `up -d` alone only gives you the first. If the
> network was renamed out from under them (a `COMPOSE_PROJECT_NAME` change; see
> the project-name caveat below), the worker loses container-name DNS to them
> and their RCON dials fail until each is restarted.
>
> **React if your compose file is customised**: an override that redefines the
> `worker` service's `networks:` list, or a second stack that renames the pinned
> network (as `scripts/compose.relay-e2e.yaml` does), must name **both**
> networks — listing only `default` silently strands the worker without
> container-name DNS and every RCON dial fails. Anything you deliberately
> attached to `mcsd` to talk to a Minecraft container needs moving to
> `mcsd-servers`.

> **Caveat — the API port binds to loopback by default.** `compose.yaml`
> publishes the API HTTP port on `127.0.0.1` (loopback), not `0.0.0.0` (all
> interfaces), so that an operator running the Cloudflare Tunnel profile does
> not also have the plaintext API reachable on `http://<host-ip>:8000` — the
> tunnel section promises no inbound port. Only `cloudflared` (on `mcsd`) and
> same-host processes reach the API by default. **If your deployment relies on
> the API being reachable from the network** (no tunnel, no same-host reverse
> proxy, or LAN/dev setups), add to `.env` before rebuilding:
>
> ```sh
> API_HTTP_BIND_IP=0.0.0.0
> ```

> **Caveat — the relay's Worker dial-back port binds to loopback by
> default.** `compose.yaml` publishes the relay's `25665` on
> `${RELAY_CONTROL_BIND_IP:-127.0.0.1}`, not on every interface. The three
> other relay publications — `25565`, `25675/udp` and `19132-19231/udp` — are
> on every interface. The loopback bind closes a path that network segmentation
> does not: a port published with no host IP is DNATed from every interface, so
> a plugin in a Minecraft container would reach the dial-back tunnel through the
> bridge gateway even though the MC containers are off `mcsd`
> ([`../app/SECURITY.md`](../app/SECURITY.md) Section 6).
>
> **A single-host deployment needs `MCD_RELAY_TUNNEL_PUBLIC_ENDPOINT=relay:25665`
> in `.env`.** The in-compose worker dials whatever that variable advertises; on
> the internal `mcsd` network the relay answers as `relay:25665` and no host
> publication is involved. If yours advertises a **host** address instead, the
> worker's dial is refused (the port is on loopback) and the tunnel never comes
> up. Either switch the endpoint to `relay:25665` and regenerate the tunnel
> certificate with a matching SAN (`subjectAltName=DNS:relay`, see "TLS
> material" in [Section 12](#12-relay-game-ingress)), or keep the host address
> and add `RELAY_CONTROL_BIND_IP=<that address>` to `.env` before rebuilding.
>
> **A worker on another host** needs the port off loopback: set both
> `RELAY_CONTROL_BIND_IP` and `MCD_RELAY_TUNNEL_PUBLIC_ENDPOINT` to an address
> that worker can reach ([Section 8](#8-tls-guidance)).

> **Caveat — PostgreSQL major upgrades; `db-data` mounts at
> `/var/lib/postgresql`.** `compose.yaml` runs `postgres:18`, whose image keeps
> `PGDATA` at `/var/lib/postgresql/<major>/docker` and declares its volume at
> that parent directory; the mount follows that layout. When a revision bumps
> the PostgreSQL major, an existing deployment's `db-data` volume holds data in
> the older major's format, which the new image cannot read: the container
> aborts during entrypoint init — loudly, and without touching your data — and
> because `migrate` and `api` both gate on `db` being healthy, the whole stack
> stays down until the data is restored into the new major.
> `scripts/deploy_preflight.sh` detects a pending major bump and refuses the
> deploy, so `make update` stops before it takes the stack down, and names the
> script below. The preflight runs from the already-checked-out revision (it
> validates before the pull), so it enforces only the checks that revision
> carries. The procedure below is written for the `postgres:17` → `postgres:18`
> case; substitute the majors for any other bump.
>
> **Take the dump while the stack is still running `postgres:17` — before
> `docker compose up` swaps the image.** The `postgres:18` image ships no
> PostgreSQL 17 binary, so once the new container is in place there is no
> supported way to read the old volume. **`git pull` is not that moment**: it
> rewrites files on disk and nothing else, and the running `db` container keeps
> serving the image it was started from until a `docker compose up` recreates it.
> That is what makes the order below safe.
>
> **Primary path — `scripts/pg_major_upgrade.sh`.** Three commands, from the repo
> root, on a clean `main`, with the old stack still up:
>
> ```sh
> git pull --ff-only origin main   # 1. how you obtain the script and the new image
> ./scripts/pg_major_upgrade.sh    # 2. exits 0 doing nothing unless an upgrade is pending
> docker compose up -d --build     # 3. only once step 2 exited 0
> ```
>
> The pull comes first because it has to: the script validates the revision it
> runs from, so running it before the pull leaves it validating the revision
> you are replacing — it reports `nothing to do` and tells you to pull. It is
> safe in either order for the *data* — see the paragraph above. That sequence
> is the same on every upgrade, major or not: step 2 is a no-op when nothing is
> pending.
>
> **Already pulled, or already brought the stack up?** Nothing is lost either
> way. A pull on its own leaves the running stack exactly as it was — pick the
> sequence up at step 2. If the stack was brought up on the new image without
> the preflight (a plain `docker compose up`, or an update that skipped it), the
> `db` container is now `postgres:18` aborting on a PostgreSQL 17 volume and
> **your data is untouched** — that abort happens before postgres starts. Run
> step 2 anyway: it refuses, because the running `db` is not the major that
> wrote the volume, and the refusal names the revision to put back. Then:
>
> ```sh
> MCSD_ALLOW_PRIMARY_BRANCH=1 git checkout <the revision it named>
> docker compose up -d --wait db   # PostgreSQL 17, on the volume it never touched
> git checkout main                # no override needed going back
> ./scripts/pg_major_upgrade.sh    # now it has a 17 to dump
> docker compose up -d --build
> ```
>
> `MCSD_ALLOW_PRIMARY_BRANCH=1` is not optional on the first line: this repo's
> post-checkout hook silently restores the primary checkout to `main`
> ([`AGENTS.md`](AGENTS.md) Section 1), which would put `postgres:18` straight
> back and start it on the 17 data.
>
> It refuses unless an upgrade is actually pending (so re-running after a
> successful one is a no-op), refuses unless the running `db` container is still
> the major that wrote the volume — which is also the check that catches a stack
> already restarted onto `postgres:18` — stops the writers, and takes the dump,
> then **verifies it**, on `pg_dumpall`'s own exit status *and* PostgreSQL's
> end-of-dump marker, before anything destructive happens. Everything it
> validates comes from `HEAD`, the revision you just pulled and the one step 3
> deploys, so no push landing on `main` mid-run can make it restore into an image
> it never checked. It archives the old volume to a host-side tarball and lists
> that tarball back, and only then removes the volume; the PostgreSQL 18 `db`
> comes up with `--wait` and the dump is restored into it under
> `ON_ERROR_STOP=1`, so a statement that errors halfway aborts the run rather
> than reporting a half-restored database as a success. Any failure exits
> non-zero with the old volume still there.
>
> Nothing invokes this script for you, by design: `make update` and
> `scripts/deploy.sh` never trigger it. The API is down for the duration and the
> volume swap is irreversible, so it runs when *you* have chosen to.
>
> Artifacts (dump, volume archive, restore log) land in a timestamped directory
> next to the repo, or in `$MCSD_PG_UPGRADE_DIR` if you set one. **They are yours
> to delete** — the script never removes them, because until you have verified
> the new cluster they are the only copies of the PostgreSQL 17 data. If a run
> fails after the volume has been released, it prints the exact commands to put
> the old data back from the archive; follow them and the deployment returns to
> the cluster it started on, on a checkout that runs it.
>
> One line of that recovery block is worth knowing about in advance: it checks
> out **a different revision than the one you just pulled**, because bringing the
> old data back means bringing back a `compose.yaml` that pins the old major.
> Your checkout is on the new revision by then, and nothing on the host reliably
> records which revision the running stack was built from (`.last-deploy-sha` is
> written only by `make deploy` / `make update`, so a deployment ever brought up
> with the plain `docker compose up -d --build` of Section 4 has none, and
> nothing about the file distinguishes a stale value from a current one). So
> the script derives it from this repo's own
> history: the newest revision whose `compose.yaml` deploys the major the volume
> holds, **verified** by reading that revision's image the same way it read
> `HEAD`'s. If history contains no such revision, it says so up front, before
> anything destructive, and the recovery block tells you what to look for rather
> than naming a revision that would start the wrong major.
>
> While the old volume is gone and the new cluster is not yet restored, the
> script keeps `.pg-upgrade-incomplete` in the repo root (gitignored, like
> `.last-deploy-sha`). A re-run that finds it **refuses** rather than reporting
> "nothing to do": a partially restored PostgreSQL 18 cluster and a finished one
> are both just "the volume holds 18", and bringing the stack up on the first is
> the one outcome this whole procedure exists to prevent. That refusal prints the
> same recovery commands, reconstructed from what the unfinished run recorded in
> the file — you do not need the original run's output still on screen. The
> recovery instructions tell you when to delete it.
>
> Finish with `docker compose up -d --build`, then log in and check that servers,
> backups, and snapshots resolve.
>
> **Manual fallback.** The same sequence by hand, including the two checks the
> script exists to enforce:
>
> ```sh
> # 0. Take the new revision. This swaps no containers -- the running `db` keeps
> #    serving postgres:17 until step 3 recreates it -- and it is what puts the
> #    new compose.yaml on disk. Note the revision you are leaving; step 5 of the
> #    recovery in the script's output is the only thing that needs it, but if
> #    you are doing this by hand, `git rev-parse HEAD` BEFORE this line is the
> #    cheapest way to have it.
> git pull --ff-only origin main
>
> # 1. With the 17 stack still up. Stop the writers FIRST:
> #    `api` is the only DB client (`worker` and `relay` reach it over gRPC), so
> #    anything written after the dump would be lost on restore.
> docker compose stop api worker relay cloudflared
> docker compose exec -T db pg_dumpall -U mcsd > backup-pg17.sql   # check $?
> tail -n 5 backup-pg17.sql | grep 'PostgreSQL database cluster dump complete'
>
> # 2. Archive the volume BEFORE releasing it, and list the archive back. Without
> #    this the dump is the only copy of the data the moment the volume goes.
> docker compose down
> docker run --rm --entrypoint sh \
>   -v mc-server-dashboard-v2_db-data:/src:ro -v "$(pwd)/..":/out postgres:17 \
>   -c 'tar czf /out/db-data-pg17.tar.gz -C /src .'
> tar tzf ../db-data-pg17.tar.gz | grep PG_VERSION   # prints it, or the archive is bad
> docker volume rm mc-server-dashboard-v2_db-data
>
> # 3. Start the 18 db on a fresh volume (step 0 already put its compose.yaml
> #    in place).
> #    `--wait` is NOT enough on its own. It blocks on the compose healthcheck,
> #    which is `pg_isready` over the container's unix socket -- and the image's
> #    entrypoint runs a TEMPORARY server on that same socket to execute its init
> #    scripts before shutting it down and starting the real one. `--wait` can
> #    therefore return mid-bootstrap and the restore then dies with
> #    "FATAL: the database system is shutting down". The socket answers
> #    seconds before TCP opens. The temp server runs with
> #    `listen_addresses=''`, so waiting for TCP tells the two apart
> #    structurally -- it can never answer.
> docker compose up -d --wait db
> until docker compose exec -T db pg_isready -q -h 127.0.0.1 -U mcsd -d postgres
> do sleep 1; done
>
> # 4. Restore. `ON_ERROR_STOP=1` is what makes a failed statement visible:
> #    without it psql exits 0 after an error and a half-restored database is
> #    indistinguishable from a good one. That means the two statements the new
> #    container's initdb already ran from .env have to be REMOVED rather than
> #    tolerated -- drop the empty database it created (the dump recreates it
> #    with the original encoding and locale), and filter the single CREATE ROLE
> #    line for the role you connect as, which cannot be dropped. Its following
> #    `ALTER ROLE` carries the attributes and password, so nothing is lost.
> grep -c -x -F 'CREATE ROLE mcsd;' backup-pg17.sql       # must print exactly 1
> docker compose exec -T db psql -v ON_ERROR_STOP=1 -U mcsd -d postgres \
>   -c 'DROP DATABASE IF EXISTS mcsd'
> grep -v -x -F 'CREATE ROLE mcsd;' backup-pg17.sql \
>   | docker compose exec -T db psql -v ON_ERROR_STOP=1 -U mcsd -d postgres
>
> # 5. Only once that exited 0, bring the rest of the stack up:
> docker compose up -d --build
> ```
>
> All three checks are load-bearing. `pg_dumpall` failing partway — full disk,
> broken pipe, a non-zero exit — still leaves a non-empty, plausible-looking
> `backup-pg17.sql`, because the shell created the file before the command ran;
> the completion marker is the only evidence the dump finished. An archive that
> will not list back is not a copy you can restore from, so checking it is what
> makes the `docker volume rm` on the next line safe. And `ON_ERROR_STOP=1` is
> the difference between a failed `COPY` stopping the restore and a table that
> exists with rows missing from it.
>
> Do **not** substitute "read the restore log and ignore the errors you
> recognise" for that flag. The two expected errors sit in the same list as a
> real one and look no different — a run with a genuinely broken statement in it
> prints three `ERROR:` lines where a good run prints two, and nothing marks
> which is which.
>
> (The volume name is `<project>_db-data`, where `<project>` is `mcsd` unless
> the deployment runs under another project name (`-p`, `COMPOSE_PROJECT_NAME`
> — see the project-name caveat below), in which case every `docker compose`
> line here needs `-p <that name>` to address it. `docker volume ls` shows the
> exact names. Naming `relay`/`cloudflared` is harmless when
> those profiles are inactive. `-T` on the dump matters: without it Compose
> allocates a TTY and the redirected SQL is line-ending mangled. `mcsd` above is
> `POSTGRES_USER` / `POSTGRES_DB` from your `.env` — substitute yours in all
> four places.)
>
> For a large cluster, `pg_upgrade --link` converts in place much faster, but it
> needs both majors' binaries in one image and is not covered here.

> **Caveat — the compose project is pinned to `mcsd`, and changing the project
> name moves the named volumes.** `compose.yaml` carries a top-level
> `name: mcsd` and derives both network names from it
> (`${COMPOSE_PROJECT_NAME:-mcsd}` / `${COMPOSE_PROJECT_NAME:-mcsd}-servers`),
> so a second stack brought up with `-p <name>` gets its own fabric instead of
> joining this one. A deployment that takes the default renders `mcsd` and
> `mcsd-servers`, and the worker attaches MC containers to `mcsd-servers`.
>
> **Compose scopes volumes by project.** A stack brought up under a different
> project name — an explicit `-p <name>` or a `COMPOSE_PROJECT_NAME` in `.env`
> — holds its live data in `<name>_db-data`, `<name>_api-storage` and
> `<name>_seaweedfs-data`.
> Project `mcsd` does not see those volumes: a plain `docker compose up -d`
> creates **empty** `mcsd_*` volumes beside them and boots an empty database and
> object store, while the old containers are still running and holding the host
> ports. Nothing is deleted, and nothing warns you either.
>
> To move such a stack onto the pinned name, copy the data across once, with the
> stack down. Stop every Minecraft server from the dashboard first — their
> containers sit on the old stack's servers network and hold it open — then,
> from the repo root (the commands use `mc-server-dashboard-v2` as the example
> old name; substitute yours):
>
> ```sh
> docker compose -p mc-server-dashboard-v2 down          # the -p is required:
>                                                        # a bare `down`
>                                                        # resolves to `mcsd`
>                                                        # and stops nothing
> for v in db-data api-storage seaweedfs-data; do
>   docker volume create "mcsd_$v"
>   docker run --rm -v "mc-server-dashboard-v2_$v":/from -v "mcsd_$v":/to \
>     debian:bookworm-slim sh -c 'cp -a /from/. /to/'
> done
> docker compose up -d --build
> ```
>
> Verify before deleting anything: `GET /api/healthz`, the server list in the
> UI, and one server start (which proves the object store and the scratch dir
> came across). Only then `docker volume rm mc-server-dashboard-v2_db-data
> mc-server-dashboard-v2_api-storage mc-server-dashboard-v2_seaweedfs-data`.
> `MCSD_SCRATCH_DIR` is a host bind mount, not a named volume, so it is not
> affected.
>
> `scripts/deploy_preflight.sh` reads the db volume name out of the rendered
> compose config, so until the copy is done it looks for `mcsd_db-data`, finds
> nothing, and reports "no db-data volume yet (fresh deployment)" — its
> PostgreSQL-major guard is inert for that window, which is one more reason not
> to put the copy off.
>
> **A deployment already under project `mcsd`** (an `-p mcsd` invocation, or
> `COMPOSE_PROJECT_NAME=mcsd` in `.env`) has nothing to do: the pin only writes
> down what it is using.
>
> **The cheaper alternative, and what it costs.** `COMPOSE_PROJECT_NAME` in
> `.env` overrides the `name:` key, so pinning your existing project name there
> keeps the volumes exactly where they are and moves no data:
>
> ```sh
> echo 'COMPOSE_PROJECT_NAME=mc-server-dashboard-v2' >> .env
> ```
>
> The trade is the mirror image: the **networks** are then named after that
> project (`mc-server-dashboard-v2` / `mc-server-dashboard-v2-servers`), so if
> the stack's networks are named differently (the `mcsd` / `mcsd-servers`
> default), the next `up` recreates every service container, and Minecraft
> containers that are already running stay on the old servers network — the
> worker loses container-name DNS to them and their RCON dials fail until each
> is restarted (the same cycle-your-servers step the network caveat above
> describes). Take this path only if you would rather cycle servers than copy
> volumes.

The `api` image pre-creates the storage mount point owned by the app user, so a
fresh `api-storage` volume is writable. An `api-storage` volume owned by root
(its ownership is fixed when the volume is first populated) is not: the non-root
app (uid 10001) cannot write to it. Fix the ownership once, then bring the stack
up:

```sh
docker run --rm -v mcsd_api-storage:/fix \
  debian:bookworm-slim chown 10001:10001 /fix
```

(The volume name is `<project>_api-storage`, where `<project>` is the project
name `compose.yaml` pins — `mcsd` — unless you override it with
`COMPOSE_PROJECT_NAME` / `-p`. `docker volume ls` shows the exact names.)

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
docker compose exec -T db pg_dump -U mcsd -d mcsd > backup-db.sql
docker run --rm -v mcsd_seaweedfs-data:/data \
  -v "$PWD":/backup debian:bookworm-slim \
  tar czf /backup/backup-storage.tar.gz -C /data .
```

For the `fs` backend, archive the `api-storage` volume instead:

```sh
docker run --rm -v mcsd_api-storage:/data \
  -v "$PWD":/backup debian:bookworm-slim \
  tar czf /backup/backup-storage.tar.gz -C /data .
```

(The volume name is `<project>_api-storage`, where `<project>` is the project
name `compose.yaml` pins — `mcsd` — unless you override it; `docker volume ls`
shows the exact names.) The worker scratch dir
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
  server's `name` comes from the request, not the archive.

### Export format (`format: 1`)

`export_metadata.json` lives at the root of the ZIP and carries:

| field         | meaning                                              |
| ------------- | ---------------------------------------------------- |
| `format`      | the format version — `1`                             |
| `name`        | the source server's name (informational; import uses the request name) |
| `mc_edition`  | the Minecraft edition (`java`)                       |
| `mc_version`  | the Minecraft version                                |
| `server_type` | the server type (`vanilla` / `paper` / `fabric` / `forge`) |
| `exported_at` | the export timestamp (ISO 8601, UTC)                 |

On import the `format` field must equal `1`, and `server_type` / `mc_version` are
re-validated against the version catalog (the same check `create` runs), so an
unsupported type is rejected. The `export_metadata.json` member
itself is never written into the new working set.

Only archives produced by this system's export (`format: 1`) are importable.

## 12. Relay (game ingress)

The relay lets players join at `<slug>.<base_domain>` (e.g.
`amber-falcon-42.mc.example.com`) with no port number and no client mods, and
it keeps the Worker's IP off the internet — including when the Worker runs
behind NAT. See `docs/app/RELAY.md` for the full design.

The relay is **opt-in**: the `relay` compose profile is inactive by default.
Enable it only when you have a public
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
# (`relay` for the in-compose worker; e.g. relay.example.com for one on another
# host). The SAN must match the host the Worker dials — Go ignores CN and
# requires a matching DNS or IP SAN.
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
   | `MCD_RELAY_TUNNEL_PUBLIC_ENDPOINT` | `relay:25665` for the in-compose worker; `<reachable-host>:25665` for a worker on another host |
   | `RELAY_CONTROL_BIND_IP` | leave unset (loopback) for the in-compose worker; the same reachable address for a worker on another host |
   | `MCD_RELAY_TLS_DIR` | host path to `tunnel-cert.pem` / `tunnel-key.pem` |

   The last two go together. `25665` is the Worker dial-back, not
   a player port, so `compose.yaml` publishes it on `127.0.0.1` by default: the
   in-compose worker reaches the relay over the `mcsd` network as `relay:25665`
   and never touches the host port, and the loopback bind is what keeps the
   dial-back out of reach of the Minecraft containers
   ([`../app/SECURITY.md`](../app/SECURITY.md) Section 6). Only a worker on
   another host needs the port off loopback — see
   [Section 8](#the-relay-tunnel-port-for-a-cross-host-worker). The
   three player-facing / Bedrock publications (`25565`, `25675/udp`,
   `19132-19231/udp`) are on every interface either way.

3. Rebuild and bring the stack up:

   ```sh
   docker compose up -d --build
   ```

### Single-host port collision

The relay binds `0.0.0.0:25565` for the game listener, overlapping the API's
default game-port allocator range (`25565..25664`). This is handled
automatically: when `relay.enabled=true` — which the relay setup above requires —
the allocator excludes the relay's published host binds (`relay.game_port`,
`relay.tunnel_port`) from the assignable range, so the first server created on a
single host is assigned `25566`. Only binds inside the range are
excluded; by default that is just 25565, since the tunnel's 25665 sits above
`range_end`. The exclusion covers auto-assignment, explicit `game_port` requests
(rejected as taken), and the free-port listings alike.

`MCD_API_PORTS__RANGE_START` (e.g. `25566`) is available as a fallback knob for
shifting the range; the documented single-host relay setup does not need it.

**Residual case:** the exclusion applies at assignment time only. A server
created *before* the relay was enabled keeps its `game_port`, so one already
holding 25565 still collides when the relay comes up — reassign its `game_port`
before enabling the relay.

### Reconciler grace after an API restart

When a worker or the `api` container is recreated (e.g. a UI-only redeploy), the
startup reset / worker orphan sweep marks the bounced servers `observed=unknown`;
the reconciler then waits for the divergence to outlast a **grace window** before
re-dispatching the start. The grace is per-action:

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
  first dispatch and spawning a duplicate live instance on another worker, so
  it is **not** safe to shorten on these paths.

During the grace window the relay maps `observed=unknown` → `STOPPED` and players
get a "server stopped" MOTD even though the MC containers are healthy;
the fast held path keeps that window short for routine single-host restarts without
the operator lowering `grace_seconds` below its safety floor.

Both knobs have boot-time safety floors (a warning, not fatal): `grace_seconds`
must exceed `max(hydrate_timeout + command_timeout, snapshot_timeout, stop_timeout)`,
and `held_start_grace_seconds` must exceed `command_timeout_seconds` (it only covers
a command-only start). Lowering `grace_seconds` below its floor reopens the
duplicate-start / stale-snapshot / stale-stop-replay races; prefer the (already
short by default) held path over shrinking the full grace.

The reconciler knobs (`INTERVAL_SECONDS`, `GRACE_SECONDS`, `BACKOFF_BASE_SECONDS`,
`BACKOFF_MAX_SECONDS`) are forwarded via `compose.yaml`; see `.env.example` for
their defaults and `api/src/mc_server_dashboard_api/config.py` for constraints
(`backoff_max_seconds` must be ≥ 600 to keep crash-loop damping effective).
`held_start_grace_seconds` defaults to 90 in the application and is set via
`MCD_API_RECONCILER__HELD_START_GRACE_SECONDS` (forwarded through
`compose.yaml` like the other reconciler knobs).

### Direct path vs relay path

| | Direct path (default) | Relay path |
|---|---|---|
| `relay.enabled` (API) | `false` (default) | `true` |
| Player address | `<worker host>:<game_port>` | `<slug>.<base_domain>` |
| `driver.container.game_bind_ip` | `0.0.0.0` (compose default) | `127.0.0.1` — no inbound game port needed |
| `MCD_WORKER_GAME_BIND_IP` in `.env` | unset (defaults to `0.0.0.0`) | `127.0.0.1` |
| Host firewall (worker) | game-port range open | nothing inbound on the Worker |

When the relay is enabled, set `MCD_WORKER_GAME_BIND_IP=127.0.0.1` in `.env`
so game ports bind only on loopback — the Worker dials its own loopback game
port into the tunnel, and no inbound game-port range is needed on the worker
host. The relay takes all inbound player traffic on port 25565; on a single host
the allocator keeps new servers off that port automatically (see
[Single-host port collision](#single-host-port-collision)).

The two paths are not mutually exclusive at the protocol level (a server is
reachable both ways while switching between them); `relay.enabled` governs whether the
relay control surface is active.

### Firewall summary (relay host)

| Port | Protocol | Direction | Purpose |
|---|---|---|---|
| 25565 | TCP | inbound | player game connections |
| 25665 | TCP | host-local (loopback) by default | Worker dial-back (TLS tunnel). Published on `${RELAY_CONTROL_BIND_IP:-127.0.0.1}`, so the in-compose worker (which dials `relay:25665` over `mcsd`) needs nothing inbound here. Open it — and set both `RELAY_CONTROL_BIND_IP` and `MCD_RELAY_TUNNEL_PUBLIC_ENDPOINT` — only for a worker on another host (Section 8) |
| 25675 | UDP | inbound | Worker's Bedrock QUIC tunnel dial-back (`bedrock.tunnel_listen`) — only when the Bedrock gate is on |
| 19132-19231 | UDP | inbound | Bedrock player connections (`ports.bedrock_range_start..end` default window) — only when the Bedrock gate is on |
| 50051 | TCP | internal (the `mcsd` network only — not `mcsd-servers`) | gRPC control plane (not published) |
| 9090 | TCP | internal (the `mcsd` network only — not `mcsd-servers`) | API Prometheus exposition (not published; off unless `MCD_API_METRICS__ENABLED=true` — see Section 8) |
| 9090 | TCP | relay-local (loopback) | Relay Prometheus exposition + `/healthz` (off unless `MCD_RELAY_METRICS_ENABLED=true`; binds `127.0.0.1` by default, RELAY.md Section 13). Same port number as the API's, above: distinct containers under compose, but a collision if both run natively on this host |

### Bedrock (Geyser)

Bedrock-edition players can join through the same relay, over a separate QUIC
tunnel and per-server UDP ingress (see `docs/app/BEDROCK.md` for the feature
overview and `docs/app/BEDROCK_TUNNEL.md` for the wire-level design). It builds
on the relay setup above (same tunnel TLS material; DNS needs one extra
record — see below) and needs these additional steps:

1. Add an **apex** `A`/`AAAA` record for the *bare* base domain, pointing at
   the relay host's public IP — **DNS-only (not proxied)**:

   ```
   <base_domain>      A    <relay public IP>
   ```

   The relay's wildcard `*.<base_domain>` covers Java's `<slug>.<base_domain>`
   join hostnames, but Bedrock's join address is the bare `<base_domain>`
   (routed by UDP port, no slug), and a wildcard does not match the apex. An
   HTTP(S) proxy (e.g. Cloudflare orange-cloud) must NOT be enabled on either
   record — it won't pass Minecraft's TCP or Bedrock's UDP. Without the apex
   record, Bedrock clients must connect by raw IP; the hostname fails with
   Bedrock's "Server address is not correctly formatted" (its message for a
   hostname it can't resolve).
2. Set `MCD_API_RELAY__BEDROCK_ENABLED=true` in `.env` (see `.env.example`) —
   this single flag also gates the relay's own Bedrock listener (see the
   Bedrock caveat in [Section 9](#9-upgrade)).
3. Open the two additional firewall rows above on the relay host: the Bedrock
   QUIC tunnel port (25675/udp) and the client-facing UDP window
   (19132-19231/udp, `compose.yaml`'s relay service already publishes both).
4. Rebuild and bring the stack up (`docker compose up -d --build`), same as
   [Enabling the relay profile](#enabling-the-relay-profile).

No other configuration is required: installing Geyser (Modrinth catalog) and
Floodgate (jar upload — no Spigot build on Modrinth) on a Paper
server through the normal plugin flow is what allocates its `bedrock_port` and
opens the tunnel on start (`docs/app/BEDROCK.md` "Activation").

#### UDP receive buffer (`net.core.rmem_max`)

For a Bedrock-enabled relay expecting load, raise the host's maximum UDP socket
receive buffer. On each bound Bedrock UDP port the relay requests a 4 MiB
receive buffer (`SO_RCVBUF`) so it can keep draining inbound RakNet datagrams
across a transient stall in its outbound QUIC send path, instead of letting the
kernel drop a burst for every flow on that port. On a default
Linux host `net.core.rmem_max` is ~208 KiB, so the kernel silently clamps the
4 MiB request and the enlargement has little effect — the relay logs a
`UDP receive buffer clamped below requested size` warning when this happens
(quic-go logs the same for the Bedrock QUIC tunnel socket). Mirroring quic-go's
[UDP Buffer Sizes](https://github.com/quic-go/quic-go/wiki/UDP-Buffer-Sizes)
guidance, raise the ceiling `net.core.rmem_max` (and the per-socket default
`net.core.rmem_default`) on the relay host:

```sh
sysctl -w net.core.rmem_max=7500000
sysctl -w net.core.rmem_default=7500000
```

Persist it across reboots with a drop-in such as
`/etc/sysctl.d/99-mcsd-relay.conf`. This only matters for Bedrock-enabled
relays under load: the relay's reader/sender decoupling already keeps one
congested flow from stalling the others regardless of buffer size, so leaving
`rmem_max` at its default is safe — just less resilient to inbound datagram
bursts.

#### Manual verification (real Bedrock client)

The e2e suite (`make bedrock-e2e`, `.github/workflows/bedrock-e2e.yml`) covers
the tunnel's data path against a fake Geyser responder — CI does not boot real
Geyser (a Modrinth/GeyserMC download would make it flaky) or join a real
Bedrock client. Verify those manually against a live deployment:

1. Enable the Bedrock gate (above) and confirm `GET /api/meta` reports
   `bedrock_enabled: true`.
2. Create a Paper server, install Geyser from the plugin catalog and Floodgate
   via jar upload (see `docs/app/BEDROCK.md`), and start the server.
3. Confirm the server response carries `bedrock_address` / `bedrock_port` (also
   shown as a badge in the Web UI, `docs/ui/WEBUI_SPEC.md`).
4. From a machine that can reach the relay host, smoke-test the RakNet
   listener is actually answering before trying a real client — an Unconnected
   Ping/Pong round trip, printed as hex (replace `<host>`/`<port>` with the
   reported `bedrock_address`/`bedrock_port`):

   ```sh
   printf '\x01\x00\x00\x00\x00\x00\x00\x00\x01\x00\xff\xff\x00\xfe\xfe\xfe\xfe\xfd\xfd\xfd\xfd\x12\x34\x56\x78\x00\x00\x00\x00\x00\x00\x00\x01' \
     | timeout 3 nc -u -w2 <host> <port> | xxd | head -3
   ```

   A reply starting with byte `1c` is an Unconnected Pong — Geyser is up and
   reachable through the relay.
5. Join from a real Bedrock client: add a server at `<host>:<port>` (the
   reported `bedrock_address`/`bedrock_port`; no SRV record, the port must be
   typed). Expect Floodgate auth (no Java account) and, on an older Java
   server version, degraded compatibility unless ViaVersion is installed (see
   `docs/app/BEDROCK.md` "Limitations").
