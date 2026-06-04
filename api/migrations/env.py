"""Alembic environment: async engine, URL from the app's configuration.

The connection string is a secret (CONFIGURATION.md Section 3), so it is read
from the same ``MCD_API_`` configuration the service uses rather than from
``alembic.ini``. ``target_metadata`` is ``None`` until entity tables land with
their features (DATABASE.md); the baseline migration is empty.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from mc_server_dashboard_api.config import load_settings
from mc_server_dashboard_api.core.adapters.database import create_engine

target_metadata = None


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
