"""Unit tests for the transient-database-error classification (#3313, #3995).

A database outage surfaces through SQLAlchemy over asyncpg in several shapes,
depending on when it hits: a refused connect raises the raw ``OSError``, a
connection killed mid-query the generic ``DBAPIError`` with the connection
invalidated, an exhausted pool SQLAlchemy's own ``TimeoutError``, and a server
that is full, starting up or shutting down answers a connect with an error that
reaches the caller as the generic ``DBAPIError``, its SQLSTATE on ``orig``. Each
is "retry later"; a programming or data error, and a server that refuses the
configured credentials or database, are not.
"""

from __future__ import annotations

import asyncpg
import pytest
import sqlalchemy.exc
from sqlalchemy import text
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi

from mc_server_dashboard_api.core.adapters.database import (
    DatabaseConnectError,
    create_engine,
    is_transient_database_error,
)
from tests.core.fakes import interface_error, server_error


def _dbapi(cls: type[sqlalchemy.exc.DBAPIError], *, invalidated: bool) -> Exception:
    return cls("SELECT 1", None, Exception("orig"), connection_invalidated=invalidated)


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(ConnectionRefusedError(111, "refused"), id="connect-refused"),
        pytest.param(TimeoutError(), id="connect-timeout"),
        pytest.param(sqlalchemy.exc.TimeoutError("pool"), id="pool-timeout"),
        pytest.param(
            _dbapi(sqlalchemy.exc.DBAPIError, invalidated=True),
            id="connection-invalidated",
        ),
        pytest.param(
            interface_error(
                asyncpg.InterfaceError("connection is closed"), invalidated=True
            ),
            id="interface-connection-closed",
        ),
        pytest.param(asyncpg.CannotConnectNowError("starting up"), id="starting-up"),
        pytest.param(asyncpg.AdminShutdownError("shutting down"), id="shutting-down"),
        pytest.param(asyncpg.TooManyConnectionsError("full"), id="too-many"),
        pytest.param(
            asyncpg.ConnectionFailureError("connection failure"), id="class-08"
        ),
    ],
)
def test_outage_shapes_are_transient(exc: Exception) -> None:
    assert is_transient_database_error(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(
            _dbapi(sqlalchemy.exc.IntegrityError, invalidated=False), id="integrity"
        ),
        pytest.param(
            _dbapi(sqlalchemy.exc.ProgrammingError, invalidated=False),
            id="programming",
        ),
        pytest.param(
            _dbapi(sqlalchemy.exc.DBAPIError, invalidated=False),
            id="dbapi-live-connection",
        ),
        # Neither type is an outage by itself (#4010): the 2.1 dialect maps
        # nothing to ``OperationalError``, and an ``InterfaceError`` on a live
        # connection is a fault of the caller or its configuration.
        pytest.param(
            _dbapi(sqlalchemy.exc.OperationalError, invalidated=False),
            id="operational-live-connection",
        ),
        pytest.param(
            interface_error(
                asyncpg.InterfaceError(
                    "cannot perform operation: another operation is in progress"
                )
            ),
            id="interface-another-operation",
        ),
        pytest.param(ValueError("bug"), id="bug"),
    ],
)
def test_other_errors_are_not_transient(exc: Exception) -> None:
    assert is_transient_database_error(exc) is False


@pytest.mark.parametrize(
    ("error", "sqlstate"),
    [
        pytest.param(asyncpg.PostgresConnectionError("x"), "08000", id="08000"),
        pytest.param(asyncpg.ConnectionDoesNotExistError("x"), "08003", id="08003"),
        pytest.param(asyncpg.ConnectionFailureError("x"), "08006", id="08006"),
        pytest.param(
            asyncpg.AdminShutdownError("terminating connection"), "57P01", id="57P01"
        ),
        pytest.param(asyncpg.CrashShutdownError("crash"), "57P02", id="57P02"),
        pytest.param(
            asyncpg.CannotConnectNowError("the database system is starting up"),
            "57P03",
            id="57P03",
        ),
        pytest.param(
            asyncpg.TooManyConnectionsError("sorry, too many clients already"),
            "53300",
            id="53300",
        ),
    ],
)
def test_an_outage_the_server_reports_is_transient(
    error: asyncpg.PostgresError, sqlstate: str
) -> None:
    assert error.sqlstate == sqlstate
    assert is_transient_database_error(server_error(error)) is True


