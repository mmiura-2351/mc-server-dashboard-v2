"""Integration test for the real SqlAlchemyDatabasePing against PostgreSQL.

Runs only when a real database is provided via ``MCD_TEST_DATABASE_URL`` (set by
the CI Postgres service); skipped otherwise so the unit suite stays fast and
hermetic (TESTING.md Section 5). This is the one place the real async-SQLAlchemy
adapter is exercised end-to-end.
"""

from __future__ import annotations

import os

import pytest

from mc_server_dashboard_api.core.adapters.database import (
    SqlAlchemyDatabasePing,
    create_engine,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")


@pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)
async def test_is_reachable_true_against_real_postgres() -> None:
    assert _DB_URL is not None
    engine = create_engine(_DB_URL)
    try:
        ping = SqlAlchemyDatabasePing(engine)
        assert await ping.is_reachable() is True
    finally:
        await engine.dispose()


async def test_is_reachable_false_when_unconnectable() -> None:
    # A port nothing listens on: the adapter must report False, not raise.
    engine = create_engine("postgresql+asyncpg://u:p@127.0.0.1:5/none")
    try:
        ping = SqlAlchemyDatabasePing(engine)
        assert await ping.is_reachable() is False
    finally:
        await engine.dispose()
