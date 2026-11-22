"""Async SQLAlchemy engine/session plumbing behind the persistence seam.

This is the single place that knows the concrete database technology. Entity
mappings and the ``<Entity>Repository`` / ``UnitOfWork`` adapters land with
their features (DATABASE.md); for now it provides the async engine the wiring
layer owns and the liveness probe behind the :class:`DatabasePing` Port.
"""

from __future__ import annotations

from typing import Any

import asyncpg
import sqlalchemy.exc
from sqlalchemy import MetaData, event, text
from sqlalchemy.engine import Dialect, ExceptionContext
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import AsyncAdaptedQueuePool, PoolProxiedConnection

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


_TIMED_OUT = "the database did not answer within the engine's bound"


class _DriverTimeoutPool(AsyncAdaptedQueuePool):
    """The engines' pool, raising the driver's timeout as :class:`DatabaseTimeoutError`.

    A checkout can run out the driver's bound before any statement does: in the
    ping before a pooled connection is handed out, or in the queries that set up
    a new one. SQLAlchemy raises both from here untranslated, past
    ``handle_error``, which sees only the ping's driver errors (issue #4042).
    """

    # Log as the pool it extends, under SQLAlchemy's own quiet namespace.
    _sqla_logger_namespace = "sqlalchemy.pool.impl.AsyncAdaptedQueuePool"

    def connect(self) -> PoolProxiedConnection:
        try:
            return super().connect()
        except TimeoutError as exc:
            raise DatabaseTimeoutError(_TIMED_OUT) from exc


def _type_driver_timeout(context: ExceptionContext) -> BaseException | None:
    """Raise a statement's, a commit's or a rollback's driver timeout typed.

    Any bare ``TimeoutError`` SQLAlchemy meets there is the driver's bound: a
    deadline set around the call cancels it instead (issue #4042).
    """

    if isinstance(context.original_exception, TimeoutError):
        return DatabaseTimeoutError(_TIMED_OUT)
    return None