@pytest.mark.parametrize(
    ("error", "sqlstate"),
    [
        # A protocol violation is a driver or proxy fault, not an outage (#4010).
        pytest.param(asyncpg.ProtocolViolationError("x"), "08P01", id="08P01"),
        # A cancelled or timed-out statement, a dropped database and an idle
        # session timed out are not an outage of the server.
        pytest.param(asyncpg.QueryCanceledError("x"), "57014", id="57014"),
        pytest.param(asyncpg.DatabaseDroppedError("x"), "57P04", id="57P04"),
        pytest.param(asyncpg.IdleSessionTimeoutError("x"), "57P05", id="57P05"),
        # A serialization failure or a deadlock is the transaction's to retry,
        # not the server being unavailable.
        pytest.param(asyncpg.SerializationError("x"), "40001", id="40001"),
        pytest.param(asyncpg.DeadlockDetectedError("x"), "40P01", id="40P01"),
        # Out of disk or memory is not a full connection table.
        pytest.param(asyncpg.DiskFullError("x"), "53100", id="53100"),
        pytest.param(asyncpg.OutOfMemoryError("x"), "53200", id="53200"),
        # Wrong credentials or database are a configuration fault: retrying
        # does not fix them.
        pytest.param(
            asyncpg.InvalidAuthorizationSpecificationError("x"), "28000", id="28000"
        ),
        pytest.param(
            asyncpg.InvalidPasswordError("password authentication failed"),
            "28P01",
            id="28P01",
        ),
        pytest.param(
            asyncpg.InvalidCatalogNameError("database does not exist"),
            "3D000",
            id="3D000",
        ),
    ],
)
def test_another_error_the_server_reports_is_not_transient(
    error: asyncpg.PostgresError, sqlstate: str
) -> None:
    assert error.sqlstate == sqlstate
    assert is_transient_database_error(server_error(error)) is False


def test_a_connection_lost_mid_query_is_transient() -> None:
    lost = asyncpg.ConnectionDoesNotExistError("connection was closed")

    assert is_transient_database_error(server_error(lost, invalidated=True)) is True


def test_a_protocol_violation_is_not_transient_in_any_shape() -> None:
    # The server closes the connection after a protocol violation, so it can
    # arrive invalidated too; the fault is still the driver's or a proxy's.
    violation = asyncpg.ProtocolViolationError("invalid message format")

    assert is_transient_database_error(violation) is False
    assert is_transient_database_error(server_error(violation)) is False
    assert (
        is_transient_database_error(server_error(violation, invalidated=True)) is False
    )


def test_an_error_with_no_sqlstate_is_not_transient() -> None:
    # The dialect's ``Error`` always has the attribute; with no server error
    # behind it, it is ``None``.
    orig = AsyncAdapt_asyncpg_dbapi.Error("no server error")  # type: ignore[no-untyped-call]
    wrapped = sqlalchemy.exc.DBAPIError.instance(
        None, None, orig, AsyncAdapt_asyncpg_dbapi.Error
    )

    assert orig.sqlstate is None
    assert is_transient_database_error(wrapped) is False


def test_a_connect_outage_an_engine_translated_is_transient() -> None:
    translated = DatabaseConnectError("ConnectionRefusedError: refused")
    translated.__cause__ = ConnectionRefusedError(111, "refused")

    assert is_transient_database_error(translated) is True


async def test_an_unreachable_database_raises_a_transient_error() -> None:
    # What the driver really raises when nothing listens, not a hand-built one.
    engine = create_engine("postgresql+asyncpg://u:p@127.0.0.1:5/none")
    try:
        with pytest.raises(Exception) as caught:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()
    assert is_transient_database_error(caught.value) is True
