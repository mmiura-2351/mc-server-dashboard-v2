"""Unit tests for the transient-database-error classification (#3313).

A database outage surfaces through SQLAlchemy over asyncpg in several shapes,
depending on when it hits: a refused connect raises the raw ``OSError``, a
connection killed mid-query the generic ``DBAPIError`` with the connection
invalidated, an exhausted pool SQLAlchemy's own ``TimeoutError``, and a server
starting up or shutting down asyncpg's raw class-57 errors at connect. Each is
"retry later"; a programming or data error is not.
"""

from __future__ import annotations

import asyncpg
import pytest
import sqlalchemy.exc
from sqlalchemy import text

from mc_server_dashboard_api.core.adapters.database import (
    create_engine,
    is_transient_database_error,
)


def _dbapi(cls: type[sqlalchemy.exc.DBAPIError], *, invalidated: bool) -> Exception:
    return cls("SELECT 1", None, Exception("orig"), connection_invalidated=invalidated)


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(ConnectionRefusedError(111, "refused"), id="connect-refused"),
        pytest.param(TimeoutError(), id="connect-timeout"),
        pytest.param(sqlalchemy.exc.TimeoutError("pool"), id="pool-timeout"),
        pytest.param(
            _dbapi(sqlalchemy.exc.OperationalError, invalidated=False),
            id="operational",
        ),
        pytest.param(
            _dbapi(sqlalchemy.exc.InterfaceError, invalidated=False), id="interface"
        ),
        pytest.param(
            _dbapi(sqlalchemy.exc.DBAPIError, invalidated=True),
            id="connection-invalidated",
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
        pytest.param(ValueError("bug"), id="bug"),
    ],
)
def test_other_errors_are_not_transient(exc: Exception) -> None:
    assert is_transient_database_error(exc) is False


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
