"""Alembic environment: async engine, URL from the app's configuration.

The connection string is a secret (CONFIGURATION.md Section 3), so it is read
from the same ``MCD_API_`` configuration the service uses rather than from
``alembic.ini``. ``target_metadata`` is the shared declarative ``Base.metadata``;
each context's ORM models are imported so their tables register on it.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from mc_server_dashboard_api.audit.adapters import models as _audit_models
from mc_server_dashboard_api.community.adapters import models as _community_models
from mc_server_dashboard_api.config import load_settings
from mc_server_dashboard_api.core.adapters.database import Base, create_engine
from mc_server_dashboard_api.identity.adapters import models as _identity_models
from mc_server_dashboard_api.servers.adapters import backup_models as _backup_models
from mc_server_dashboard_api.servers.adapters import models as _servers_models

# Importing the models registers their tables on ``Base.metadata`` for autogenerate.
_ = (
    _identity_models,
    _community_models,
    _servers_models,
    _backup_models,
    _audit_models,
)

target_metadata = Base.metadata


def _database_url() -> str:
    return load_settings(config_file=None).database.url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_engine(_database_url())
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
