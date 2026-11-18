"""Round-trip for the 0046 server last-crash migration (issue #3281).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Verifies that:

- upgrade adds the nullable ``crash_reason`` and ``crash_detail`` columns, an
  existing row reading NULL in both (no backfill: the crash detail and reason
  were never stored, so there is nothing to carry over);
- ``ck_server_crash_reason`` admits each reason the Worker classifies and
  rejects any other value;
- downgrade drops both columns again.
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

_PRE_MIGRATION = "0045_installation"
_MIGRATION = "0046_server_last_crash"

_COLUMNS = text(
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_name = 'server' AND column_name LIKE 'crash_%' "
    "ORDER BY column_name"
)


async def _seed_crashed_server(conn: AsyncConnection) -> uuid.UUID:
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
            "observed_state, created_at, updated_at) VALUES "
            "(:id, :community_id, 'survival', 'java', '1.21.1', 'forge', "
            "'{}', :slug, 'running', 'crashed', now(), now())"
        ),
        {
            "id": server_id,
            "community_id": community_id,
            "slug": f"survival-{str(server_id)[:8]}",
        },
    )
    return server_id


async def _set_reason(conn: AsyncConnection, server_id: uuid.UUID, reason: str) -> None:
    await conn.execute(
        text("UPDATE server SET crash_reason = :reason WHERE id = :id"),
        {"reason": reason, "id": server_id},
    )


async def test_server_last_crash_migration_round_trip() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    engine = create_async_engine(_DB_URL)
    try:
        await upgrade_to(_PRE_MIGRATION, _DB_URL)
        async with engine.begin() as conn:
            assert (await conn.execute(_COLUMNS)).scalars().all() == []
            server_id = await _seed_crashed_server(conn)

        await upgrade_to(_MIGRATION, _DB_URL)
        async with engine.begin() as conn:
            assert (await conn.execute(_COLUMNS)).scalars().all() == [
                "crash_detail",
                "crash_reason",
            ]
            stored = (
                await conn.execute(
                    text(
                        "SELECT crash_reason, crash_detail FROM server WHERE id = :id"
                    ),
                    {"id": server_id},
                )
            ).one()
            assert tuple(stored) == (None, None)
            for reason in (
                "forge_install_failed",
                "forge_install_out_of_memory",
                "forge_install_java_incompatible",
            ):
                await _set_reason(conn, server_id, reason)
            await conn.execute(
                text("UPDATE server SET crash_detail = :detail WHERE id = :id"),
                {"detail": "installer ran out of memory", "id": server_id},
            )
        with pytest.raises(IntegrityError, match="ck_server_crash_reason"):
            async with engine.begin() as conn:
                await _set_reason(conn, server_id, "out_of_memory")

        await downgrade_to(_PRE_MIGRATION, _DB_URL)
        async with engine.connect() as conn:
            assert (await conn.execute(_COLUMNS)).scalars().all() == []
    finally:
        await engine.dispose()
        await downgrade_base(_DB_URL)
