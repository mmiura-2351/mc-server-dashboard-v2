"""The engine's connect and statement bounds (issue #4042).

A database that stops answering, rather than refusing, must fail a connect or a
statement within its bound, with an error :func:`is_transient_database_error`
counts as an outage, so a held Worker report retries instead of waiting forever.
Two fakes stand in for the stalled server: one accepts the TCP connection and
never answers it, the other completes the startup and then answers no query, as a
frozen server does.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from mc_server_dashboard_api.core.adapters.database import (
    create_engine,
    is_transient_database_error,
)
from tests.core.fakes import black_hole_database, first_failure, silent_database

_BOUND_SECONDS = 0.2
# Generous against a loaded host, and far below the 60 s connect timeout and the
# unbounded statements the driver would otherwise apply.
_WITHIN_SECONDS = 2.0


async def _query(engine: AsyncEngine) -> None:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


async def test_a_connect_the_server_never_answers_fails_within_the_bound() -> None:
    with black_hole_database() as url:
        engine = create_engine(url, connect_timeout=_BOUND_SECONDS)
        error, elapsed = await first_failure(lambda: _query(engine))

    assert elapsed < _WITHIN_SECONDS
    assert is_transient_database_error(error) is True


async def test_a_query_the_server_never_answers_fails_within_the_bound() -> None:
    # The bound is the driver's own, not the server's ``statement_timeout``: a
    # server that stopped answering cannot cancel anything.
    with silent_database() as url:
        engine = create_engine(url, statement_timeout=_BOUND_SECONDS)
        error, elapsed = await first_failure(lambda: _query(engine))

    assert elapsed < _WITHIN_SECONDS
    assert is_transient_database_error(error) is True
