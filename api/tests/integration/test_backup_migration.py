"""Migration round-trip for the ``backup`` table against real PostgreSQL.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Proves the 0006 migration upgrades and
downgrades cleanly and that the ``backup`` table exists after upgrade
(DATABASE.md Section 8).
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


async def test_upgrade_creates_backup_then_downgrade_drops_it() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
            assert "backup" in tables
            indexes = await conn.run_sync(
                lambda sync_conn: {
                    ix["name"] for ix in inspect(sync_conn).get_indexes("backup")
                }
            )
            assert "ix_backup_server_id_created_at" in indexes
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
            assert "backup" not in tables
            assert "alembic_version" in tables
    finally:
        await engine.dispose()
