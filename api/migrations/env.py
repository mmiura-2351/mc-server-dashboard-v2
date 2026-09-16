"""Alembic environment: async engine, URL from the app's configuration.

The connection string is a secret (CONFIGURATION.md Section 3), so it is read
from the same ``MCD_API_`` configuration the service uses rather than from
``alembic.ini``. ``target_metadata`` comes from ``model_registry``, which imports
each context's ORM models so their tables register on the shared ``Base.metadata``.
"""

from __future__ import annotations

import asyncio

from alembic import context

# ``model_registry`` imports every adapters model module so their tables register
# on the shared ``Base.metadata`` it exposes as ``target_metadata``. Keeping the
# registration in its own importable module lets a test verify it in isolation.
from model_registry import target_metadata
from sqlalchemy.engine import Connection

from mc_server_dashboard_api.app import _resolve_config_file
from mc_server_dashboard_api.config import load_settings
from mc_server_dashboard_api.core.adapters.database import create_engine


def _database_url() -> str:
    # ``_resolve_config_file`` is the app's own ``MCD_API_CONFIG_FILE`` lookup
    # (the entry points in ``serve`` and ``integrity_sweep_cli`` reuse it the
    # same way). Reading the variable here instead would let the two readers
    # drift, which is the #2953 defect: with the file channel ignored, Alembic
    # ran against a different database than the API -- or against none at all.
    return load_settings(_resolve_config_file()).database.url


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
