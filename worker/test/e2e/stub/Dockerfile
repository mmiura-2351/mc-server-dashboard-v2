# Stub "MC server" image for the container-driver e2e scenarios (issues #234,
# #326). The container driver runs `java <args>` as the container command
# (containerdriver.containerCmd); this image provides a `java` shim on PATH that
# branches on its argv (see worker/test/e2e/stub/java):
#
#   - A Forge SUPERVISED INSTALL invocation (`java -jar server.jar --installServer`)
#     CREATES the launch artifact the driver's post-install re-plan globs for —
#     libraries/net/minecraftforge/forge/<v>/unix_args.txt — under the bind-mounted
#     working dir and exits 0, standing in for a real forge installer without a JRE
#     or any download.
#   - Any OTHER invocation (a plain JAR launch, or a Forge args-file launch) blocks
#     until SIGTERM, so the container stays in the running state a real Minecraft
#     server would — a long-lived container the restart/stop paths can stop and
#     recreate. SIGTERM ends it promptly so `docker stop` is clean.
#
# It needs no JRE, no JAR, and no network listener.
FROM alpine@sha256:4bcff63911fcb4448bd4fdacec207030997caf25e9bea4045fa6c8c44de311d1

# The shim runs as the container's PID 1; the blocking branch traps SIGTERM so
# `docker stop` ends it without waiting out the SIGKILL grace period (PID 1
# receives only signals it traps). The install branch writes the version-stamped
# unix_args.txt the driver's post-install re-plan globs for, then exits 0; the
# working dir is bind-mounted and set as the container working dir (/data), so the
# relative artifact lands in the working set the harness inspects on the host.
COPY java /usr/local/bin/java
RUN chmod +x /usr/local/bin/java

ENTRYPOINT []
