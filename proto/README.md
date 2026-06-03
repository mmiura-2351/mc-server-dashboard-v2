# proto

The shared protobuf / gRPC **control-plane** contract between `api/` and
`worker/`, managed as a [buf](https://buf.build) module. This package contains
the contract only — no generated code and no language wiring (that lands with
issue #22). For the contract reference (stream lifecycle, message flow, and the
requirement mapping) see [`../docs/app/CONTROL_PLANE.md`](../docs/app/CONTROL_PLANE.md).

## Layout

```
proto/
├── buf.yaml                                  # buf module: lint + breaking config
└── mcsd/controlplane/v1/control_plane.proto  # the WorkerService bidi-stream contract
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

`buf breaking` compares against a baseline; wire it to the default branch once a
generation/CI step exists (issue #22).

## Conventions

- proto3, package `mcsd.controlplane.v1`.
- Lint uses the buf `STANDARD` rule set. The single exception is the
  request/response naming rules for the `Session` RPC, whose request and
  response are the multiplexing `WorkerMessage` / `ApiMessage` envelopes rather
  than RPC-named messages (see the comment in `buf.yaml`).
- Well-known types are used for time: `google.protobuf.Timestamp` and
  `google.protobuf.Duration`.
