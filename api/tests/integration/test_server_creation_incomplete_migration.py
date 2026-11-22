"""Round-trip for the 0049 server creation-incomplete migration (issue #4065).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Verifies that:

- upgrade adds the NOT NULL ``creation_incomplete`` column, an existing row
  reading ``false``: no row before the column is marked, startable as before;
- a row inserted without the column reads ``false`` too;
- downgrade drops the column again.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from tests.integration.migrate import downgrade_base, downgrade_to, upgrade_to

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_PRE_MIGRATION = "0048_final_snapshot_refusal"
_MIGRATION = "0049_server_creation_incomplete"

_COLUMNS = text(
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_name = 'server' AND column_name = 'creation_incomplete'"
)


async def _seed_server(conn: AsyncConnection, community_id: uuid.UUID) -> uuid.UUID:
    server_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO server (id, community_id, name, mc_edition, "
            "mc_version, server_type, config, slug, desired_state, "
            "observed_state, created_at, updated_at) VALUES "
            "(:id, :community_id, :name, 'java', '1.21.1', 'vanilla', "
            "'{}', :slug, 'stopped', 'stopped', now(), now())"
        ),
        {
            "id": server_id,
            "community_id": community_id,
            "name": f"survival-{str(server_id)[:8]}",
            "slug": f"survival-{str(server_id)[:8]}",
        },
    )
    return server_id


async def _flag(conn: AsyncConnection, server_id: uuid.UUID) -> object:
    return (
        await conn.execute(
            text("SELECT creation_incomplete FROM server WHERE id = :id"),
            {"id": server_id},
        )
    ).scalar_one()


async def test_server_creation_incomplete_migration_round_trip() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    engine = create_async_engine(_DB_URL)
    try:
        community_id = uuid.uuid4()
        await upgrade_to(_PRE_MIGRATION, _DB_URL)
        async with engine.begin() as conn:
            assert (await conn.execute(_COLUMNS)).scalars().all() == []
            await conn.execute(
                text(
                    "INSERT INTO community (id, name, created_at, updated_at) "
                    "VALUES (:id, 'guild', now(), now())"
                ),
                {"id": community_id},
            )
            existing = await _seed_server(conn, community_id)

        await upgrade_to(_MIGRATION, _DB_URL)
        async with engine.begin() as conn:
            assert (await conn.execute(_COLUMNS)).scalars().all() == [
                "creation_incomplete"
            ]
            assert await _flag(conn, existing) is False
            inserted = await _seed_server(conn, community_id)
            assert await _flag(conn, inserted) is False

        await downgrade_to(_PRE_MIGRATION, _DB_URL)
        async with engine.connect() as conn:
            assert (await conn.execute(_COLUMNS)).scalars().all() == []
    finally:
        await engine.dispose()
        await downgrade_base(_DB_URL)
