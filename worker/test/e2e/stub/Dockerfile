# Provide a Java shim that creates Forge install artifacts or blocks until SIGTERM.
# The harness exercises container lifecycle without a JVM or downloads.
FROM alpine@sha256:4bcff63911fcb4448bd4fdacec207030997caf25e9bea4045fa6c8c44de311d1

# Run the shim as PID 1; its SIGTERM trap permits graceful container cleanup.
COPY java /usr/local/bin/java
RUN chmod +x /usr/local/bin/java

ENTRYPOINT []
