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

from fastapi import APIRouter, FastAPI

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
from mc_server_dashboard_api.core.api import health, meta, metrics, readiness
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
from mc_server_dashboard_api.fleet.adapters.relay_server import register_relay_service
from mc_server_dashboard_api.fleet.adapters.relay_state import (
    BedrockTunnelTable,
    JoinTokenTable,
    RelayRegistration,
)
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
from mc_server_dashboard_api.middleware import (
    correlation_id_middleware,
    security_headers_middleware,
    strip_no_content_body_headers_middleware,
)
from mc_server_dashboard_api.servers.adapters.backup_loop import run_backup_loop
from mc_server_dashboard_api.servers.adapters.backup_store import (
    StorageBackupStoreAdapter,
)
from mc_server_dashboard_api.servers.adapters.bedrock_tunnel_sync import (
    BedrockTunnelSyncer,
)
from mc_server_dashboard_api.servers.adapters.clock import (
    SystemClock as ServersSystemClock,
)
from mc_server_dashboard_api.servers.adapters.control_plane import (
    FleetControlPlaneAdapter,
)
from mc_server_dashboard_api.servers.adapters.file_store import (
    StorageFileStoreAdapter,
)
from mc_server_dashboard_api.servers.adapters.game_session_prune_loop import (
    run_game_session_prune_loop,
)
from mc_server_dashboard_api.servers.adapters.jar_provisioner import (
    CatalogJarProvisioner,
)
from mc_server_dashboard_api.servers.adapters.late_snapshot_result_sink import (
    ServersLateSnapshotResultSink,
)
from mc_server_dashboard_api.servers.adapters.lifecycle_lock import PgLifecycleLock
from mc_server_dashboard_api.servers.adapters.plugin_cache_gc_loop import (
    run_plugin_cache_gc_loop,
)
from mc_server_dashboard_api.servers.adapters.plugin_cache_references import (
    PluginCacheReferences,
)
from mc_server_dashboard_api.servers.adapters.plugin_cache_store import (
    ObjectPluginCacheStore,
)
from mc_server_dashboard_api.servers.adapters.reconciler_loop import (
    run_reconciler_loop,
)
from mc_server_dashboard_api.servers.adapters.resource_pack_store import (
    ObjectResourcePackStore,
)
from mc_server_dashboard_api.servers.adapters.server_route_resolver import (
    ServersServerRouteResolver,
)
from mc_server_dashboard_api.servers.adapters.server_state_sink import (
    ServersServerStateSink,
)
from mc_server_dashboard_api.servers.adapters.session_sink import ServersSessionSink
from mc_server_dashboard_api.servers.adapters.snapshot_loop import run_snapshot_loop
from mc_server_dashboard_api.servers.adapters.store_generation import (
    StorageGenerationReader,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.api import backups as server_backups
from mc_server_dashboard_api.servers.api import catalog as server_catalog
from mc_server_dashboard_api.servers.api import files as server_files
from mc_server_dashboard_api.servers.api import groups as server_groups
from mc_server_dashboard_api.servers.api import plugins as server_plugins
from mc_server_dashboard_api.servers.api import ports as server_ports
from mc_server_dashboard_api.servers.api import resource_packs as server_resource_packs
from mc_server_dashboard_api.servers.api import servers
from mc_server_dashboard_api.servers.application.backup_scheduler import (
    RunBackupScheduleTick,
)
from mc_server_dashboard_api.servers.application.backups import CreateBackup
from mc_server_dashboard_api.servers.application.bedrock_sweep import (
    SweepBedrockPorts,
)
from mc_server_dashboard_api.servers.application.game_sessions import PruneGameSessions
from mc_server_dashboard_api.servers.application.lifecycle import (
    StartServer,
    StopServer,
)
from mc_server_dashboard_api.servers.application.plugin_cache_gc import (
    RunPluginCacheGc,
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
from mc_server_dashboard_api.servers.domain.ports import PortRange
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

# Margin added to the larger API transfer budget to form the Worker-side
# transfer deadline advertised in RegisterAck (issue #874). It keeps the Worker
# bound strictly above the API budget so the API-side dispatch timeout always
# fires first and the Worker bound stays a cleanup backstop, never the primary
# deadline that could kill a transfer the API still considers in flight.
_TRANSFER_DEADLINE_MARGIN_SECONDS = 60

# How often the game_session retention prune loop wakes (RELAY.md Section 8, issue
# #957). The window is configured in *days* (relay.session_retention_days), so an
# hourly resolution is far finer than needed while keeping the table bounded.
_SESSION_PRUNE_TICK_SECONDS = 3600.0


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
    # Treat empty/whitespace as missing, not just None: compose interpolates an unset
    # ``${MCD_API_STORAGE__OBJECT__ACCESS_KEY}`` to "" rather than dropping it, so an
    # `is None`-only guard would boot a silently-unauthenticated deployment against
    # SeaweedFS (which accepts blank-credential clients). Fail fast instead (#702).
    missing = [
        name
        for name in ("endpoint", "bucket", "access_key", "secret_key")
        if not (getattr(obj, name) or "").strip()
    ]
    if missing:
        raise ValueError(
            "storage.backend 'object' requires non-empty "
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


def _build_resource_pack_store(
    settings: Settings,
) -> ObjectResourcePackStore | None:
    """Build the :class:`ResourcePackStore` adapter (issue #1176).

    Only available for the ``object`` storage backend; returns ``None`` for
    ``fs``/``remote-fs`` (the fs adapter is not implemented yet). The dependency
    layer raises 503 when the store is ``None``.
    """

    if settings.storage.backend in ("fs", "remote-fs"):
        return None
    obj = settings.storage.object
    assert obj.endpoint is not None
    assert obj.bucket is not None
    assert obj.access_key is not None
    assert obj.secret_key is not None
    return ObjectResourcePackStore(
        make_s3_client_factory(
            endpoint=obj.endpoint,
            bucket=obj.bucket,
            access_key=obj.access_key,
            secret_key=obj.secret_key,
        )
    )


def _build_plugin_cache_store(
    settings: Settings,
) -> ObjectPluginCacheStore | None:
    """Build the content-addressed plugin cache store (issue #1306).

    Only available for the ``object`` storage backend; returns ``None`` for
    ``fs``/``remote-fs`` (no fs adapter yet). The dependency layer raises 503 when
    the store is ``None``.
    """

    if settings.storage.backend in ("fs", "remote-fs"):
        return None
    obj = settings.storage.object
    assert obj.endpoint is not None
    assert obj.bucket is not None
    assert obj.access_key is not None
    assert obj.secret_key is not None
    return ObjectPluginCacheStore(
        make_s3_client_factory(
            endpoint=obj.endpoint,
            bucket=obj.bucket,
            access_key=obj.access_key,
            secret_key=obj.secret_key,
        )
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
    CatalogServerType.PAPER: "https://fill.papermc.io",
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


def _warn_reconciler_grace_floor(settings: Settings) -> None:
    """Warn (not fatal) when the reconciler grace is too small to be safe (#822).

    Duplicate-start safety (issue #774/#812) hinges on the reconciler not racing
    an in-flight original dispatch: it must wait out the longest a started server's
    FIRST dispatch round-trip can still be in flight before it re-dispatches. A
    start's dispatch is hydrate-then-start, so that round-trip is bounded by the
    hydrate budget plus the start command deadline. The grace floor is therefore::

        grace_seconds > hydrate_timeout_seconds + command_timeout_seconds

    (Pre-#822 both phases shared ``command_timeout_seconds``, giving the historical
    ``grace > 2 × command_timeout_seconds``.) Below the floor, a slow first start
    can still be converging on the assigned Worker when the reconciler's orphan path
    re-places it elsewhere and starts a second live instance. This is a warning, not
    a hard failure, because the window only opens on a crash/timeout mid-start and
    operators may knowingly accept it. The stock ``grace_seconds=660`` exceeds the
    stock floor (600 + 30), so no warning fires by default. Operators who lower
    ``grace_seconds`` below the floor are warned.

    The stop-side final-snapshot budget (``snapshot_timeout_seconds``, issue #847)
    also bounds the floor. That budget bounds the SECOND dispatch of a stop (the
    held final snapshot), recovered by the reconciler's stale-stop arm. The arm has
    its own safety constraint, ``grace_seconds > snapshot_timeout_seconds`` (so the
    arm never clears a still-healthy snapshot hold mid-upload — which would reopen
    the #847 race the hold exists to close, now that the timeout path leans on the
    arm). With the stock values the duplicate-start bound (630) dominates the
    snapshot bound (600), but an operator who raises ``snapshot_timeout_seconds``
    above ``hydrate + command`` makes the snapshot bound binding — so the floor is
    ``max(hydrate + command, snapshot_timeout, stop_timeout)`` to enforce all
    constraints.

    The stop command's own worker round-trip budget (``stop_timeout_seconds``,
    issue #930) is the third term. It bounds the FIRST dispatch of a stale stop the
    reconciler replays (``redispatch_stop`` on an observed=running row): the row
    stays diverged while that dispatch is in flight, so grace must exceed it or the
    reconciler re-selects and re-dispatches the same stop before the first settles.
    With the stock 600 it sits under the duplicate-start bound (630), but an
    operator who raises it must keep grace above it.

    ``held_start_grace_seconds`` (issue #999) is the SHORTER grace for a
    ``redispatch_start`` that will skip hydrate (the assigned Worker is connected and
    holds a fresh working set), so it is bounded only by the start COMMAND deadline,
    not the hydrate budget: its floor is ``> command_timeout_seconds``. The full
    ``grace_seconds`` floor above is unchanged — every hydrating start and every
    stop-side action still waits it out, so the #822/#847 safety is intact.
    """

    floor = max(
        settings.control.hydrate_timeout_seconds
        + settings.control.command_timeout_seconds,
        settings.control.snapshot_timeout_seconds,
        settings.control.stop_timeout_seconds,
    )
    if settings.reconciler.grace_seconds <= floor:
        logging.getLogger(__name__).warning(
            "reconciler.grace_seconds (%d) <= max("
            "control.hydrate_timeout_seconds (%d) + "
            "control.command_timeout_seconds (%d), "
            "control.snapshot_timeout_seconds (%d), "
            "control.stop_timeout_seconds (%d)); a slow start re-dispatched by "
            "the reconciler before its first round-trip settles can spawn a "
            "duplicate live instance (issue #822), the stale-stop arm can clear "
            "a still-healthy final-snapshot hold mid-upload (issue #847), or a "
            "stale stop can be re-dispatched before its first round-trip settles "
            "(issue #930). Raise grace_seconds above %d.",
            settings.reconciler.grace_seconds,
            settings.control.hydrate_timeout_seconds,
            settings.control.command_timeout_seconds,
            settings.control.snapshot_timeout_seconds,
            settings.control.stop_timeout_seconds,
            floor,
        )
    # The held-start short grace (issue #999) only covers a command-only
    # redispatch_start (hydrate skipped), so its floor is the start COMMAND deadline,
    # not the hydrate budget. The full grace_seconds floor above is unchanged.
    if (
        settings.reconciler.held_start_grace_seconds
        <= settings.control.command_timeout_seconds
    ):
        logging.getLogger(__name__).warning(
            "reconciler.held_start_grace_seconds (%d) <= "
            "control.command_timeout_seconds (%d); a held-server start re-dispatched "
            "by the reconciler before its command round-trip settles can race the "
            "in-flight start (issue #999). Raise held_start_grace_seconds above %d.",
            settings.reconciler.held_start_grace_seconds,
            settings.control.command_timeout_seconds,
            settings.control.command_timeout_seconds,
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

    # The relay credential and base domain are required whenever the game-ingress
    # relay is enabled (RELAY.md Section 13); fail fast rather than serving a
    # RelayService that would admit any relay (NFR-SEC-1) or building a
    # ``join_hostname`` with no base domain. Mirrors the Worker-credential guard,
    # and ``not <secret>`` treats a blank-collapsed-to-None as missing (#943).
    if settings.relay.enabled:
        if not settings.relay.credential:
            raise ValueError("relay.credential is required when relay.enabled is true")
        if not settings.relay.base_domain:
            raise ValueError("relay.base_domain is required when relay.enabled is true")
        # The RelayService is served on the *same* gRPC listener as WorkerService
        # (RELAY.md Section 6), which only starts when control.enabled. With the
        # control plane off there is no listener to attach RelayService to, so the
        # relay would be silently unserved while join_hostname is still exposed
        # (PR #973 review). Fail fast rather than half-enabling the relay.
        if not settings.control.enabled:
            raise ValueError(
                "relay.enabled requires control.enabled "
                "(the RelayService shares the control-plane gRPC listener)"
            )

    # bedrock_enabled has no effect without relay.enabled: the deployment gate is
    # relay.enabled AND relay.bedrock_enabled (RelaySettings docstring), so with
    # the relay off no bedrock_port is ever allocated and the flag is silently
    # inert (issue #1552). Warn (not fatal) rather than leaving the contradictory
    # config undiagnosed.
    if settings.relay.bedrock_enabled and not settings.relay.enabled:
        logging.getLogger(__name__).warning(
            "relay.bedrock_enabled is true but relay.enabled is false; "
            "bedrock_enabled has no effect without the relay (the deployment gate "
            "is relay.enabled AND relay.bedrock_enabled). Set relay.enabled=true "
            "or drop relay.bedrock_enabled."
        )

    # The control channel must be authenticated AND encrypted (NFR-SEC-1). The
    # gRPC listener serves over TLS when control.tls.cert_file/key_file are both
    # set; control.tls.insecure=true opts in to a plaintext listener for
    # local/dev only. Require one or the other — fail fast rather than silently
    # binding plaintext. Mirrors the Worker's api.tls required-unless-insecure
    # rule (CONFIGURATION.md Section 6.1).
    if settings.control.enabled:
        _validate_control_tls(settings)
        # Warn (not fatal) if the reconciler grace is too small to keep the
        # duplicate-start window closed against the start's hydrate+command budget
        # (issue #822). Only meaningful when the control plane — and thus the
        # reconciler — is enabled.
        _warn_reconciler_grace_floor(settings)

    # Bind the Storage Port to the config-selected backend now, so an unsupported
    # backend (or, by construction, a missing fs root) fails fast at boot rather
    # than on first use (CONFIGURATION.md Section 3; STORAGE.md Section 7).
    storage = _build_storage(settings)

    # Build the resource pack store (issue #1176): only available for the object
    # backend; None for fs/remote-fs.
    resource_pack_store = _build_resource_pack_store(settings)

    # Build the content-addressed plugin cache store (issue #1306): same backend
    # gating as the resource pack store.
    plugin_cache_store = _build_plugin_cache_store(settings)

    # Build the process-wide version catalog now so its in-process manifest cache
    # is shared across requests (FR-VER-2). No external secret is required, so it
    # cannot fail at boot; it is stored on app state below.
    version_catalog, version_fetcher = _build_version_catalog()

    configure_logging(settings.log.level, settings.log.format)

    heartbeat_timeout = dt.timedelta(seconds=settings.control.heartbeat_timeout_seconds)
    # Worker-side data-plane transfer bound advertised in RegisterAck (issue #874):
    # the larger of the two API transfer budgets plus a margin, so it is always
    # >= the API budget. The API-side dispatch timeout (hydrate/snapshot_timeout)
    # fires first; this Worker bound is the cleanup backstop that structurally
    # closes the unbounded-upload case (#869). Deriving it API-side keeps the
    # Worker on one source — no operator coordination across the two processes.
    transfer_deadline = dt.timedelta(
        seconds=max(
            settings.control.hydrate_timeout_seconds,
            settings.control.snapshot_timeout_seconds,
        )
        + _TRANSFER_DEADLINE_MARGIN_SECONDS
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(
            settings.database.url,
            pool_size=settings.database.pool_size,
            max_overflow=settings.database.max_overflow,
        )
        app.state.engine = engine
        app.state.settings = settings
        app.state.storage = storage
        app.state.resource_pack_store = resource_pack_store
        app.state.plugin_cache_store = plugin_cache_store
        app.state.version_catalog = version_catalog
        # The catalog's manifest-cache invalidator + per-type source prefixes, for
        # the platform-admin manual refresh (issue #286).
        app.state.version_cache_invalidator = version_fetcher
        app.state.version_source_prefixes = _CATALOG_SOURCE_PREFIXES
        # Readiness flag for /readyz (issue #282): the control-plane gRPC server
        # has not started yet; flipped True once start() returns below.
        app.state.grpc_started = False
        # Boot-time reachability probe for object storage (issue #945): a
        # misconfigured or unreachable S3 endpoint must fail fast with a clear
        # diagnostic rather than degrading silently at the first runtime op.
        if isinstance(storage, ObjectStorage):
            obj = settings.storage.object
            assert obj.endpoint is not None
            assert obj.bucket is not None
            await storage.check_reachable(endpoint=obj.endpoint, bucket=obj.bucket)
        # Crash-recovery orphan sweep on startup (STORAGE.md Section 4.3, epic #8
        # note): reclaim any staging dir/prefix or superseded snapshot left by a
        # crash before this process serves. Idempotent and keyed off the live
        # pointer, so it never reclaims authoritative data. The fs sweep is
        # blocking I/O run off the event loop; the object sweep is async (S3 calls).
        if inspect.iscoroutinefunction(storage.sweep):
            await storage.sweep()
        else:
            await asyncio.to_thread(storage.sweep)
        # Bedrock gate-flip sweep (issue #1588): when the Bedrock deployment
        # gate is on, allocate bedrock_port for every server that already has
        # an installed, enabled Geyser but no port (installed while the gate
        # was off). Idempotent and runs once at startup; a no-op when every
        # Geyser server already carries a port.
        if settings.relay.enabled and settings.relay.bedrock_enabled:
            bedrock_range = PortRange(
                start=settings.ports.bedrock_range_start,
                end=settings.ports.bedrock_range_end,
                reserved=frozenset({settings.relay.bedrock_tunnel_port}),
            )
            await SweepBedrockPorts(
                uow=ServersUnitOfWork(create_session_factory(engine)),
                port_range=bedrock_range,
                clock=ServersSystemClock(),
            )()
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
        # Bedrock relay tunnel dispatch (issue #1544) shares the relay's
        # registration and its own tunnel-token table with the RelayService
        # servicer registered below (only when relay.enabled). Constructed
        # unconditionally here -- cheap, in-memory, and harmless when the relay
        # is off, since a server never carries a bedrock_port unless the relay
        # Bedrock gate was on at allocation time (issue #1541) -- so the sink
        # can dispatch OpenBedrockTunnel/CloseBedrockTunnel regardless of
        # construction order relative to the relay.enabled block.
        relay_registration = RelayRegistration()
        bedrock_tunnel_table = BedrockTunnelTable()
        # Exposed on app state so the request-scoped DeleteServer use case can
        # evict a deleted server's tunnel credential (issue #1544).
        app.state.bedrock_tunnel_table = bedrock_tunnel_table
        # Bedrock tunnel syncer (issue #1602): shared by the sink (status-change
        # path) and the lifecycle use cases (INVALID_STATE convergence path).
        # Built unconditionally; the Port's sync_observed returns early when
        # bedrock_port is None, so it is harmless when the relay is off.
        bedrock_tunnel_syncer = BedrockTunnelSyncer(
            create_session_factory(engine),
            control_plane=app.state.control_plane,
            relay_registration=relay_registration,
            bedrock_tunnel_table=bedrock_tunnel_table,
            bedrock_tunnel_port=settings.relay.bedrock_tunnel_port,
        )
        app.state.bedrock_tunnel_sync = bedrock_tunnel_syncer
        # The control-plane event path writes back observed server state through
        # this sink (its own session per call; the servicer has no request UoW).
        state_sink = ServersServerStateSink(
            create_session_factory(engine),
            clock=ServersSystemClock(),
            control_plane=app.state.control_plane,
            relay_registration=relay_registration,
            bedrock_tunnel_table=bedrock_tunnel_table,
            bedrock_tunnel_port=settings.relay.bedrock_tunnel_port,
        )
        # A final-snapshot result that arrives after its dispatch timed out (a late
        # TRANSFER_FAILED once the worker's transfer bound aborts the upload, or a
        # late SUCCESS) releases the held assignment immediately instead of waiting
        # out the reconciler grace (issue #891). The sink runs the same guarded
        # clear the final-snapshot path uses; its control-plane dependency is only
        # the StopServer constructor's (the clear dispatches no command). Injected
        # after construction because the state, the GrpcControlPlane adapter, and
        # this sink form a construction chain.
        control_plane_state.set_late_snapshot_sink(
            ServersLateSnapshotResultSink(
                create_session_factory(engine),
                control_plane=FleetControlPlaneAdapter(
                    registry=registry,
                    control_plane=app.state.control_plane,
                ),
                clock=ServersSystemClock(),
            )
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
        plugin_cache_gc_task: asyncio.Task[None] | None = None
        session_prune_task: asyncio.Task[None] | None = None
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
                transfer_deadline=transfer_deadline,
                control_plane=control_plane_state,
                state_sink=state_sink,
                real_time_events=real_time_events,
                host=settings.server.host,
                port=settings.server.grpc_port,
                cert_file=settings.control.tls.cert_file,
                key_file=settings.control.tls.key_file,
                insecure=settings.control.tls.insecure,
            )
            # Game-ingress relay control surface (RELAY.md Sections 4, 6, 13,
            # issue #956): served on the SAME gRPC listener as WorkerService, only
            # when relay.enabled. The relay dispatches TunnelDial over the existing
            # Worker stream, so it shares the control plane and registry built
            # above; its registration + join-token state are process-local
            # in-memory adapters like the rest of the control plane. The
            # required-when-enabled credential/base_domain are validated at the top
            # of the factory.
            if settings.relay.enabled:
                assert settings.relay.credential is not None
                assert settings.relay.base_domain is not None
                register_relay_service(
                    grpc_server,
                    credential=settings.relay.credential,
                    base_domain=settings.relay.base_domain,
                    registration=relay_registration,
                    token_table=JoinTokenTable(),
                    bedrock_tunnel_table=bedrock_tunnel_table,
                    resolver=ServersServerRouteResolver(create_session_factory(engine)),
                    registry=registry,
                    control_plane=app.state.control_plane,
                    session_sink=ServersSessionSink(create_session_factory(engine)),
                    clock=FleetSystemClock(),
                )
                logging.getLogger(__name__).info(
                    "relay service registered on the gRPC listener",
                    extra={"base_domain": settings.relay.base_domain},
                )
                # Game-session retention prune (RELAY.md Section 8, issue #957):
                # delete game_session rows older than relay.session_retention_days.
                # Runs only when relay.enabled (the relay is what populates the
                # table); a fixed hourly resolution is fine for a days-wide window.
                session_pruner = PruneGameSessions(
                    uow=ServersUnitOfWork(create_session_factory(engine)),
                    clock=ServersSystemClock(),
                    retention=dt.timedelta(days=settings.relay.session_retention_days),
                )
                session_prune_task = asyncio.create_task(
                    run_game_session_prune_loop(
                        session_pruner,
                        tick_seconds=_SESSION_PRUNE_TICK_SECONDS,
                    )
                )
                logging.getLogger(__name__).info("game_session prune loop started")
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
                    data_plane_base_url=settings.server.effective_data_plane_base_url,
                    worker_credential=settings.control.worker_credential,
                    snapshot_timeout_seconds=settings.control.snapshot_timeout_seconds,
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
                data_plane_base_url=settings.server.effective_data_plane_base_url,
                worker_credential=settings.control.worker_credential,
                snapshot_timeout_seconds=settings.control.snapshot_timeout_seconds,
            )
            backup_scheduler = RunBackupScheduleTick(
                uow=ServersUnitOfWork(create_session_factory(engine)),
                create_backup=CreateBackup(
                    uow=ServersUnitOfWork(create_session_factory(engine)),
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
                data_plane_base_url=settings.server.effective_data_plane_base_url,
                worker_credential=settings.control.worker_credential,
                hydrate_timeout_seconds=settings.control.hydrate_timeout_seconds,
                snapshot_timeout_seconds=settings.control.snapshot_timeout_seconds,
                stop_timeout_seconds=settings.control.stop_timeout_seconds,
            )
            # Build a fresh StartServer/StopServer (each with its own UnitOfWork)
            # per reconcile action so concurrent actions never share a session
            # (#871). The control plane, clock, and JAR seams are stateless and
            # reused across the per-action use cases.
            reconciler = RunReconcilerTick(
                uow=ServersUnitOfWork(create_session_factory(engine)),
                make_start_server=lambda: StartServer(
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
                    store_generation=StorageGenerationReader(storage=storage),
                    file_store=StorageFileStoreAdapter(storage=storage),
                    # Carry the real per-server lock as insurance for future locked
                    # reconciler paths (issue #876): a tick that eventually acquires
                    # the lock will contend correctly against HTTP-path holders.
                    lifecycle_lock=PgLifecycleLock(engine=engine),
                    bedrock_tunnel_sync=bedrock_tunnel_syncer,
                ),
                make_stop_server=lambda: StopServer(
                    uow=ServersUnitOfWork(create_session_factory(engine)),
                    control_plane=reconciler_control_plane,
                    clock=ServersSystemClock(),
                    bedrock_tunnel_sync=bedrock_tunnel_syncer,
                ),
                control_plane=reconciler_control_plane,
                store_generation=StorageGenerationReader(storage=storage),
                clock=ServersSystemClock(),
                grace_seconds=settings.reconciler.grace_seconds,
                held_start_grace_seconds=settings.reconciler.held_start_grace_seconds,
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
            # backfill them (DEPLOYMENT.md Section 7). Run from inside the loop's
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
        # Run the periodic plugin-cache GC as a lifespan task (issue #1332,
        # #1403). Gated on the plugin cache store being available (object
        # backend only), NOT on control.enabled — plugin installs work
        # regardless of the control plane, so the GC must too. Reclaims
        # cached plugin/mod blobs not referenced by any server_plugin row.
        if plugin_cache_store is not None:
            plugin_cache_gc = RunPluginCacheGc(
                cache=plugin_cache_store,
                references=PluginCacheReferences(
                    uow=ServersUnitOfWork(create_session_factory(engine))
                ),
                clock=ServersSystemClock(),
            )
            plugin_cache_gc_task = asyncio.create_task(
                run_plugin_cache_gc_loop(
                    plugin_cache_gc,
                    tick_seconds=settings.plugin_cache_gc.interval_seconds,
                )
            )
            logging.getLogger(__name__).info("plugin-cache GC started")
        try:
            yield
        finally:
            prune_attempts_task.cancel()
            with suppress(asyncio.CancelledError):
                await prune_attempts_task
            if session_prune_task is not None:
                session_prune_task.cancel()
                with suppress(asyncio.CancelledError):
                    await session_prune_task
            if plugin_cache_gc_task is not None:
                plugin_cache_gc_task.cancel()
                with suppress(asyncio.CancelledError):
                    await plugin_cache_gc_task
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

    # The entire HTTP API is namespaced under ``/api`` (issue #498) so it can
    # never collide with an SPA client-side route: the SPA is served from this
    # same origin (WEBUI_SPEC 7.7), and three of its deep-links shared paths
    # with API GET routes, returning JSON on a hard reload. With every route
    # (REST, WebSocket, and the OpenAPI schema + docs) under ``/api``, the rule
    # becomes absolute — any non-``/api`` path falls through to the SPA. Health
    # and readiness probes and the Prometheus ``/metrics`` endpoint move under
    # ``/api`` too so there is no carve-out in the fallback (DEPLOYMENT.md,
    # SECURITY.md).
    app = FastAPI(
        title="mc-server-dashboard API",
        lifespan=lifespan,
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
    )
    # Render every error response as RFC 9457 problem+json (issue #371): one body
    # shape for application errors, framework HTTPExceptions, and 422 validation.
    install_problem_handlers(app)
    # Middleware is applied outermost-last: adding the metrics middleware after the
    # correlation-id one makes it the outer wrapper, so it times the full request
    # handling and labels by route template (issue #282).
    app.middleware("http")(correlation_id_middleware)
    # Defence-in-depth security headers (issue #635): CSP, X-Frame-Options,
    # nosniff, Referrer-Policy, Permissions-Policy, conditional Cache-Control
    # and HSTS. Registered after the correlation-ID middleware so it is inside
    # that wrapper (outermost-last ordering).
    app.middleware("http")(security_headers_middleware)
    app.middleware("http")(metrics_middleware)
    # Strip the spurious Content-Type/Content-Length the default JSONResponse
    # stamps onto a 204 No Content (issue #633). Registered last so it is the
    # outermost wrapper and sees the final response of every route.
    app.middleware("http")(strip_no_content_body_headers_middleware)
    # One ``/api`` prefix carried by a parent router that includes every other
    # router, so the prefix lives in one place and the generated openapi.json
    # paths are honest (issue #498). Sub-router order is preserved (e.g. the
    # exact ``/users/me`` paths still register before templated
    # ``/users/{user_id}``).
    api_router = APIRouter(prefix="/api")
    api_router.include_router(health.router)
    api_router.include_router(readiness.router)
    api_router.include_router(metrics.router)
    api_router.include_router(meta.router)
    api_router.include_router(users.router)
    api_router.include_router(admin_users.router)
    api_router.include_router(auth.router)
    api_router.include_router(communities.router)
    api_router.include_router(admin_communities.router)
    api_router.include_router(me.router)
    api_router.include_router(members.router)
    api_router.include_router(roles.router)
    api_router.include_router(grants.router)
    api_router.include_router(servers.router)
    api_router.include_router(server_ports.router)
    api_router.include_router(server_files.router)
    api_router.include_router(server_backups.router)
    api_router.include_router(server_groups.router)
    api_router.include_router(server_plugins.router)
    api_router.include_router(server_catalog.router)
    api_router.include_router(server_resource_packs.router)
    api_router.include_router(server_resource_packs.public_router)
    api_router.include_router(server_resource_packs.assignment_router)
    api_router.include_router(workers.router)
    api_router.include_router(server_events.router)
    api_router.include_router(transfers.router)
    api_router.include_router(versions_api.router)
    api_router.include_router(audit.router)
    app.include_router(api_router)
    # Serve the built Web UI from the same origin when a dist dir is configured
    # (WEBUI_SPEC 7.7, issue #490). Mounted last so the ``/api`` router and the
    # WebSocket endpoints above take strict precedence; the SPA fallback covers
    # every other path. Unset (dev + tests) → no mount.
    if settings.webui.dist_dir is not None:
        mount_webui(app, settings.webui.dist_dir)
    return app
