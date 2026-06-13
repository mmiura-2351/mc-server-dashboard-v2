"""Round-trip for the 0017 game_session migration + session:read backfill.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Asserts the ``game_session`` table is
created with the documented shape (RELAY.md Section 14) and dropped on downgrade,
and that ``session:read`` is appended to preset Owner roles only (mirroring the
0008 backfill round-trip), idempotently and reversibly (issue #957).
"""

from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from tests.integration.migrate import (
    downgrade_base,
    downgrade_to,
    upgrade_to,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 12, 12, 0, tzinfo=dt.timezone.utc)
# An old-style Owner permission set (missing session:read).
_OLD_OWNER_PERMS = ["server:create", "server:read", "community:read"]


async def _table_exists(conn: AsyncConnection, name: str) -> bool:
    result = await conn.execute(
        text("SELECT to_regclass(:name)"), {"name": f"public.{name}"}
    )
    return result.scalar_one() is not None


async def _seed_roles(
    conn: AsyncConnection, community_id: uuid.UUID
) -> dict[str, uuid.UUID]:
    await conn.execute(
        text(
            "INSERT INTO community (id, name, created_at, updated_at) "
            "VALUES (:id, :name, :at, :at)"
        ),
        {"id": community_id, "name": "guild", "at": _NOW},
    )
    ids = {
        "owner": uuid.uuid4(),
        "custom": uuid.uuid4(),
        "other_preset": uuid.uuid4(),
    }
    rows = [
        (ids["owner"], "Owner", _OLD_OWNER_PERMS, True),
        (ids["custom"], "CustomOwner", _OLD_OWNER_PERMS, False),
        (ids["other_preset"], "Moderator", _OLD_OWNER_PERMS, True),
    ]
    for role_id, name, perms, is_preset in rows:
        await conn.execute(
            text(
                "INSERT INTO role "
                "(id, community_id, name, permissions, is_preset, "
                "created_at, updated_at) "
                "VALUES (:id, :cid, :name, :perms, :preset, :at, :at)"
            ),
            {
                "id": role_id,
                "cid": community_id,
                "name": name,
                "perms": perms,
                "preset": is_preset,
                "at": _NOW,
            },
        )
    return ids


async def _perms(conn: AsyncConnection, role_id: uuid.UUID) -> list[str]:
    result = await conn.execute(
        text("SELECT permissions FROM role WHERE id = :id"), {"id": role_id}
    )
    return list(result.scalar_one())


async def test_upgrade_creates_table_and_backfills_session_read() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_to("0016_relay_ingress", _DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        community_id = uuid.uuid4()
        async with engine.begin() as conn:
            assert not await _table_exists(conn, "game_session")
            ids = await _seed_roles(conn, community_id)

        await upgrade_to("0017_game_session", _DB_URL)

        async with engine.connect() as conn:
            assert await _table_exists(conn, "game_session")
            # Backfill: preset Owner only.
            assert "session:read" in await _perms(conn, ids["owner"])
            assert "session:read" not in await _perms(conn, ids["custom"])
            assert "session:read" not in await _perms(conn, ids["other_preset"])

        # Idempotent: re-running upgrade adds no duplicate.
        await downgrade_to("0016_relay_ingress", _DB_URL)
        await upgrade_to("0017_game_session", _DB_URL)
        async with engine.connect() as conn:
            assert (await _perms(conn, ids["owner"])).count("session:read") == 1

        # Downgrade drops the table and strips the code.
        await downgrade_to("0016_relay_ingress", _DB_URL)
        async with engine.connect() as conn:
            assert not await _table_exists(conn, "game_session")
            assert "session:read" not in await _perms(conn, ids["owner"])
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)
