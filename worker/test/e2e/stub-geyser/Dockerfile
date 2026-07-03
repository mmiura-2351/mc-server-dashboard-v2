# Stub "Geyser" image for the Bedrock relay e2e (epic #1540, issue #1547): a
# minimal RakNet Unconnected Ping/Pong responder on :19132/udp standing in for
# Geyser inside a Minecraft server container (worker/test/e2e/stub-geyser/main.go
# doc comment has the wire-format detail). It needs no JRE, no JAR, and no
# Modrinth/GeyserMC download -- real Geyser+Floodgate behavior was already
# validated live (epic #1540 issue #1542).
#
# Multi-stage: build the static binary with the pinned Go toolchain (matching
# worker/Dockerfile's builder pin), then ship it alone on the sibling
# container-driver e2e stub's already-vetted digest-pinned Alpine base
# (worker/test/e2e/stub/Dockerfile).
FROM golang:1.26 AS builder

WORKDIR /src
COPY go.mod main.go ./
RUN CGO_ENABLED=0 GOOS=linux go build -o /out/stub-geyser .

FROM alpine@sha256:4bcff63911fcb4448bd4fdacec207030997caf25e9bea4045fa6c8c44de311d1

COPY --from=builder /out/stub-geyser /usr/local/bin/stub-geyser
ENTRYPOINT ["/usr/local/bin/stub-geyser"]
