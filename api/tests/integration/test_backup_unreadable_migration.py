"""Round-trip for the 0038 backup ``unreadable`` health migration (issue #2374).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Verifies that:

- the widened ``ck_backup_health`` admits ``unreadable`` at head and still rejects
  a value outside the enum;
- upgrade leaves existing ``quarantined`` rows exactly as they are (owner decision:
  the next integrity sweep re-classifies them; rewriting them to ``unknown`` would
  make known-damaged backups look unexamined);
- downgrade remaps ``unreadable`` rows to ``quarantined`` — the verdict the sweep
  recorded for the same finding before this change — so the re-narrowed CHECK
  never rejects data that was valid at head.
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

_PRE_MIGRATION = "0037_backup_only_when_running"
_MIGRATION = "0038_backup_health_unreadable"


async def _seed_server(conn: AsyncConnection) -> uuid.UUID:
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
            "(:id, :community_id, 'survival', 'java', '1.21.1', 'vanilla', "
            "'{}', :slug, 'stopped', 'unknown', now(), now())"
        ),
        {
            "id": server_id,
            "community_id": community_id,
            "slug": f"survival-{str(server_id)[:8]}-00",
        },
    )
    return server_id


async def _insert_backup(
    conn: AsyncConnection, server_id: uuid.UUID, *, health: str
) -> uuid.UUID:
    backup_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO backup (id, server_id, storage_ref, source, health, "
            "created_at) VALUES (:id, :server_id, :ref, 'manual', :health, now())"
        ),
        {
            "id": backup_id,
            "server_id": server_id,
            "ref": str(backup_id),
            "health": health,
        },
    )
    return backup_id


async def _health(conn: AsyncConnection, backup_id: uuid.UUID) -> object:
    return await conn.scalar(
        text("SELECT health FROM backup WHERE id = :id"), {"id": backup_id}
    )


async def _cleanup(conn: AsyncConnection) -> None:
    await conn.execute(text("DELETE FROM backup"))
    await conn.execute(text("DELETE FROM server"))
    await conn.execute(text("DELETE FROM community"))


async def test_upgrade_leaves_quarantined_rows_untouched() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_to(_PRE_MIGRATION, _DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            server_id = await _seed_server(conn)
            quarantined = await _insert_backup(conn, server_id, health="quarantined")

        await upgrade_to(_MIGRATION, _DB_URL)

        async with engine.begin() as conn:
            assert await _health(conn, quarantined) == "quarantined"
            await _cleanup(conn)
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)


async def test_check_admits_unreadable_and_still_rejects_unknown_values() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_to(_MIGRATION, _DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            server_id = await _seed_server(conn)
            unreadable = await _insert_backup(conn, server_id, health="unreadable")
        async with engine.connect() as conn:
            with pytest.raises(IntegrityError):
                await _insert_backup(conn, server_id, health="missing")
        async with engine.begin() as conn:
            assert await _health(conn, unreadable) == "unreadable"
            await _cleanup(conn)
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)


async def test_downgrade_remaps_unreadable_rows_to_quarantined() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_to(_MIGRATION, _DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            server_id = await _seed_server(conn)
            unreadable = await _insert_backup(conn, server_id, health="unreadable")
            healthy = await _insert_backup(conn, server_id, health="healthy")

        await downgrade_to(_PRE_MIGRATION, _DB_URL)

        async with engine.begin() as conn:
            assert await _health(conn, unreadable) == "quarantined"
            assert await _health(conn, healthy) == "healthy"
            await _cleanup(conn)
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)
