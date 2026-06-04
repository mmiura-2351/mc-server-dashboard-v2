"""FastAPI application factory — the process edge / wiring entry point.

Loads configuration, installs structured logging and the correlation-ID
middleware, builds the async engine, and mounts the routers. This is the only
place (with :mod:`dependencies`) that reads configuration and constructs
adapters (ARCHITECTURE.md Section 2.1, CONFIGURATION.md Section 1).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from mc_server_dashboard_api.community.api import (
    communities,
    grants,
    members,
    roles,
)
from mc_server_dashboard_api.config import Settings, load_settings
from mc_server_dashboard_api.core.adapters.database import (
    create_engine,
    create_session_factory,
)
from mc_server_dashboard_api.core.api import health
from mc_server_dashboard_api.fleet.adapters.clock import SystemClock as FleetSystemClock
from mc_server_dashboard_api.fleet.adapters.control_plane import (
    ControlPlaneState,
    GrpcControlPlane,
)
from mc_server_dashboard_api.fleet.adapters.grpc_server import make_grpc_server
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.api import workers
from mc_server_dashboard_api.identity.api import auth, users
from mc_server_dashboard_api.logging import configure_logging
from mc_server_dashboard_api.middleware import correlation_id_middleware
from mc_server_dashboard_api.servers.adapters.clock import (
    SystemClock as ServersSystemClock,
)
from mc_server_dashboard_api.servers.adapters.server_state_sink import (
    ServersServerStateSink,
)
from mc_server_dashboard_api.servers.api import servers
from mc_server_dashboard_api.storage.adapters.fs import FsStorage

# Optional TOML config file location, overridable per deployment.
_CONFIG_FILE_ENV = "MCD_API_CONFIG_FILE"


def _resolve_config_file() -> Path | None:
    raw = os.environ.get(_CONFIG_FILE_ENV)
    return Path(raw) if raw else None


def _build_storage(settings: Settings) -> FsStorage:
    """Bind the :class:`Storage` Port to the config-selected adapter (STORAGE.md §7).

    Only ``fs`` is implemented in M1 (and ``remote-fs`` reuses it via a POSIX
    mount, Section 7.2). The ``object`` backend lands in a later sub-issue (#105);
    selecting an unimplemented backend fails fast at boot rather than starting with
    no working store (CONFIGURATION.md Section 3).
    """

    if settings.storage.backend in ("fs", "remote-fs"):
        return FsStorage(
            Path(settings.storage.fs.root),
            version_retention=settings.storage.version_retention,
        )
    raise ValueError(
        f"storage.backend {settings.storage.backend!r} has no adapter yet "
        "(only 'fs'/'remote-fs' are implemented in M1)"
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

    # Bind the Storage Port to the config-selected backend now, so an unsupported
    # backend (or, by construction, a missing fs root) fails fast at boot rather
    # than on first use (CONFIGURATION.md Section 3; STORAGE.md Section 7).
    storage = _build_storage(settings)

    configure_logging(settings.log.level, settings.log.format)

    heartbeat_timeout = dt.timedelta(seconds=settings.control.heartbeat_timeout_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database.url)
        app.state.engine = engine
        app.state.settings = settings
        app.state.storage = storage
        # Crash-recovery orphan sweep on startup (STORAGE.md Section 4.3, epic #8
        # note): reclaim any staging dir or superseded snapshot left by a crash
        # before this process serves. Idempotent and keyed off the live ``current``
        # target, so it never reclaims authoritative data. Runs off the event loop.
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
        logging.getLogger(__name__).info(
            "api starting", extra={"config": settings.masked_dump()}
        )
        grpc_server = None
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
                host=settings.server.host,
                port=settings.server.grpc_port,
            )
            await grpc_server.start()
            app.state.grpc_server = grpc_server
            logging.getLogger(__name__).info(
                "control-plane gRPC server started",
                extra={"port": settings.server.grpc_port},
            )
        try:
            yield
        finally:
            if grpc_server is not None:
                await grpc_server.stop(grace=None)
            await engine.dispose()

    app = FastAPI(title="mc-server-dashboard API", lifespan=lifespan)
    app.middleware("http")(correlation_id_middleware)
    app.include_router(health.router)
    app.include_router(users.router)
    app.include_router(auth.router)
    app.include_router(communities.router)
    app.include_router(members.router)
    app.include_router(roles.router)
    app.include_router(grants.router)
    app.include_router(servers.router)
    app.include_router(workers.router)
    return app
