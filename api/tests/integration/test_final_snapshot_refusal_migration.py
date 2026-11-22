"""Round-trip for the 0048 server final-snapshot-refusal migration (issue #4054).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Verifies that:

- upgrade adds the nullable ``final_snapshot_refusal`` column, an existing
  assigned row reading NULL: no stop was held by a refusal before the column;
- ``ck_server_final_snapshot_refusal`` admits ``busy`` and ``unavailable`` and
  rejects any other value;
- downgrade drops the column again.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from tests.integration.migrate import downgrade_base, downgrade_to, upgrade_to

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_PRE_MIGRATION = "0047_server_awaiting_hydrate"
_MIGRATION = "0048_final_snapshot_refusal"

_COLUMNS = text(
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_name = 'server' AND column_name = 'final_snapshot_refusal'"
)


async def _seed_held_server(conn: AsyncConnection) -> uuid.UUID:
    community_id = uuid.uuid4()
    server_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO community (id, name, created_at, updated_at) "
            "VALUES (:id, 'guild', now(), now())"
        ),
        {"id": community_id},
    )
    await conn.execute(
        text(
            "INSERT INTO server (id, community_id, name, mc_edition, "
            "mc_version, server_type, config, slug, desired_state, "
            "observed_state, assigned_worker_id, created_at, updated_at) VALUES "
            "(:id, :community_id, 'survival', 'java', '1.21.1', 'vanilla', "
            "'{}', :slug, 'stopped', 'stopped', :worker, now(), now())"
        ),
        {
            "id": server_id,
            "community_id": community_id,
            "slug": f"survival-{str(server_id)[:8]}",
            "worker": uuid.uuid4(),
        },
    )
    return server_id


async def _set_refusal(conn: AsyncConnection, server_id: uuid.UUID, value: str) -> None:
    await conn.execute(
        text("UPDATE server SET final_snapshot_refusal = :value WHERE id = :id"),
        {"value": value, "id": server_id},
    )


async def test_server_final_snapshot_refusal_migration_round_trip() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    engine = create_async_engine(_DB_URL)
    try:
        await upgrade_to(_PRE_MIGRATION, _DB_URL)
        async with engine.begin() as conn:
            assert (await conn.execute(_COLUMNS)).scalars().all() == []
            server_id = await _seed_held_server(conn)

        await upgrade_to(_MIGRATION, _DB_URL)
        async with engine.begin() as conn:
            assert (await conn.execute(_COLUMNS)).scalars().all() == [
                "final_snapshot_refusal"
            ]
            stored = (
                await conn.execute(
                    text("SELECT final_snapshot_refusal FROM server WHERE id = :id"),
                    {"id": server_id},
                )
            ).scalar_one()
            assert stored is None
            for value in ("busy", "unavailable"):
                await _set_refusal(conn, server_id, value)
        with pytest.raises(IntegrityError, match="ck_server_final_snapshot_refusal"):
            async with engine.begin() as conn:
                await _set_refusal(conn, server_id, "transfer_failed")

        await downgrade_to(_PRE_MIGRATION, _DB_URL)
        async with engine.connect() as conn:
            assert (await conn.execute(_COLUMNS)).scalars().all() == []
    finally:
        await engine.dispose()
        await downgrade_base(_DB_URL)
