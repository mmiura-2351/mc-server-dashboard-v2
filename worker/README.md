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
| `api.tls.ca_file` | `MCD_WORKER_API_TLS_CA_FILE` | no | CA bundle for the API's TLS; empty dials insecurely (local only). |
| `api.tls.client_cert_file` / `api.tls.client_key_file` | `…_CLIENT_CERT_FILE` / `…_CLIENT_KEY_FILE` | no | mTLS client cert/key pair. |
| `worker.id` | `MCD_WORKER_WORKER_ID` | no | Registration id; defaults to the host name. |
| `worker.drivers` | `MCD_WORKER_WORKER_DRIVERS` | no | Comma-separated `host-process` / `container`; default `host-process`. |
| `worker.max_servers` | `MCD_WORKER_WORKER_MAX_SERVERS` | no | Capacity hint; default `0` (no cap). |
| `worker.scratch_dir` | `MCD_WORKER_WORKER_SCRATCH_DIR` | yes | Local working-set root. |
| `log.level` / `log.format` | `MCD_WORKER_LOG_LEVEL` / `…_LOG_FORMAT` | no | `info` / `json` by default; format is `json` or `text`. |

## Running against a local API

With a local API control-plane server listening (e.g. on `localhost:50051`),
run the Worker pointing at it. For local development without TLS, leave
`api.tls.ca_file` unset to dial insecurely:

```sh
MCD_WORKER_API_GRPC_ENDPOINT=localhost:50051 \
MCD_WORKER_API_DATA_PLANE_URL=http://localhost:8000/data \
MCD_WORKER_API_CREDENTIAL=dev-secret \
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

[worker]
scratch_dir = "/tmp/mcsd-worker"
drivers = ["host-process"]
```

The Worker registers, then emits a heartbeat every interval the API returns in
its `RegisterAck`. Stop it with Ctrl-C (`SIGINT`) or `SIGTERM`; it closes the
stream cleanly. If the connection drops it reconnects with exponential backoff
and re-registers from scratch (CONTROL_PLANE.md Section 4.4).
