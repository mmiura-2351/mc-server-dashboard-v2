# The Worker creates sibling Minecraft containers through the Docker socket.
# Java lives in the configured per-server images, not in this image.

FROM golang:1.27.1 AS builder

WORKDIR /src

# Resolve modules first (cached across source-only changes).
COPY go.mod go.sum ./
RUN go mod download

COPY . .

ARG VERSION=0.0.0-dev

RUN CGO_ENABLED=0 GOOS=linux go build -ldflags "-X main.version=${VERSION}" -o /out/worker ./cmd/worker

# CA certificates support TLS verification for cross-host deployments.
FROM debian:bookworm-20260918-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /out/worker /usr/local/bin/worker

# Root permits access to the mounted Docker socket and working-set ownership handoff.
ENTRYPOINT ["/usr/local/bin/worker"]
