# worker/

The Go execution agent of mc-server-dashboard. It runs Minecraft server
processes on a host and reports observed state to the authoritative `api/`
service over the gRPC control plane. See
[`docs/app/ARCHITECTURE.md`](../docs/app/ARCHITECTURE.md) for the system design;
this README covers how to build, test, lint, configure, and run the module.

The Worker loads its configuration, dials the API, registers with its
advertised capabilities, heartbeats, and reconnects with backoff (see
[Running against a local API](#running-against-a-local-api)). Lifecycle and
console commands arriving on the session are handled by the instance-manager
use case through the container execution driver; a command kind with no wired
handler is acknowledged with an "unsupported" error result rather than dropped.

## Layout

Hexagonal layering (ARCHITECTURE.md Section 2) applied idiomatically to Go:

```
worker/
├── cmd/worker/            # edge / wiring: config load, dial, run, signals
└── internal/
    ├── domain/            # pure core: entities, value objects, Ports
    │   ├── execution/     # ExecutionDriver + ServerControl Ports and their value types
    │   └── session/       # control-plane session state machine + backoff
    ├── application/       # use cases, depending only on domain
    │   └── instancemanager/  # control-plane commands → driver calls; observed state → session
    └── adapters/          # concrete Port implementations (drivers, clients)
        ├── bedrocktunnel/ # Worker side of the Bedrock relay QUIC tunnel
        ├── clock/         # wall-clock Clock adapter
        ├── config/        # TOML + MCD_WORKER_ env config loader
        ├── containerdriver/  # ExecutionDriver running servers in Docker containers
        ├── controlplane/  # gRPC client for the Session stream
        ├── datatransfer/  # HTTP data-plane client (working set snapshot/hydrate)
        ├── hostresources/ # host CPU + memory readout
        ├── javaruntime/   # Minecraft version → Java major mapping
        ├── rcon/          # ServerControl over the Source RCON protocol
        ├── regionfsck/    # structural .mca region validation before a snapshot
        └── tunnel/        # Worker side of the relay TLS dial-back tunnel
```

Dependency direction points inward to `domain`; see ARCHITECTURE.md Section 2.2.

The generated control-plane gRPC stubs are checked in under
`internal/controlplane/` (package `controlplanev1`). Do not edit them by hand;
regenerate with `make proto-gen` from the repo root (see
[`../proto/README.md`](../proto/README.md)).

## Toolchain

- **Go**: 1.26 (pinned in `go.mod`; see
  [`docs/dev/DEPENDENCIES.md`](../docs/dev/DEPENDENCIES.md)).
- **golangci-lint**: pinned by `GOLANGCI_VERSION` in the root `Makefile`.

Run every command below from this `worker/` directory, except the `make`
targets, which run from the repo root.

### Install golangci-lint

golangci-lint is not part of the Go distribution, so `make` installs the pinned
version into the module-local `./.bin` (gitignored) and reinstalls it whenever
the pin moves. Do not install it by hand: a hand-placed binary satisfies the
same path and then lints at whatever version it happens to be (#2903). From the
repo root:

```sh
make bootstrap   # or any lint target -- `make worker-lint`, `make relay-lint`
```

## Commands

| Task | Command |
|---|---|
| Format check | `gofmt -l .` (no output = clean) |
| Vet | `go vet ./...` |
| Lint | `./.bin/golangci-lint run` |
| Test | `go test ./...` |
| Build | `go build ./...` |

To auto-format instead of just checking: `gofmt -w .`.

The default `go test ./...` deliberately excludes the cross-language harnesses
under `test/e2e/`: they are behind the `e2e` build tag and each skips unless its
environment is set —
[Cross-language data-plane e2e](#cross-language-data-plane-e2e) needs
`MCD_E2E_API_URL` + `MCD_E2E_CREDENTIAL`,
[Container-driver restart e2e](#container-driver-restart-e2e) needs
`MCD_E2E_DOCKER` + `MCD_E2E_STUB_IMAGE` (and a reachable Docker daemon), and
[Bedrock relay tunnel e2e](#bedrock-relay-tunnel-e2e) needs
`MCD_E2E_DOCKER` + `MCD_BEDROCK_E2E_RELAY_ADDR` + `MCD_BEDROCK_E2E_CA_FILE` +
`MCD_E2E_STUB_GEYSER_IMAGE` (run via `make bedrock-e2e` rather than by hand —
see that section).

## Cross-language data-plane e2e

`test/e2e/` drives the **real** Go data-plane client against a **real** running
Python API, proving the tar conventions, status codes, and auth header line up
end to end. CI runs it in `.github/workflows/e2e.yml`; to dry-run it locally,
boot the API and point the test at it.

The data-plane endpoints need only Storage and the Worker credential, so the
control plane can stay disabled. The hydrate path's resolved-JAR lookup reads the
`servers` table, so the API needs a database at migration head — point it at a local
Postgres (`api/tests/integration/README.md` shows how to start a scratch one in
Docker).

```sh
# 1. From api/: migrate, then boot uvicorn with the data-plane config.
export MCD_API_DATABASE__URL=postgresql+asyncpg://mcsd:mcsd@localhost:5432/mcsd
export MCD_API_CONTROL__ENABLED=false
export MCD_API_CONTROL__WORKER_CREDENTIAL=dev-secret
export MCD_API_AUTH__TOKEN__SIGNING_KEY=dev-signing-key-0123456789abcdef0123
export MCD_API_STORAGE__BACKEND=fs
export MCD_API_STORAGE__FS__ROOT=/tmp/mcsd-e2e-storage
uv run alembic upgrade head
uv run uvicorn mc_server_dashboard_api.app:create_app --factory \
  --host 127.0.0.1 --port 8000 &

# 2. From worker/: run the e2e-tagged test against it.
MCD_E2E_API_URL=http://127.0.0.1:8000 \
MCD_E2E_CREDENTIAL=dev-secret \
  go test -tags e2e -v -run TestSnapshotThenHydrateRoundTrip ./test/e2e/...
```

## Container-driver restart e2e

`test/e2e/restart_e2e_test.go` drives the **real** container `ExecutionDriver`
against a **real** Docker daemon, restarting a server through the worker's
command path (`StartServer` → `RestartServer`) and asserting it returns to
running in a **new** container. It is the structural guard for the
create-vs-async-remover restart race: creating the replacement container races
the daemon's asynchronous removal of the outgoing one, and the interleavings a
real daemon produces are not reproducible with unit fakes, so this scenario
needs a real daemon.

`test/e2e/forge_e2e_test.go` drives the same real driver+daemon through the
Forge supervised-install path: a Forge args-file `StartServer` whose
working set lacks the args file runs a supervised install container
(`mcsd-<id>-install`), which the stub installer satisfies by writing the launch
args file, then proceeds to a running launch container — asserting the args file,
the gone install container, and `logs/forge-install.log`. A companion case proves
an already-installed working set skips the install step.

Both scenarios stand in for the Minecraft process with a tiny stub image
(`test/e2e/stub/`): a `java` shim that branches on its argv — a Forge
`--installServer` invocation writes the launch args file and exits, every other
invocation blocks until SIGTERM, so the container stays running and `docker stop`
ends it cleanly. Each uses a unique per-run worker id (prefixed `e2e-restart-` /
`e2e-forge-`) and the deterministic `mcsd-<server-id>` container name, so its
orphan sweep and cleanup touch only its own containers — never another server on
the host. CI runs both as the `container-restart` job in
`.github/workflows/e2e.yml` (the GitHub-hosted runner has Docker preinstalled).

```sh
# 1. Build the stub image (once; rebuild if the Dockerfile changes).
docker build -t mcsd-e2e-stub:latest worker/test/e2e/stub

# 2. From worker/: run a scenario against the local daemon.
MCD_E2E_DOCKER=1 \
MCD_E2E_STUB_IMAGE=mcsd-e2e-stub:latest \
  go test -tags e2e -v -timeout 300s -run TestContainerRestart ./test/e2e/...

# Or the Forge install scenario:
MCD_E2E_DOCKER=1 \
MCD_E2E_STUB_IMAGE=mcsd-e2e-stub:latest \
  go test -tags e2e -v -timeout 300s -run TestContainerForge ./test/e2e/...
```

On a host where Docker needs a group wrapper, prefix the commands with it
(e.g. `sg docker -c "..."`). The test talks to the daemon over the default
`unix:///var/run/docker.sock`.

## Bedrock relay tunnel e2e

`test/e2e/bedrock_e2e_test.go` drives the **real**
`internal/adapters/bedrocktunnel.Manager` against the real relay's
`bedrock.Listener` (a sibling Go module, so it runs as a separate coordinating
`go test` process — see the file's package doc comment) and a real Docker
container running a fake-Geyser RakNet responder
(`test/e2e/stub-geyser/`), proving the relay-UDP-ingress → QUIC-tunnel →
Worker → container-port data path, flow demultiplexing across concurrent
clients, and relay-port unbind on tunnel teardown. The suite is protocol-level:
real Geyser is deliberately not booted, because a Modrinth/GeyserMC download at
test time would make CI flaky, so the fake-Geyser responder stands in for it.

Run it (from the repo root) the same way CI does:

```sh
make bedrock-e2e   # scripts/run_bedrock_e2e.sh
```

See [`docs/app/BEDROCK.md`](../docs/app/BEDROCK.md) for the feature overview and
[`docs/app/BEDROCK_TUNNEL.md`](../docs/app/BEDROCK_TUNNEL.md) for the wire design.

## Configuration

The Worker reads its configuration from an optional TOML file plus
`MCD_WORKER_`-prefixed environment variables, with environment variables taking
precedence (`defaults < file < env`). The full key reference is
[`docs/app/CONFIGURATION.md`](../docs/app/CONFIGURATION.md) Section 6. A required
key missing everywhere is a fatal startup error; secrets are masked in logs.

Point the Worker at a config file with `MCD_WORKER_CONFIG`. The environment-
variable form of a key is its dotted path upper-cased with dots replaced by
underscores, e.g. `api.grpc_endpoint` → `MCD_WORKER_API_GRPC_ENDPOINT`.

| Key | Env var | Required | Meaning |
|---|---|---|---|
| `api.grpc_endpoint` | `MCD_WORKER_API_GRPC_ENDPOINT` | yes | API control-plane gRPC address to dial. |
| `api.credential` | `MCD_WORKER_API_CREDENTIAL` | yes (secret) | Worker credential, sent as stream metadata. |
| `api.tls.ca_file` | `MCD_WORKER_API_TLS_CA_FILE` | yes¹ | CA bundle verifying the API's TLS. |
| `api.tls.insecure` | `MCD_WORKER_API_TLS_INSECURE` | no | `true` opts in to a plaintext (no-TLS) dial for local dev; default `false`. |
| `api.tls.client_cert_file` / `api.tls.client_key_file` | `…_CLIENT_CERT_FILE` / `…_CLIENT_KEY_FILE` | no | mTLS client cert/key pair. |
| `worker.id` | `MCD_WORKER_WORKER_ID` | no | Registration id; **must be a UUID** (the API rejects a non-UUID id with `INVALID_ARGUMENT`, and the Worker fails fast at config load if you set a non-UUID). When unset, a UUID is generated and persisted at `<worker.scratch_dir>/worker-id` on first boot and reused on later restarts, so zero-config workers keep a stable id. The persisted id is what ties the Worker to its `assigned_worker_id` rows: if it is lost (for example a wiped scratch dir), the API sees a new Worker and the servers assigned to the old id are recovered via the disconnect/mark-unknown path and restart cleanly on hydrate. |
| `worker.drivers` | `MCD_WORKER_WORKER_DRIVERS` | yes | `container` is the only shipped driver and it requires `driver.container.images`, so there is no zero-config default: the set must be supplied and non-empty. |
| `worker.max_servers` | `MCD_WORKER_WORKER_MAX_SERVERS` | no | Capacity hint; default `0` (no cap). |
| `worker.scratch_dir` | `MCD_WORKER_WORKER_SCRATCH_DIR` | yes | Local working-set root. |
| `log.level` / `log.format` | `MCD_WORKER_LOG_LEVEL` / `…_LOG_FORMAT` | no | `info` / `json` by default; format is `json` or `text`. |

¹ `api.tls.ca_file` is required **unless** `api.tls.insecure=true` is set. With
neither, startup fails fast; with `insecure=true` the Worker dials plaintext and
logs a `WARN` at boot. Production must set `ca_file`.

## Running against a local API

With a local API control-plane server listening (e.g. on `localhost:50051`),
run the Worker pointing at it. For local development without TLS, set
`api.tls.insecure=true` to dial plaintext (the Worker logs a `WARN` at boot):

```sh
MCD_WORKER_API_GRPC_ENDPOINT=localhost:50051 \
MCD_WORKER_API_CREDENTIAL=dev-secret \
MCD_WORKER_API_TLS_INSECURE=true \
MCD_WORKER_WORKER_SCRATCH_DIR=/tmp/mcsd-worker \
go run ./cmd/worker
```

Or with a TOML file:

```sh
MCD_WORKER_CONFIG=./worker.toml \
MCD_WORKER_API_CREDENTIAL=dev-secret \
go run ./cmd/worker
```

```toml
# worker.toml
[api]
grpc_endpoint = "localhost:50051"

[api.tls]
insecure = true  # local dev only; set ca_file instead in production

[worker]
scratch_dir = "/tmp/mcsd-worker"
drivers = ["container"]

[driver.container.images]
21 = "eclipse-temurin:21-jre"
```

The Worker registers, then emits a heartbeat every interval the API returns in
its `RegisterAck`. Stop it with Ctrl-C (`SIGINT`) or `SIGTERM`; it closes the
stream cleanly. If the connection drops it reconnects with exponential backoff
and re-registers from scratch (CONTROL_PLANE.md Section 4.4).
