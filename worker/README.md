# worker/

The Go execution agent of mc-server-dashboard. It runs Minecraft server
processes on a host and reports observed state to the authoritative `api/`
service over the gRPC control plane. See
[`docs/app/ARCHITECTURE.md`](../docs/app/ARCHITECTURE.md) for the system design;
this README covers how to build, test, lint, configure, and run the module.

The control-plane session is implemented: the Worker loads its configuration,
dials the API, registers with its advertised capabilities, heartbeats, and
reconnects with backoff (see [Running against a local API](#running-against-a-local-api)).
Execution drivers and real command handling are later epics; inbound commands
are currently acknowledged with an "unsupported" error rather than dropped.

## Layout

Hexagonal layering (ARCHITECTURE.md Section 2) applied idiomatically to Go:

```
worker/
├── cmd/worker/            # edge / wiring: config load, dial, run, signals
└── internal/
    ├── domain/            # pure core: entities, value objects, Ports
    │   └── session/       # control-plane session state machine + backoff
    ├── application/       # use cases, depending only on domain
    └── adapters/          # concrete Port implementations (drivers, clients)
        ├── clock/         # wall-clock Clock adapter
        ├── config/        # TOML + MCD_WORKER_ env config loader
        └── controlplane/  # gRPC client for the Session stream
```

Dependency direction points inward to `domain`; see ARCHITECTURE.md Section 2.2.

The generated control-plane gRPC stubs are checked in under
`internal/controlplane/` (package `controlplanev1`). Do not edit them by hand;
regenerate with `make proto-gen` from the repo root (see
[`../proto/README.md`](../proto/README.md)).

## Toolchain

- **Go**: 1.26 (pinned in `go.mod`; see
  [`docs/dev/DEPENDENCIES.md`](../docs/dev/DEPENDENCIES.md)).
- **golangci-lint**: 2.12.2.

Run every command below from this `worker/` directory.

### Install golangci-lint (one-time)

golangci-lint is not part of the Go distribution, so install the pinned version
into the module-local `./.bin` (gitignored):

```sh
GOBIN="$(pwd)/.bin" go install github.com/golangci/golangci-lint/v2/cmd/golangci-lint@v2.12.2
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
`MCD_E2E_API_URL` + `MCD_E2E_CREDENTIAL`, and
[Container-driver restart e2e](#container-driver-restart-e2e) needs
`MCD_E2E_DOCKER` + `MCD_E2E_STUB_IMAGE` (and a reachable Docker daemon).

## Cross-language data-plane e2e

`test/e2e/` drives the **real** Go data-plane client against a **real** running
Python API, proving the tar conventions, status codes, and auth header line up
end to end (issue #111). CI runs it in `.github/workflows/e2e.yml`; to dry-run it
locally, boot the API and point the test at it.

The data-plane endpoints need only Storage and the Worker credential, so the
control plane can stay disabled. The hydrate path's resolved-JAR lookup reads the
`servers` table, so the API needs a migrated database — point it at a local
Postgres (the `api/` README covers spinning one up for its integration tests).

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
running in a **new** container (issue #234). It is the structural guard for the
create-vs-async-remover restart race (#226/#229/#233): three rounds of fixes each
passed their unit fakes while the real daemon found a new interleaving, so this
scenario needs a real daemon to reproduce the class.

The scenario stands in for the Minecraft process with a tiny stub image
(`test/e2e/stub/`): a `java` shim that ignores its args and blocks until SIGTERM,
so the container stays running and `docker stop` ends it cleanly. It uses a unique
per-run worker id and the deterministic `mcsd-<server-id>` container name, so its
orphan sweep and cleanup touch only its own container — never another server on
the host. CI runs it as the `container-restart` job in
`.github/workflows/e2e.yml` (the GitHub-hosted runner has Docker preinstalled).

```sh
# 1. Build the stub image (once; rebuild if the Dockerfile changes).
docker build -t mcsd-e2e-stub:latest worker/test/e2e/stub

# 2. From worker/: run the restart scenario against the local daemon.
MCD_E2E_DOCKER=1 \
MCD_E2E_STUB_IMAGE=mcsd-e2e-stub:latest \
  go test -tags e2e -v -timeout 300s -run TestContainerRestart ./test/e2e/...
```

On a host where Docker needs a group wrapper, prefix the commands with it
(e.g. `sg docker -c "..."`). The test talks to the daemon over the default
`unix:///var/run/docker.sock`.

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
| `api.data_plane_url` | `MCD_WORKER_API_DATA_PLANE_URL` | yes | API HTTP data-plane base URL. |
| `api.credential` | `MCD_WORKER_API_CREDENTIAL` | yes (secret) | Worker credential, sent as stream metadata. |
| `api.tls.ca_file` | `MCD_WORKER_API_TLS_CA_FILE` | yes¹ | CA bundle verifying the API's TLS. |
| `api.tls.insecure` | `MCD_WORKER_API_TLS_INSECURE` | no | `true` opts in to a plaintext (no-TLS) dial for local dev; default `false`. |
| `api.tls.client_cert_file` / `api.tls.client_key_file` | `…_CLIENT_CERT_FILE` / `…_CLIENT_KEY_FILE` | no | mTLS client cert/key pair. |
| `worker.id` | `MCD_WORKER_WORKER_ID` | no | Registration id; **must be a UUID** (the API rejects a non-UUID id with `INVALID_ARGUMENT`, and the Worker fails fast at config load if you set a non-UUID). When unset, a UUID is generated and persisted at `<worker.scratch_dir>/worker-id` on first boot and reused on later restarts, so zero-config workers keep a stable id. **Upgrade impact:** a Worker that previously defaulted to its hostname gets a new UUID on first boot after upgrading, so the API sees a new Worker and the old `assigned_worker_id` rows are orphaned (recovered via the disconnect/mark-unknown path; servers restart cleanly on hydrate). One-time transition. |
| `worker.drivers` | `MCD_WORKER_WORKER_DRIVERS` | no | Comma-separated `host-process` / `container`; default `host-process`. |
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
MCD_WORKER_API_DATA_PLANE_URL=http://localhost:8000/data \
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
data_plane_url = "http://localhost:8000/data"

[api.tls]
insecure = true  # local dev only; set ca_file instead in production

[worker]
scratch_dir = "/tmp/mcsd-worker"
drivers = ["host-process"]
```

The Worker registers, then emits a heartbeat every interval the API returns in
its `RegisterAck`. Stop it with Ctrl-C (`SIGINT`) or `SIGTERM`; it closes the
stream cleanly. If the connection drops it reconnects with exponential backoff
and re-registers from scratch (CONTROL_PLANE.md Section 4.4).
