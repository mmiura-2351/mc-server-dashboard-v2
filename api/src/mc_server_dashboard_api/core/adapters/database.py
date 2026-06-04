"""Async SQLAlchemy engine/session plumbing behind the persistence seam.

This is the single place that knows the concrete database technology. Entity
mappings and the ``<Entity>Repository`` / ``UnitOfWork`` adapters land with
their features (DATABASE.md); for now it provides the async engine the wiring
layer owns and the liveness probe behind the :class:`DatabasePing` Port.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from mc_server_dashboard_api.core.domain.health import DatabasePing


class Base(DeclarativeBase):
    """Shared declarative base; every context's ORM models register here.

    A single ``MetaData`` keeps cross-context foreign keys resolvable and gives
    Alembic one place to read the schema from (migrations/env.py).
    """


def create_engine(url: str) -> AsyncEngine:
    """Create the application's async engine for ``url`` (e.g. asyncpg DSN)."""

    return create_async_engine(url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the async session factory the persistence adapters open sessions from."""

    return async_sessionmaker(engine, expire_on_commit=False)


class SqlAlchemyDatabasePing(DatabasePing):
    """:class:`DatabasePing` adapter that runs ``SELECT 1`` on the engine."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def is_reachable(self) -> bool:
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:
            # A liveness probe must report, not raise: any connect/query
            # failure means the dependency is down (degraded, not crashed).
            return False
        return True
