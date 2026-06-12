# relay/

The Go game ingress relay of mc-server-dashboard. Players join a server at
`<slug>.<base_domain>` with no port; the relay parses the Minecraft handshake,
resolves the hostname via the `api/` RelayService, accepts the Worker's
outbound TLS dial-back, and splices the two TCP connections. It holds no
persistent state. See [`docs/app/RELAY.md`](../docs/app/RELAY.md) for the full
design (epic #659); this README covers build, test, lint, configure, and run.

## Layout

Hexagonal layering (ARCHITECTURE.md Section 2) applied to Go, mirroring
`worker/`:

```
relay/
├── cmd/relay/             # edge / wiring: config load, dial, bind, run, signals
└── internal/
    ├── mc/                # the tiny plaintext Minecraft protocol slice
    ├── game/              # public player listener, hostname routing, status cache, IP caps
    ├── tunnel/            # TLS dial-back listener + single-use token rendezvous
    ├── splice/            # bidirectional byte splice with half-close propagation
    ├── session/           # session id minting + batched ReportSessions
    ├── relaysvc/          # Register-with-backoff loop + learned base_domain
    ├── genproto/          # generated mcsd.relay.v1 stubs (see below)
    └── adapters/
        ├── apiclient/     # gRPC client for the API's RelayService
        └── config/        # TOML + MCD_RELAY_ env config loader
```

The generated relay gRPC stubs are checked in under `internal/genproto/`
(package `relayv1`). The relay is a separate Go module, so it cannot import the
worker module's `internal/controlplane/` copy of the same package — Go's
`internal/` rule bars cross-module imports. The relay therefore generates its
own copy from the same `proto/mcsd/relay/v1/relay.proto`, via a dedicated buf
template (`proto/buf.gen.relay.yaml`). Do not edit the stubs by hand;
regenerate with `make proto-gen` from the repo root.

## Toolchain

- **Go**: 1.26 (pinned in `go.mod`).
- **golangci-lint**: 2.12.2 — reuses the binary installed into `worker/.bin`
  (one install for both Go modules). Run `make worker/.bin/golangci-lint` (or
  any `make *-lint`) to install it.

From the repo root:

```
make relay-format       # gofmt -w
make relay-lint         # gofmt check + go vet + golangci-lint
make relay-test         # go test ./...
make relay-test-race    # go test -race ./... (CI gate)
```

## Configuration

TOML file (path via `MCD_RELAY_CONFIG`) plus `MCD_RELAY_*` env overrides; see
[`docs/app/RELAY.md`](../docs/app/RELAY.md) Section 12 and the example below.
Secrets come from the environment; invalid config fails fast at startup.

```toml
[api]
grpc_endpoint = "api:50051"
credential = "<relay shared secret>"   # prefer the MCD_RELAY_API_CREDENTIAL env var
[api.tls]
ca_file = "/etc/mcsd/api-ca.pem"       # or insecure = true for a local dev dial

[game]
listen = ":25565"
status_cache_seconds = 5
max_conns_per_ip = 32
joins_per_ip_per_second = 10

[tunnel]
listen = ":25665"
public_endpoint = "relay.example.com:25665"
[tunnel.tls]
cert_file = "/etc/mcsd/tunnel-cert.pem"
key_file = "/etc/mcsd/tunnel-key.pem"

[log]
level = "info"
format = "json"
```
