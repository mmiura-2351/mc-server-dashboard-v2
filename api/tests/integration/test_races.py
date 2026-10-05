"""Tests for the shared race-test harness in ``tests/integration/races.py`` (#3255).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (a real PostgreSQL); skipped
otherwise (TESTING.md Section 5).
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.integration.races import end_leftover_transactions, race_database

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


async def test_leftover_transaction_is_ended() -> None:
    # A race test that fails before resuming its paused transaction leaves it
    # open with its locks held; the teardown's downgrade would wait on it forever.
    assert _DB_URL is not None
    async with race_database(_DB_URL) as engine:
        leftover = await engine.connect()
        await leftover.execute(text('LOCK TABLE "user" IN ACCESS SHARE MODE'))

        await end_leftover_transactions(engine)

        with pytest.raises(DBAPIError):
            await leftover.execute(text("SELECT 1"))
        await leftover.invalidate()
