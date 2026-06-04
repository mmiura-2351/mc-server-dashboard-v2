"""Migration round-trip for the community tables against real PostgreSQL.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Proves the 0004 migration upgrades and
downgrades cleanly and that the documented tables exist after upgrade
(DATABASE.md Sections 5-6).
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_COMMUNITY_TABLES = {
    "community",
    "membership",
    "role",
    "membership_role",
    "resource_grant",
}


async def test_upgrade_creates_tables_then_downgrade_drops_them() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
            assert _COMMUNITY_TABLES <= tables
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
            assert _COMMUNITY_TABLES.isdisjoint(tables)
            # The alembic bookkeeping table survives a downgrade-to-base; that is
            # expected and is not part of the application schema.
            assert "alembic_version" in tables
    finally:
        await engine.dispose()
