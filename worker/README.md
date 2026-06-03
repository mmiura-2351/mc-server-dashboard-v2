# worker/

The Go execution agent of mc-server-dashboard. It runs Minecraft server
processes on a host and reports observed state to the authoritative `api/`
service over the gRPC control plane. See
[`docs/app/ARCHITECTURE.md`](../docs/app/ARCHITECTURE.md) for the system design;
this README covers only how to build, test, and lint the module.

This is currently a skeleton: the layout and toolchain are in place, but no
domain logic, drivers, or control-plane client exist yet.

## Layout

Hexagonal layering (ARCHITECTURE.md Section 2) applied idiomatically to Go:

```
worker/
├── cmd/worker/            # edge / wiring: process entry point (stub)
└── internal/
    ├── domain/            # pure core: entities, value objects, Ports
    ├── application/       # use cases, depending only on domain
    └── adapters/          # concrete Port implementations (drivers, clients)
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
