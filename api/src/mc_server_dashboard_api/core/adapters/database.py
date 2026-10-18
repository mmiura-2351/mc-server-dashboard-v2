"""Async SQLAlchemy engine/session plumbing behind the persistence seam.

This is the single place that knows the concrete database technology. Entity
mappings and the ``<Entity>Repository`` / ``UnitOfWork`` adapters land with
their features (DATABASE.md); for now it provides the async engine the wiring
layer owns and the liveness probe behind the :class:`DatabasePing` Port.
"""

from __future__ import annotations

import asyncpg
import sqlalchemy.exc
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


def create_engine(
    url: str,
    *,
    pool_size: int | None = None,
    max_overflow: int | None = None,
) -> AsyncEngine:
    """Create the application's async engine for ``url`` (e.g. asyncpg DSN).

    ``pool_size`` and ``max_overflow`` are forwarded to SQLAlchemy's
    ``create_async_engine`` when supplied; when omitted SQLAlchemy's own
    defaults (5 / 10) apply (issue #884).
    """

    kwargs: dict[str, object] = {"pool_pre_ping": True}
    if pool_size is not None:
        kwargs["pool_size"] = pool_size
    if max_overflow is not None:
        kwargs["max_overflow"] = max_overflow
    return create_async_engine(url, **kwargs)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the async session factory the persistence adapters open sessions from."""

    return async_sessionmaker(engine, expire_on_commit=False)


# What a database outage raises, by when it hits (#3313). asyncpg's connect is
# not translated by SQLAlchemy, so a refused or timed-out connect is the raw
# ``OSError`` and a server starting up, shutting down or full is asyncpg's raw
# class 08 / 57 / ``TooManyConnections`` error; a pool that cannot hand out a
# connection is SQLAlchemy's ``TimeoutError``; a connection lost mid-query is a
# translated error (see :func:`is_transient_database_error`).
_TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    OSError,
    sqlalchemy.exc.TimeoutError,
    sqlalchemy.exc.OperationalError,
    sqlalchemy.exc.InterfaceError,
    asyncpg.PostgresConnectionError,
    asyncpg.OperatorInterventionError,
    asyncpg.TooManyConnectionsError,
)


def is_transient_database_error(exc: BaseException) -> bool:
    """Whether ``exc`` means the database is unavailable for now, not a bug.

    Retrying later may succeed. A connection lost mid-query is the generic
    ``DBAPIError`` asyncpg's errors translate to, told apart by SQLAlchemy
    having invalidated the connection.
    """

    if isinstance(exc, _TRANSIENT_ERRORS):
        return True
    return isinstance(exc, sqlalchemy.exc.DBAPIError) and exc.connection_invalidated


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
