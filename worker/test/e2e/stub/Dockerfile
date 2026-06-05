# Stub "MC server" image for the container-driver e2e restart scenario
# (issue #234). The container driver runs `java -jar <jar> nogui` as the
# container command (containerdriver.serverCmd); this image provides a `java`
# shim on PATH that ignores its arguments and blocks forever, so the container
# stays in the running state the way a real Minecraft server would — without a
# JRE, a JAR, or any network listener. It stands in for the MC process exactly
# as the harness needs: a long-lived container the restart path can stop and
# recreate. SIGTERM ends the sleep promptly so `docker stop` is clean.
FROM alpine@sha256:4bcff63911fcb4448bd4fdacec207030997caf25e9bea4045fa6c8c44de311d1

# A `java` that swallows its args and waits for a signal. It runs as the
# container's PID 1, so it must install an explicit SIGTERM handler — PID 1
# receives only signals it traps — for `docker stop` to end it promptly instead
# of waiting out the SIGKILL grace period. `wait` lets the trap fire between
# sleeps; the loop keeps the process alive like a real server.
RUN printf '#!/bin/sh\ntrap "exit 0" TERM INT\nwhile true; do sleep 1 & wait $!; done\n' \
        > /usr/local/bin/java \
    && chmod +x /usr/local/bin/java

ENTRYPOINT []
