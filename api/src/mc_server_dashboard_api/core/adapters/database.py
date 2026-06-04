"""Async SQLAlchemy engine/session plumbing behind the persistence seam.

This is the single place that knows the concrete database technology. Entity
mappings and the ``<Entity>Repository`` / ``UnitOfWork`` adapters land with
their features (DATABASE.md); for now it provides the async engine the wiring
layer owns and the liveness probe behind the :class:`DatabasePing` Port.
"""

from __future__ import annotations

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from mc_server_dashboard_api.core.domain.health import DatabasePing

# Deterministic constraint/index names so a model with no explicit name renders
# the same name the hand-written migrations created, keeping a future Alembic
# autogenerate diff quiet (issue #60). No ``ck`` template on purpose: the ``ck``
# convention interpolates ``%(constraint_name)s``, so it re-prefixes any
# *explicitly* named CheckConstraint -- and because this metadata is also
# Alembic's ``target_metadata``, ``op.create_table`` would apply it during a
# migration too, turning the migration's ``name="ck_server_type"`` into
# ``ck_server_ck_server_type`` in the database. CheckConstraints are always
# named explicitly here (models and migrations alike), so they need no template.
_NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Shared declarative base; every context's ORM models register here.

    A single ``MetaData`` keeps cross-context foreign keys resolvable and gives
    Alembic one place to read the schema from (migrations/env.py). Its
    ``naming_convention`` pins constraint/index names (issue #60).
    """

    metadata = MetaData(naming_convention=_NAMING_CONVENTION)


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
