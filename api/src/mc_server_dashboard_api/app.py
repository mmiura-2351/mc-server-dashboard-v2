"""FastAPI application factory — the process edge / wiring entry point.

Loads configuration, installs structured logging and the correlation-ID
middleware, builds the async engine, and mounts the routers. This is the only
place (with :mod:`dependencies`) that reads configuration and constructs
adapters (ARCHITECTURE.md Section 2.1, CONFIGURATION.md Section 1).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import logging
import os
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI

from mc_server_dashboard_api.audit.api import audit
from mc_server_dashboard_api.community.api import (
    admin_communities,
    communities,
    grants,
    me,
    members,
    roles,
)
from mc_server_dashboard_api.config import Settings, load_settings
from mc_server_dashboard_api.core.adapters.database import (
    create_engine,
    create_session_factory,
)
from mc_server_dashboard_api.core.adapters.metrics_middleware import metrics_middleware
from mc_server_dashboard_api.core.api import health, metrics, readiness
from mc_server_dashboard_api.dataplane.api import transfers
from mc_server_dashboard_api.dependencies import (
    build_brute_force_config,
    build_registration_config,
)
from mc_server_dashboard_api.fleet.adapters.clock import SystemClock as FleetSystemClock
from mc_server_dashboard_api.fleet.adapters.control_plane import (
    ControlPlaneState,
    GrpcControlPlane,
)
from mc_server_dashboard_api.fleet.adapters.grpc_server import make_grpc_server
from mc_server_dashboard_api.fleet.adapters.real_time_events import (
    InProcessRealTimeEvents,
)
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.api import events as server_events
from mc_server_dashboard_api.fleet.api import workers
from mc_server_dashboard_api.http_problem import install_problem_handlers
from mc_server_dashboard_api.identity.adapters.clock import (
    SystemClock as IdentitySystemClock,
)
from mc_server_dashboard_api.identity.adapters.login_attempt_store import (
    SqlAlchemyLoginAttemptStore,
)
from mc_server_dashboard_api.identity.adapters.prune_login_attempts_loop import (
    run_prune_login_attempts_loop,
)
from mc_server_dashboard_api.identity.api import admin_users, auth, users
from mc_server_dashboard_api.identity.application.prune_login_attempts import (
    PruneLoginAttempts,
)
from mc_server_dashboard_api.logging import configure_logging
from mc_server_dashboard_api.middleware import correlation_id_middleware
from mc_server_dashboard_api.servers.adapters.backup_loop import run_backup_loop
from mc_server_dashboard_api.servers.adapters.backup_store import (
    StorageBackupStoreAdapter,
)
from mc_server_dashboard_api.servers.adapters.clock import (
    SystemClock as ServersSystemClock,
)
from mc_server_dashboard_api.servers.adapters.control_plane import (
    FleetControlPlaneAdapter,
)
from mc_server_dashboard_api.servers.adapters.jar_provisioner import (
    CatalogJarProvisioner,
)
from mc_server_dashboard_api.servers.adapters.reconciler_loop import (
    run_reconciler_loop,
)
from mc_server_dashboard_api.servers.adapters.server_state_sink import (
    ServersServerStateSink,
)
from mc_server_dashboard_api.servers.adapters.snapshot_loop import run_snapshot_loop
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.api import backups as server_backups
from mc_server_dashboard_api.servers.api import files as server_files
from mc_server_dashboard_api.servers.api import groups as server_groups
from mc_server_dashboard_api.servers.api import ports as server_ports
from mc_server_dashboard_api.servers.api import servers
from mc_server_dashboard_api.servers.application.backup_scheduler import (
    RunBackupScheduleTick,
)
from mc_server_dashboard_api.servers.application.backups import CreateBackup
from mc_server_dashboard_api.servers.application.lifecycle import (
    StartServer,
    StopServer,
)
from mc_server_dashboard_api.servers.application.reconciler import RunReconcilerTick
from mc_server_dashboard_api.servers.application.snapshot_scheduler import (
    RunSnapshotCadenceTick,
    SnapshotServer,
)
from mc_server_dashboard_api.servers.application.startup_reset import (
    ResetUnverifiableObservedStates,
)
from mc_server_dashboard_api.servers.application.warn_missing_ports import (
    WarnLegacyMissingPorts,
)
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.adapters.object_client import (
    make_s3_client_factory,
)
from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from mc_server_dashboard_api.versions.adapters.clock import (
    SystemClock as VersionsSystemClock,
)
from mc_server_dashboard_api.versions.adapters.composite import CompositeCatalog
from mc_server_dashboard_api.versions.adapters.fabric import FabricCatalog
from mc_server_dashboard_api.versions.adapters.forge import ForgeCatalog
from mc_server_dashboard_api.versions.adapters.http_fetcher import HttpxJsonFetcher
from mc_server_dashboard_api.versions.adapters.http_jar_fetcher import HttpxJarFetcher
from mc_server_dashboard_api.versions.adapters.jar_gc_loop import run_jar_gc_loop
from mc_server_dashboard_api.versions.adapters.paper import PaperCatalog
from mc_server_dashboard_api.versions.adapters.retry_cache import RetryCachingFetcher
from mc_server_dashboard_api.versions.adapters.server_jar_references import (
    ServerJarReferences,
)
from mc_server_dashboard_api.versions.adapters.storage_jar_pool import StorageJarPool
from mc_server_dashboard_api.versions.adapters.vanilla import VanillaCatalog
from mc_server_dashboard_api.versions.api import versions as versions_api
from mc_server_dashboard_api.versions.application.ensure_jar import EnsureJar
from mc_server_dashboard_api.versions.application.jar_gc import RunJarPoolGc
from mc_server_dashboard_api.versions.domain.catalog import VersionCatalog
from mc_server_dashboard_api.versions.domain.value_objects import (
    ServerType as CatalogServerType,
)
from mc_server_dashboard_api.webui import mount_webui

# Optional TOML config file location, overridable per deployment.
_CONFIG_FILE_ENV = "MCD_API_CONFIG_FILE"


def _resolve_config_file() -> Path | None:
    raw = os.environ.get(_CONFIG_FILE_ENV)
    return Path(raw) if raw else None


def _build_storage(settings: Settings) -> FsStorage | ObjectStorage:
    """Bind the :class:`Storage` Port to the config-selected adapter.

    Backend selection follows STORAGE.md Section 7. ``fs`` is the M1 default and
    ``remote-fs`` reuses it via a POSIX mount (Section 7.2). ``object`` binds the
    S3-compatible adapter (Section 7.3); its endpoint/bucket/credentials are
    required and a missing one fails fast at boot rather than starting with an
    unusable store (CONFIGURATION.md Section 3).
    """

    if settings.storage.backend in ("fs", "remote-fs"):
        return FsStorage(
            Path(settings.storage.fs.root),
            version_retention=settings.storage.version_retention,
        )
    obj = settings.storage.object
    missing = [
        name
        for name in ("endpoint", "bucket", "access_key", "secret_key")
        if getattr(obj, name) is None
    ]
    if missing:
        raise ValueError(
            "storage.backend 'object' requires "
            + ", ".join(f"storage.object.{name}" for name in missing)
        )
    assert obj.endpoint is not None
    assert obj.bucket is not None
    assert obj.access_key is not None
    assert obj.secret_key is not None
    return ObjectStorage(
        make_s3_client_factory(
            endpoint=obj.endpoint,
            bucket=obj.bucket,
            access_key=obj.access_key,
            secret_key=obj.secret_key,
        ),
        version_retention=settings.storage.version_retention,
    )


# Per-type source-host prefixes for the manual refresh (issue #286). The shared
# manifest cache is a source-down fallback keyed by URL; a per-type refresh drops
# the cached last-good entries whose URL starts with that type's source host.
# Vanilla also fetches per-version detail JSON from a different host, which a
# host-prefix match does not cover — the per-type refresh targets the type's source
# manifest (the listing), and an all-types refresh clears the whole cache. Forge is
# the same shape: the prefix targets the maven-metadata listing host; the
# promotions feed (files.minecraftforge.net) and the per-build .sha1 are cleared by
# an all-types refresh (issue #307).
_CATALOG_SOURCE_PREFIXES: dict[CatalogServerType, str] = {
    CatalogServerType.VANILLA: "https://launchermeta.mojang.com",
    CatalogServerType.PAPER: "https://api.papermc.io",
    CatalogServerType.FABRIC: "https://meta.fabricmc.net",
    CatalogServerType.FORGE: "https://maven.minecraftforge.net",
}


def _build_version_catalog() -> tuple[VersionCatalog, RetryCachingFetcher]:
    """Build the process-wide :class:`VersionCatalog` (ARCHITECTURE.md Section 7.3).

    One httpx fetcher wrapped in the retry + in-process TTL-cache fallback
    (FR-VER-2), shared by the vanilla (Mojang), Paper (PaperMC), Fabric
    (meta.fabricmc.net), and Forge (Forge Maven) catalogs so the last-good
    manifest cache is process-wide. A
    jittered, asyncio-backed backoff spaces retries; the cache TTL keeps the catalog
    serving through a transient source outage. The cache lives for the process
    lifetime, so this is built once and stored on app state. The fetcher is returned
    alongside the catalog so the manual-refresh seam (issue #286) can invalidate it.
    """

    fetcher = RetryCachingFetcher(
        inner=HttpxJsonFetcher(),
        sleep=asyncio.sleep,
        jitter=lambda: random.uniform(0.5, 1.5),
    )
    catalog = CompositeCatalog(
        by_type={
            CatalogServerType.VANILLA: VanillaCatalog(fetcher=fetcher),
            CatalogServerType.PAPER: PaperCatalog(fetcher=fetcher),
            CatalogServerType.FABRIC: FabricCatalog(fetcher=fetcher),
            CatalogServerType.FORGE: ForgeCatalog(fetcher=fetcher),
        }
    )
    return catalog, fetcher


def _validate_control_tls(settings: Settings) -> None:
    """Enforce the control-channel required-unless-insecure TLS rule (NFR-SEC-1).

    The gRPC listener serves over TLS when ``control.tls.cert_file`` and
    ``control.tls.key_file`` are both set. ``control.tls.insecure=true`` opts in
    to a plaintext listener (local/dev only). Exactly one posture must be
    chosen: with neither the cert/key pair nor ``insecure=true`` set, or with
    only one of cert/key set, startup fails fast. Mirrors the Worker's
    ``api.tls`` precedent (CONFIGURATION.md Section 6.1).
    """

    tls = settings.control.tls
    has_cert, has_key = tls.cert_file is not None, tls.key_file is not None
    if has_cert != has_key:
        raise ValueError(
            "control.tls.cert_file and control.tls.key_file must be set together"
        )
    if not has_cert and not tls.insecure:
        raise ValueError(
            "control.tls.cert_file and control.tls.key_file are required "
            "(or set control.tls.insecure=true for a plaintext dev listener)"
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = load_settings(_resolve_config_file())

    # The token signing key is a required secret whenever the auth endpoints are
    # mounted (CONFIGURATION.md Section 5.3); fail fast at boot rather than
    # starting unable to issue or verify tokens (Section 3).
    if settings.auth.token.signing_key is None:
        raise ValueError("auth.token.signing_key is required to mount auth endpoints")

    # The Worker credential is a required secret whenever the control plane is
    # enabled (CONFIGURATION.md Section 5.1); fail fast rather than starting a
    # control-plane server that would admit any Worker (Section 3, NFR-SEC-1).
    if settings.control.enabled and not settings.control.worker_credential:
        raise ValueError(
            "control.worker_credential is required when control.enabled is true"
        )

    # The control channel must be authenticated AND encrypted (NFR-SEC-1). The
    # gRPC listener serves over TLS when control.tls.cert_file/key_file are both
    # set; control.tls.insecure=true opts in to a plaintext listener for
    # local/dev only. Require one or the other — fail fast rather than silently
    # binding plaintext. Mirrors the Worker's api.tls required-unless-insecure
    # rule (CONFIGURATION.md Section 6.1).
    if settings.control.enabled:
        _validate_control_tls(settings)

    # Bind the Storage Port to the config-selected backend now, so an unsupported
    # backend (or, by construction, a missing fs root) fails fast at boot rather
    # than on first use (CONFIGURATION.md Section 3; STORAGE.md Section 7).
    storage = _build_storage(settings)

    # Build the process-wide version catalog now so its in-process manifest cache
    # is shared across requests (FR-VER-2). No external secret is required, so it
    # cannot fail at boot; it is stored on app state below.
    version_catalog, version_fetcher = _build_version_catalog()

    configure_logging(settings.log.level, settings.log.format)

    heartbeat_timeout = dt.timedelta(seconds=settings.control.heartbeat_timeout_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database.url)
        app.state.engine = engine
        app.state.settings = settings
        app.state.storage = storage
        app.state.version_catalog = version_catalog
        # The catalog's manifest-cache invalidator + per-type source prefixes, for
        # the platform-admin manual refresh (issue #286).
        app.state.version_cache_invalidator = version_fetcher
        app.state.version_source_prefixes = _CATALOG_SOURCE_PREFIXES
        # Readiness flag for /readyz (issue #282): the control-plane gRPC server
        # has not started yet; flipped True once start() returns below.
        app.state.grpc_started = False
        # Crash-recovery orphan sweep on startup (STORAGE.md Section 4.3, epic #8
        # note): reclaim any staging dir/prefix or superseded snapshot left by a
        # crash before this process serves. Idempotent and keyed off the live
        # pointer, so it never reclaims authoritative data. The fs sweep is
        # blocking I/O run off the event loop; the object sweep is async (S3 calls).
        if inspect.iscoroutinefunction(storage.sweep):
            await storage.sweep()
        else:
            await asyncio.to_thread(storage.sweep)
        registry = InMemoryWorkerRegistry(
            clock=FleetSystemClock(), heartbeat_timeout=heartbeat_timeout
        )
        app.state.worker_registry = registry
        # Shared control-plane command-routing state: the servicer registers
        # sessions and resolves results on it; the GrpcControlPlane adapter
        # dispatches through it. The lifecycle use cases reach it via the adapter.
        control_plane_state = ControlPlaneState()
        app.state.control_plane = GrpcControlPlane(
            control_plane_state,
            timeout_seconds=settings.control.command_timeout_seconds,
        )
        # The control-plane event path writes back observed server state through
        # this sink (its own session per call; the servicer has no request UoW).
        state_sink = ServersServerStateSink(
            create_session_factory(engine), clock=ServersSystemClock()
        )
        # Process-wide in-process real-time event bus (FR-MON-1..4): the gRPC
        # servicer publishes status/log/metrics events onto it; the WebSocket
        # endpoint subscribes per server. Best-effort and decoupled from REST —
        # if it is empty, clients simply miss live events (graceful degradation).
        real_time_events = InProcessRealTimeEvents()
        app.state.real_time_events = real_time_events
        logging.getLogger(__name__).info(
            "api starting", extra={"config": settings.masked_dump()}
        )
        grpc_server = None
        snapshot_task: asyncio.Task[None] | None = None
        backup_task: asyncio.Task[None] | None = None
        reconciler_task: asyncio.Task[None] | None = None
        jar_gc_task: asyncio.Task[None] | None = None
        # Periodic login_attempt prune (SECURITY.md Section 3). Ungated on the
        # control plane: unlike the snapshot/backup loops it drives only the
        # database, so it must run on every API process to keep the append-only
        # table bounded against a failures-only attack — which never triggers the
        # on-success prune in the login use case (FR-AUTH-4).
        prune_attempts_task = asyncio.create_task(
            run_prune_login_attempts_loop(
                PruneLoginAttempts(
                    attempts=SqlAlchemyLoginAttemptStore(
                        create_session_factory(engine)
                    ),
                    brute_force=build_brute_force_config(settings.auth.brute_force),
                    clock=IdentitySystemClock(),
                    registration=build_registration_config(settings.auth.registration),
                ),
                tick_seconds=settings.auth.brute_force.prune_interval_seconds,
            )
        )
        logging.getLogger(__name__).info("login_attempt prune loop started")
        if settings.control.enabled:
            # Run the control-plane gRPC server as a lifespan task on the same
            # asyncio event loop as FastAPI (grpc.aio): one process, one loop,
            # the registry shared in-memory between the two surfaces.
            assert settings.control.worker_credential is not None
            grpc_server = make_grpc_server(
                registry=registry,
                clock=FleetSystemClock(),
                worker_credential=settings.control.worker_credential,
                heartbeat_timeout=heartbeat_timeout,
                control_plane=control_plane_state,
                state_sink=state_sink,
                real_time_events=real_time_events,
                host=settings.server.host,
                port=settings.server.grpc_port,
                cert_file=settings.control.tls.cert_file,
                key_file=settings.control.tls.key_file,
                insecure=settings.control.tls.insecure,
            )
            await grpc_server.start()
            app.state.grpc_server = grpc_server
            app.state.grpc_started = True
            logging.getLogger(__name__).info(
                "control-plane gRPC server started",
                extra={"port": settings.server.grpc_port},
            )
            # Run the periodic snapshot scheduler as a lifespan task (FR-DATA-7),
            # alongside the gRPC server: it dispatches snapshot triggers to the
            # running servers via the same registry + control-plane state. Only
            # started when the control plane is enabled — with no Worker channel
            # there is nothing to snapshot. The tick resolution is the snapshot
            # floor (CONFIGURATION.md Section 5.4).
            scheduler = RunSnapshotCadenceTick(
                uow=ServersUnitOfWork(create_session_factory(engine)),
                control_plane=FleetControlPlaneAdapter(
                    registry=registry,
                    control_plane=app.state.control_plane,
                    data_plane_base_url=settings.server.public_base_url,
                    worker_credential=settings.control.worker_credential,
                ),
                clock=ServersSystemClock(),
                default_interval_seconds=settings.snapshot.default_interval_seconds,
                min_interval_seconds=settings.snapshot.min_interval_seconds,
            )
            snapshot_task = asyncio.create_task(
                run_snapshot_loop(
                    scheduler,
                    tick_seconds=settings.snapshot.min_interval_seconds,
                )
            )
            logging.getLogger(__name__).info("snapshot scheduler started")
            # Run the periodic scheduled-backup scheduler as a lifespan task
            # (FR-BAK-3), alongside the snapshot scheduler. Gated on the control
            # plane like the snapshot loop: the running-server backup path needs a
            # worker (save-all -> snapshot), and an at-rest server cannot have been
            # started without the control plane, so a backup loop with no control
            # plane has nothing to act on. The CreateBackup it drives reuses the
            # same control-plane adapter + Storage backup seam as the HTTP path.
            backup_control_plane = FleetControlPlaneAdapter(
                registry=registry,
                control_plane=app.state.control_plane,
                data_plane_base_url=settings.server.public_base_url,
                worker_credential=settings.control.worker_credential,
            )
            backup_scheduler = RunBackupScheduleTick(
                uow=ServersUnitOfWork(create_session_factory(engine)),
                create_backup=CreateBackup(
                    uow=ServersUnitOfWork(create_session_factory(engine)),
                    control_plane=backup_control_plane,
                    backup_store=StorageBackupStoreAdapter(storage=storage),
                    snapshot_server=SnapshotServer(
                        uow=ServersUnitOfWork(create_session_factory(engine)),
                        control_plane=backup_control_plane,
                    ),
                    clock=ServersSystemClock(),
                ),
                clock=ServersSystemClock(),
            )
            backup_task = asyncio.create_task(
                run_backup_loop(
                    backup_scheduler,
                    tick_seconds=settings.backup.schedule_tick_seconds,
                )
            )
            logging.getLogger(__name__).info("backup scheduler started")
            # Run the periodic divergence reconciler as a lifespan task (issue
            # #101), alongside the snapshot/backup schedulers. Gated on the control
            # plane like them: it re-dispatches durable-but-unsent lifecycle intent
            # (a start/stop committed before a crash, or a compensation-failure
            # orphan), which needs a Worker channel to act on. It reuses the same
            # StartServer/StopServer use cases and control-plane adapter as the HTTP
            # path so the re-dispatch is identical to a normal lifecycle command.
            reconciler_control_plane = FleetControlPlaneAdapter(
                registry=registry,
                control_plane=app.state.control_plane,
                data_plane_base_url=settings.server.public_base_url,
                worker_credential=settings.control.worker_credential,
            )
            reconciler = RunReconcilerTick(
                uow=ServersUnitOfWork(create_session_factory(engine)),
                start_server=StartServer(
                    uow=ServersUnitOfWork(create_session_factory(engine)),
                    control_plane=reconciler_control_plane,
                    clock=ServersSystemClock(),
                    jar_provisioner=CatalogJarProvisioner(
                        ensure_jar=EnsureJar(
                            catalog=version_catalog,
                            fetcher=HttpxJarFetcher(),
                            pool=StorageJarPool(jars=storage),
                        )
                    ),
                ),
                stop_server=StopServer(
                    uow=ServersUnitOfWork(create_session_factory(engine)),
                    control_plane=reconciler_control_plane,
                    clock=ServersSystemClock(),
                ),
                control_plane=reconciler_control_plane,
                clock=ServersSystemClock(),
                grace_seconds=settings.reconciler.grace_seconds,
                backoff_base_seconds=settings.reconciler.backoff_base_seconds,
                backoff_max_seconds=settings.reconciler.backoff_max_seconds,
            )
            # Invalidate the stale observed-state cache before the reconciler's
            # first tick (issue #224), run from inside the loop as its first
            # action (issue #230). A full-stack restart kills the API before it
            # can observe a worker's heartbeat lapse, so an assigned, in-flight
            # row can persist as observed=running with no live instance — which
            # the reconciler reads as converged. Marking such rows
            # observed=unknown (assignment kept) lets the reconciler converge
            # truthfully once workers re-report. The loop gates ticking on this
            # reset succeeding and retries it on failure, so a DB that is briefly
            # unreachable at startup does not crash the boot (it did when this
            # ran inline in the lifespan body).
            reconciler_reset = ResetUnverifiableObservedStates(
                uow=ServersUnitOfWork(create_session_factory(engine)),
                clock=ServersSystemClock(),
            )
            # Surface legacy NULL-game_port rows on startup (issue #310): such
            # rows predate port tracking (#243) and are invisible to port
            # auto-assignment, so a new server can collide on the host port a
            # legacy server already binds. The WARN lists them so an operator can
            # backfill them (DEPLOYMENT.md Section 6). Run from inside the loop's
            # startup-once section (after the reset) — read-only and failure-
            # tolerant, so it never gates ticking nor crashes the boot.
            reconciler_warn_missing_ports = WarnLegacyMissingPorts(
                uow=ServersUnitOfWork(create_session_factory(engine)),
            )
            reconciler_task = asyncio.create_task(
                run_reconciler_loop(
                    reconciler,
                    reset=reconciler_reset,
                    warn_missing_ports=reconciler_warn_missing_ports,
                    tick_seconds=settings.reconciler.interval_seconds,
                )
            )
            logging.getLogger(__name__).info("divergence reconciler started")
            # Run the periodic reference-counted JAR-pool GC as a lifespan task
            # (D4, issue #293), alongside the other schedulers. Gated on the
            # control plane like them: it reclaims pooled JARs no live server row
            # references, and a deployment with no control plane runs no servers,
            # so the pool has nothing to reclaim. It reuses the same pool seam over
            # Storage's JarStore (list + delete) and the live-reference scan over
            # the servers repository as the manual HTTP trigger.
            jar_gc = RunJarPoolGc(
                pool=StorageJarPool(jars=storage),
                references=ServerJarReferences(
                    uow=ServersUnitOfWork(create_session_factory(engine))
                ),
                clock=VersionsSystemClock(),
            )
            jar_gc_task = asyncio.create_task(
                run_jar_gc_loop(
                    jar_gc,
                    tick_seconds=settings.jar_gc.interval_seconds,
                )
            )
            logging.getLogger(__name__).info("jar-pool GC started")
        try:
            yield
        finally:
            prune_attempts_task.cancel()
            with suppress(asyncio.CancelledError):
                await prune_attempts_task
            if jar_gc_task is not None:
                jar_gc_task.cancel()
                with suppress(asyncio.CancelledError):
                    await jar_gc_task
            if reconciler_task is not None:
                reconciler_task.cancel()
                with suppress(asyncio.CancelledError):
                    await reconciler_task
            if backup_task is not None:
                backup_task.cancel()
                with suppress(asyncio.CancelledError):
                    await backup_task
            if snapshot_task is not None:
                snapshot_task.cancel()
                with suppress(asyncio.CancelledError):
                    await snapshot_task
            if grpc_server is not None:
                await grpc_server.stop(grace=None)
            await engine.dispose()

    app = FastAPI(title="mc-server-dashboard API", lifespan=lifespan)
    # Render every error response as RFC 9457 problem+json (issue #371): one body
    # shape for application errors, framework HTTPExceptions, and 422 validation.
    install_problem_handlers(app)
    # Middleware is applied outermost-last: adding the metrics middleware after the
    # correlation-id one makes it the outer wrapper, so it times the full request
    # handling and labels by route template (issue #282).
    app.middleware("http")(correlation_id_middleware)
    app.middleware("http")(metrics_middleware)
    app.include_router(health.router)
    app.include_router(readiness.router)
    app.include_router(metrics.router)
    app.include_router(users.router)
    # Registered after users.router so the exact ``/users/me`` self-service paths
    # still match before this router's templated ``/users/{user_id}`` paths.
    app.include_router(admin_users.router)
    app.include_router(auth.router)
    app.include_router(communities.router)
    app.include_router(admin_communities.router)
    app.include_router(me.router)
    app.include_router(members.router)
    app.include_router(roles.router)
    app.include_router(grants.router)
    app.include_router(servers.router)
    app.include_router(server_ports.router)
    app.include_router(server_files.router)
    app.include_router(server_backups.router)
    app.include_router(server_groups.router)
    app.include_router(workers.router)
    app.include_router(server_events.router)
    app.include_router(transfers.router)
    app.include_router(versions_api.router)
    app.include_router(audit.router)
    # Serve the built Web UI from the same origin when a dist dir is configured
    # (WEBUI_SPEC 7.7, issue #490). Mounted last so every router and WebSocket
    # endpoint above takes strict precedence; the SPA fallback covers only paths
    # no route matched. Unset (dev + tests) → no mount.
    if settings.webui.dist_dir is not None:
        mount_webui(app, settings.webui.dist_dir)
    return app
