"""FastAPI application factory — the process edge / wiring entry point.

Loads configuration, installs structured logging and the correlation-ID
middleware, builds the async engine, and mounts the routers. This is the only
place (with :mod:`dependencies`) that reads configuration and constructs
adapters (ARCHITECTURE.md Section 2.1, CONFIGURATION.md Section 1).
"""

from __future__ import annotations

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
from mc_server_dashboard_api.core.adapters.database import create_engine
from mc_server_dashboard_api.core.api import health
from mc_server_dashboard_api.identity.api import auth, users
from mc_server_dashboard_api.logging import configure_logging
from mc_server_dashboard_api.middleware import correlation_id_middleware

# Optional TOML config file location, overridable per deployment.
_CONFIG_FILE_ENV = "MCD_API_CONFIG_FILE"


def _resolve_config_file() -> Path | None:
    raw = os.environ.get(_CONFIG_FILE_ENV)
    return Path(raw) if raw else None


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = load_settings(_resolve_config_file())

    # The token signing key is a required secret whenever the auth endpoints are
    # mounted (CONFIGURATION.md Section 5.3); fail fast at boot rather than
    # starting unable to issue or verify tokens (Section 3).
    if settings.auth.token.signing_key is None:
        raise ValueError("auth.token.signing_key is required to mount auth endpoints")

    configure_logging(settings.log.level, settings.log.format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database.url)
        app.state.engine = engine
        app.state.settings = settings
        logging.getLogger(__name__).info(
            "api starting", extra={"config": settings.masked_dump()}
        )
        try:
            yield
        finally:
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
    return app
