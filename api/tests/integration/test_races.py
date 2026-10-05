"""Tests for the shared race-test harness in ``tests/integration/races.py`` (#3255).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (a real PostgreSQL); skipped
otherwise (TESTING.md Section 5).
"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from tests.integration.races import race_database

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)


async def test_race_engine_commits_do_not_wait_for_the_wal_flush() -> None:
    # A commit that waits for its fsync can take tens of seconds on a host under
    # writeback pressure, far past the harness's bounds.
    assert _DB_URL is not None
    async with race_database(_DB_URL) as engine, engine.connect() as conn:
        setting = (await conn.execute(text("SHOW synchronous_commit"))).scalar_one()

    assert setting == "off"


class _TestFailed(Exception):
    """Stands in for the failure of a race test that never resumed its pause."""


async def _fail_holding_a_lock(
    url: str, opened: asyncio.Future[tuple[AsyncConnection, int]]
) -> None:
    async with race_database(url) as engine:
        leftover = await engine.connect()
        await leftover.execute(text('LOCK TABLE "user" IN ACCESS SHARE MODE'))
        pid = (await leftover.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        opened.set_result((leftover, pid))
        raise _TestFailed


async def test_teardown_ends_a_transaction_the_failed_test_left_open() -> None:
    # A race test that fails before resuming its paused transaction leaves it
    # open with its locks held. The teardown must not wait on it -- the
    # downgrade would wait forever -- and the test's own failure must surface.
    assert _DB_URL is not None
    opened: asyncio.Future[tuple[AsyncConnection, int]] = (
        asyncio.get_running_loop().create_future()
    )
    run = asyncio.create_task(_fail_holding_a_lock(_DB_URL, opened))
    leftover, pid = await opened

    # Rather than timing the teardown, watch for anything waiting on the
    # leftover's locks; on a regression, release them so the run still ends.
    query = text(
        "SELECT 1 FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid))"
    )
    probe = create_async_engine(_DB_URL, poolclass=NullPool)
    blocked = False
    try:
        while not run.done() and not blocked:
            try:
                async with probe.connect() as conn:
                    blocked = (
                        await conn.execute(query, {"pid": pid})
                    ).first() is not None
            except DBAPIError:
                pass  # The teardown ends every open transaction, the probe's too.
            await asyncio.sleep(0.02)
    finally:
        if blocked:
            await leftover.invalidate()
        await probe.dispose()

    with pytest.raises(_TestFailed):
        await run
    await leftover.invalidate()
    assert not blocked, "the teardown waited on the leftover transaction's locks"
