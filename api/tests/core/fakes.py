"""Shared test doubles for the core context's tests."""

from __future__ import annotations

import asyncio

import asyncpg
import sqlalchemy.exc
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi


class RecordingSleep:
    """Fake ``asyncio.sleep``: records each delay, returns at once for the first
    ``returns`` calls, then parks until the task is cancelled."""

    def __init__(self, *, returns: int) -> None:
        self.delays: list[float] = []
        self.parked = asyncio.Event()
        self._returns = returns

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) > self._returns:
            self.parked.set()
            await asyncio.Event().wait()


def server_error(
    error: asyncpg.PostgresError, *, invalidated: bool = False
) -> sqlalchemy.exc.DBAPIError:
    """What SQLAlchemy over asyncpg raises when the server answers with ``error``.

    Built the way the 2.1 dialect builds it (#3995): asyncpg's error becomes the
    dialect's own ``Error``, which carries its SQLSTATE and has asyncpg's error as
    its cause, and SQLAlchemy wraps that in the generic ``DBAPIError``. A real
    server refusing a connect gives this shape, the connection not invalidated
    (tests/integration/test_transient_database_error.py). A connection lost
    mid-query is the same shape with ``invalidated``.
    """

    # The dialect's ``Error.__init__`` is untyped.
    orig = AsyncAdapt_asyncpg_dbapi.Error(error.args[0], error)  # type: ignore[no-untyped-call]
    orig.__cause__ = error
    wrapped = sqlalchemy.exc.DBAPIError.instance(
        None,
        None,
        orig,
        AsyncAdapt_asyncpg_dbapi.Error,
        connection_invalidated=invalidated,
    )
    assert type(wrapped) is sqlalchemy.exc.DBAPIError
    return wrapped


def interface_error(
    error: asyncpg.InterfaceError, *, invalidated: bool = False
) -> sqlalchemy.exc.InterfaceError:
    """What SQLAlchemy over asyncpg raises when the driver itself refuses ``error``.

    The 2.1 dialect maps asyncpg's ``InterfaceError`` to its own, which has no
    SQLSTATE, and SQLAlchemy wraps that in ``sqlalchemy.exc.InterfaceError``
    (#4010). "another operation is in progress" on a live connection is a
    concurrency bug; "connection is closed" comes with ``invalidated``.
    """

    # The dialect's ``InterfaceError.__init__`` is untyped.
    orig = AsyncAdapt_asyncpg_dbapi.InterfaceError(error.args[0], error)  # type: ignore[no-untyped-call]
    orig.__cause__ = error
    wrapped = sqlalchemy.exc.DBAPIError.instance(
        None,
        None,
        orig,
        AsyncAdapt_asyncpg_dbapi.Error,
        connection_invalidated=invalidated,
    )
    assert type(wrapped) is sqlalchemy.exc.InterfaceError
    return wrapped
