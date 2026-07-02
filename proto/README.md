# proto

The shared protobuf / gRPC **control-plane** contract between `api/` and
`worker/`, managed as a [buf](https://buf.build) module. The contract lives
here; the generated stubs are **checked in** under each consumer (see
[Regeneration](#regeneration)). For the contract reference (stream lifecycle,
message flow, and the requirement mapping) see
[`../docs/app/CONTROL_PLANE.md`](../docs/app/CONTROL_PLANE.md).

## Layout

```
proto/
├── buf.yaml                                    # buf module: lint + breaking config
├── buf.gen.yaml                                # code generation (Go via buf)
├── mcsd/controlplane/v1/control_plane.proto    # the WorkerService bidi-stream contract
├── mcsd/relay/v1/relay.proto                   # the RelayService relay-to-API contract
└── mcsd/bedrocktunnel/v1/bedrock_tunnel.proto   # Worker<->relay Bedrock tunnel handshake
                                                  # (Go only, no gRPC service -- see the file
                                                  # header and docs/app/BEDROCK_TUNNEL.md)
```

Generated stubs (do not edit by hand; regenerate with `make proto-gen`):

```
worker/internal/controlplane/mcsd/controlplane/v1/   # Go: *.pb.go, *_grpc.pb.go
worker/internal/controlplane/mcsd/relay/v1/          # Go: *.pb.go, *_grpc.pb.go
api/src/mcsd/controlplane/v1/                        # Python: *_pb2.py(i), *_pb2_grpc.py(i)
api/src/mcsd/relay/v1/                               # Python: *_pb2.py(i), *_pb2_grpc.py(i)
```

The proto package is `mcsd.controlplane.v1`. The version segment is part of the
package and the directory path, so a future incompatible revision is a new
`v2` package alongside this one.

## buf

This module is developed against buf `1.70.0`.

Install a pinned buf into a local bin (no system install required):

```
BUF_VERSION=1.70.0
mkdir -p "$HOME/.local/bin"
curl -sSL \
  "https://github.com/bufbuild/buf/releases/download/v${BUF_VERSION}/buf-$(uname -s)-$(uname -m)" \
  -o "$HOME/.local/bin/buf"
chmod +x "$HOME/.local/bin/buf"
```

Ensure `$HOME/.local/bin` is on your `PATH`, then verify:

```
buf --version   # -> 1.70.0
```

## Commands

Run from this directory (`proto/`):

```
buf lint        # enforce the STANDARD rule set (must pass)
buf build       # compile the module (sanity check)
buf format -w   # auto-format the .proto files
```

### Breaking-change detection

`buf breaking` compares the working tree's contract against the baseline on
`origin/main` and fails on any backwards-incompatible change (those drive a
MAJOR version bump per [`../docs/dev/RELEASING.md`](../docs/dev/RELEASING.md)).
Run it locally from the repo root before pushing:

```
make proto-breaking   # needs the origin/main tracking ref updated (git fetch origin)
```

(`git fetch origin` updates the `origin/main` tracking ref the Make target
reads; `git fetch origin main` only updates `FETCH_HEAD`.)

It is **not** part of `make check`: `check` is meant to be fast and depend only
on local state, whereas this needs network/git refs to resolve the baseline.
The enforced gate is the proto CI workflow (`.github/workflows/proto.yml`),
which runs `make proto-breaking` on every PR that touches the contract.

#### Intentional breaking changes

A backwards-incompatible contract change is sometimes intended — it drives a
MAJOR (during `0.x`: MINOR) version bump per
[`../docs/dev/RELEASING.md`](../docs/dev/RELEASING.md) Section 1. To let such a
PR land with green CI, apply the `breaking` label to the PR. The same label
that groups the change under **Breaking Changes** in the release notes also
skips the buf-breaking gate (the step logs a loud notice saying why). Because
label changes don't re-trigger PR workflows by default, the proto workflow
listens for `labeled`/`unlabeled` events, so applying the label re-runs the
gate — which then skips.

## Regeneration

Stubs are **checked in** rather than generated on demand, so `worker/` and
`api/` build and import without a proto toolchain present, and CI for each
consumer needs no codegen step. Freshness is enforced by a drift gate: CI
(`.github/workflows/proto.yml`) and `make check` run `make proto-check`, which
regenerates and fails if the result differs from what is committed.

Regenerate both languages with one command from the repo root:

```
make proto-gen
```

This drives two pinned generators:

- **Go** via `buf generate` (`buf.gen.yaml`), using local plugins installed
  into the gitignored `worker/.bin/`:
  - `protoc-gen-go` **v1.36.11**
  - `protoc-gen-go-grpc` **v1.6.2**
- **Python** via `grpc_tools.protoc` + `mypy-protobuf` from the `api/` dev
  group (resolved from `api/uv.lock`):
  - `grpcio-tools` **1.80.0** (bundles protoc + the python/grpc generators)
  - `mypy-protobuf` **5.1.0** (`.pyi` type stubs)

The split exists because the gRPC Python generator ships only inside
`grpcio-tools` as a protoc frontend (not a `protoc-gen-*` plugin buf can
invoke). Versions are pinned outside the 7-day supply-chain cooldown per
[`../docs/dev/DEPENDENCIES.md`](../docs/dev/DEPENDENCIES.md); bump them in the
Makefile (Go) and `api/pyproject.toml` (Python).

Generated Go and Python are excluded from the strict lint/type gates
(golangci-lint skips `DO NOT EDIT` files automatically; ruff/mypy exclude
`api/src/mcsd` via `api/pyproject.toml`).

## Conventions

- proto3, packages `mcsd.controlplane.v1`, `mcsd.relay.v1`, and
  `mcsd.bedrocktunnel.v1`. The last is Worker<->relay only (not served over
  gRPC, and not consumed by `api/`), so `make proto-gen` generates it for Go
  only -- both the primary template (into `worker/`) and the relay-scoped
  template (into `relay/internal/genproto/`, see below); the Python leg's
  `grpc_tools.protoc` invocation names its two files explicitly and does not
  include it.
- Lint uses the buf `STANDARD` rule set. The single exception is the
  request/response naming rules for the `Session` RPC, whose request and
  response are the multiplexing `WorkerMessage` / `ApiMessage` envelopes rather
  than RPC-named messages (see the comment in `buf.yaml`).
- Well-known types are used for time: `google.protobuf.Timestamp` and
  `google.protobuf.Duration`.
