# worker/ container image (issue #189). Multi-stage:
#   1. builder — compile a static (CGO disabled) binary with the Go toolchain.
#   2. runtime — a slim base with CA certificates only.
#
# This deployment runs the worker with the container driver only (it creates
# sibling Minecraft containers via the Docker socket), so no JRE ships here — the
# JRE lives in the per-server images from driver.container.images. Tags are
# pinned, no `latest` (docs/dev/DEPENDENCIES.md Section 1).

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
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags "-X main.version=${VERSION}" -o /out/worker ./cmd/worker

# ---------------------------------------------------------------------------
# Runtime: slim base with CA certificates. The in-compose deployment dials the
# API in plaintext, so these are unused there; they are provisioned for the
# cross-host path, where the worker verifies the API's TLS cert (see
# docs/dev/DEPLOYMENT.md).
# ---------------------------------------------------------------------------
FROM debian:bookworm-20260713-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /out/worker /usr/local/bin/worker

# No USER directive: the worker runs as root so it can read the mounted Docker
# socket unconditionally. A non-root user with group_add is a possible
# hardening step (see issue #1978).
ENTRYPOINT ["/usr/local/bin/worker"]