def create_engine(
    url: str,
    *,
    pool_size: int | None = None,
    max_overflow: int | None = None,
    connect_timeout: float | None = None,
    statement_timeout: float | None = None,
) -> AsyncEngine:
    """Create the application's async engine for ``url`` (e.g. asyncpg DSN).

    ``pool_size`` and ``max_overflow`` are forwarded to SQLAlchemy's
    ``create_async_engine`` when supplied; when omitted SQLAlchemy's own
    defaults (5 / 10) apply (issue #884).

    ``connect_timeout`` bounds a new connection, its authentication included,
    and ``statement_timeout`` each exchange with the server on a connection, in
    seconds (issue #4042). Either one omitted keeps asyncpg's own default: 60 s
    for a connect, no bound for a statement. Both are the driver's, not the
    server's ``statement_timeout``, so they also fail a connection that went
    silent, on which no server-side timeout can answer. A bound that runs out
    raises :class:`DatabaseTimeoutError`, wherever the engine meets it.
    """

    kwargs: dict[str, object] = {
        "pool_pre_ping": True,
        "poolclass": _DriverTimeoutPool,
    }
    if pool_size is not None:
        kwargs["pool_size"] = pool_size
    if max_overflow is not None:
        kwargs["max_overflow"] = max_overflow
    connect_args: dict[str, float] = {}
    if connect_timeout is not None:
        connect_args["timeout"] = connect_timeout
    if statement_timeout is not None:
        connect_args["command_timeout"] = statement_timeout
    if connect_args:
        kwargs["connect_args"] = connect_args
    engine = create_async_engine(url, **kwargs)
    event.listen(engine.sync_engine, "handle_error", _type_driver_timeout)
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the async session factory the persistence adapters open sessions from."""

    return async_sessionmaker(engine, expire_on_commit=False)


class DatabaseConnectError(Exception):
    """A new connection failed because the database is unavailable (issue #3942).

    asyncpg raises a refused or timed-out connect as the raw ``OSError`` below,
    which by type is no different from a filesystem fault. An engine passed to
    :func:`translate_connect_outages` raises this instead, with the driver's error
    as its ``__cause__``, so a caller that also does file I/O can tell the two
    apart.
    """


class DatabaseTimeoutError(Exception):
    """The database did not answer within the engine's bound (issue #4042).

    asyncpg raises a connect or a statement that outlasts its bound as the bare
    ``TimeoutError``, which by type is no different from a store's or a Worker's.
    Every engine :func:`create_engine` builds raises this instead, with the
    driver's error as its ``__cause__``, so the outage handlers can tell the two
    apart.
    """


# What a database outage raises, by when it hits (#3313, #3995). SQLAlchemy does
# not translate a socket error, so a refused, timed-out or unresolvable connect is
# the raw ``OSError``, or :class:`DatabaseConnectError` on an engine that
# translates it. A pool that cannot hand out a connection is SQLAlchemy's
# ``TimeoutError``. A server that is full, starting up or shutting down answers a
# connect with an error that reaches the caller as the generic ``DBAPIError``,
# its SQLSTATE on ``orig`` (``_OUTAGE_SQLSTATES``). A connection lost mid-query,
# to a terminated backend or a server shutdown or crash, is the generic
# ``DBAPIError`` or its ``InterfaceError`` ("connection is closed") too, with the
# connection invalidated. No ``DBAPIError`` subclass is an outage by type
# (#4010): an ``InterfaceError`` on a live connection is a concurrency bug or a
# bad connect argument. asyncpg's own outage classes count for an error that
# reaches a caller untranslated; through SQLAlchemy 2.1 none does. A statement
# that outlasts the engine's bound is :class:`DatabaseTimeoutError` (#4042). A
# cancel the server reports, 57014, is never that bound: it is someone's cancel or
# the operator's own ``statement_timeout``, and not an outage.
_TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    DatabaseConnectError,
    DatabaseTimeoutError,
    OSError,
    sqlalchemy.exc.TimeoutError,
    asyncpg.PostgresConnectionError,
    asyncpg.OperatorInterventionError,
    asyncpg.TooManyConnectionsError,
)

# The SQLSTATEs of a server that cannot take the connection now, besides class
# 08 (connection exception): an administrator or crash shutdown, a server
# starting up, shutting down or recovering, and a full connection table. A wrong
# password or database (28xxx, 3D000) is a configuration fault, not an outage.
_OUTAGE_SQLSTATES = frozenset({"57P01", "57P02", "57P03", "53300"})

# The one member of class 08 that is not an outage (#4010): a protocol violation
# is a fault of the driver or a proxy, so it is never transient, even when the
# server closed the connection over it.
_PROTOCOL_VIOLATION = "08P01"


def is_transient_database_error(exc: BaseException) -> bool:
    """Whether ``exc`` means the database is unavailable for now, not a bug.

    Retrying later may succeed. A generic ``DBAPIError`` is one when SQLAlchemy
    invalidated the connection, or when the driver's error it wraps carries an
    outage's SQLSTATE. A protocol violation is not one in any shape.
    """

    if isinstance(exc, asyncpg.ProtocolViolationError):
        return False
    if isinstance(exc, _TRANSIENT_ERRORS):
        return True
    if not isinstance(exc, sqlalchemy.exc.DBAPIError):
        return False
    sqlstate = getattr(exc.orig, "sqlstate", None) or ""
    if sqlstate == _PROTOCOL_VIOLATION:
        return False
    return (
        exc.connection_invalidated
        or sqlstate.startswith("08")
        or sqlstate in _OUTAGE_SQLSTATES
    )


def is_database_timeout(exc: BaseException) -> bool:
    """Whether ``exc`` is the database not answering within an engine's bound.

    A statement's or a checkout's is :class:`DatabaseTimeoutError`; a connect's,
    on an engine passed to :func:`translate_connect_outages`, is the
    :class:`DatabaseConnectError` over the driver's ``TimeoutError`` (issue
    #4042).
    """

    return isinstance(exc, DatabaseTimeoutError) or (
        isinstance(exc, DatabaseConnectError)
        and isinstance(exc.__cause__, TimeoutError)
    )


def translate_connect_outages(engine: AsyncEngine) -> None:
    """Make ``engine`` raise :class:`DatabaseConnectError` for an outage at connect.

    Hooks the dialect's connect, so it covers every new connection the pool makes,
    a reconnect after a failed pre-ping included. Whether a failure is an outage is
    :func:`is_transient_database_error`'s call; any other failure is raised as it
    was.
    """

    def _connect(dialect: Dialect, _record: object, cargs: Any, cparams: Any) -> Any:
        try:
            return dialect.connect(*cargs, **cparams)
        except Exception as exc:
            if is_transient_database_error(exc):
                raise DatabaseConnectError(f"{type(exc).__name__}: {exc}") from exc
            raise

    event.listen(engine.sync_engine, "do_connect", _connect)


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
