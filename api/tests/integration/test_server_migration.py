"""Migration round-trip for the ``server`` table against real PostgreSQL.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Proves the 0005 migration upgrades and
downgrades cleanly and that the ``server`` table exists after upgrade
(DATABASE.md Section 7).
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)


async def test_upgrade_creates_server_then_downgrade_drops_it() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
            assert "server" in tables
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
            assert "server" not in tables
            assert "alembic_version" in tables
    finally:
        await engine.dispose()


async def test_check_admits_fabric_server_type() -> None:
    # Guards the issue #267 drift: the ORM/enum accept ``fabric`` but the live
    # ``ck_server_type`` CHECK must too, or a fabric INSERT 500s on a migrated DB.
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        community_id = uuid.uuid4()
        server_id = uuid.uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO community (id, name, created_at, updated_at) "
                    "VALUES (:id, :name, now(), now())"
                ),
                {"id": community_id, "name": "guild"},
            )
            await conn.execute(
                text(
                    "INSERT INTO server (id, community_id, name, mc_edition, "
                    "mc_version, server_type, config, slug, "
                    "desired_state, observed_state, created_at, updated_at) VALUES "
                    "(:id, :community_id, :name, 'java', '1.21.1', 'fabric', "
                    "'{}', :slug, 'stopped', 'unknown', now(), now())"
                ),
                {
                    "id": server_id,
                    "community_id": community_id,
                    "name": "survival",
                    "slug": f"survival-{str(server_id)[:8]}-00",
                },
            )
            stored = await conn.scalar(
                text("SELECT server_type FROM server WHERE id = :id"),
                {"id": server_id},
            )
            assert stored == "fabric"
            # Remove the fabric row before teardown: ``downgrade_base`` walks the
            # 0010 downgrade (which restores the 3-value CHECK) before 0005 drops
            # the table, and that re-add would reject a lingering fabric row.
            await conn.execute(text("DELETE FROM server"))
    finally:
        await engine.dispose()
        await downgrade_base(_DB_URL)
