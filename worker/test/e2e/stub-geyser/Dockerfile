# Build a RakNet ping/pong stub to test Bedrock datagram routing without Geyser downloads.
FROM golang:1.27.0 AS builder

WORKDIR /src
COPY go.mod main.go ./
RUN CGO_ENABLED=0 GOOS=linux go build -o /out/stub-geyser .

FROM alpine@sha256:4bcff63911fcb4448bd4fdacec207030997caf25e9bea4045fa6c8c44de311d1

COPY --from=builder /out/stub-geyser /usr/local/bin/stub-geyser
ENTRYPOINT ["/usr/local/bin/stub-geyser"]
