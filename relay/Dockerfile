# relay/ container image (epic #659). Multi-stage:
#   1. builder — compile a static (CGO disabled) binary with the Go toolchain.
#   2. runtime — a slim base with CA certificates only.
#
# The relay is a pure TCP/gRPC service (game listener, TLS tunnel listener, API
# client); no JRE or Docker socket. Tags are pinned, no `latest`
# (docs/dev/DEPENDENCIES.md Section 1).

# ---------------------------------------------------------------------------
# Builder: compile a static binary.
# ---------------------------------------------------------------------------
FROM golang:1.26.5 AS builder

WORKDIR /src

# Resolve modules first (cached across source-only changes).
COPY go.mod go.sum ./
RUN go mod download

COPY . .

# Version injected at build time; defaults to the dev placeholder.
ARG VERSION=0.0.0-dev

# CGO disabled -> a static binary that runs on the slim runtime base.
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags "-X main.version=${VERSION}" -o /out/relay ./cmd/relay

# ---------------------------------------------------------------------------
# Runtime: slim base with CA certificates. Needed when the relay dials the API
# over TLS and when its tunnel certificate chains to a public CA. The in-compose
# deployment dials the API in plaintext, so these are unused there; they are
# provisioned for the cross-host path (docs/dev/DEPLOYMENT.md).
# ---------------------------------------------------------------------------
FROM debian:bookworm-20260623-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /out/relay /usr/local/bin/relay

# Run as an unprivileged user. The relay only does TCP/gRPC networking on
# ports > 1024 — no Docker socket or privileged resources needed.
RUN useradd --no-create-home --uid 10001 --shell /usr/sbin/nologin app
USER app

ENTRYPOINT ["/usr/local/bin/relay"]
