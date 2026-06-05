"""Round-trip for the 0012 player-group migration + Owner backfill (issue #276).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Asserts the three tables exist at head
and are dropped on downgrade, and that the upgrade backfills the two new
permission codes onto an existing preset Owner role (seeded at 0011_user_active,
missing the codes) while leaving a non-preset and a non-Owner preset role
untouched; downgrade strips them again (mirrors 0008's audit:read backfill test).
"""

from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from tests.integration.migrate import downgrade_to, upgrade_to

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 5, 12, 0, tzinfo=dt.timezone.utc)
_GROUP_TABLES = {"player_group", "group_player", "server_group"}
_NEW_PERMISSIONS = ("group:read", "group:manage")
# An old-style Owner permission set without the new group codes.
_OLD_OWNER_PERMS = ["server:create", "server:read", "community:read"]


async def _seed_owner_roles(
    conn: AsyncConnection, community_id: uuid.UUID
) -> dict[str, uuid.UUID]:
    await conn.execute(
        text(
            "INSERT INTO community (id, name, created_at, updated_at) "
            "VALUES (:id, :name, :at, :at)"
        ),
        {"id": community_id, "name": "guild", "at": _NOW},
    )
    owner_id = uuid.uuid4()
    custom_id = uuid.uuid4()
    other_preset_id = uuid.uuid4()
    rows = [
        (owner_id, "Owner", _OLD_OWNER_PERMS, True),
        (custom_id, "CustomOwner", _OLD_OWNER_PERMS, False),
        (other_preset_id, "Moderator", _OLD_OWNER_PERMS, True),
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
    return {"owner": owner_id, "custom": custom_id, "other_preset": other_preset_id}


async def _perms(conn: AsyncConnection, role_id: uuid.UUID) -> list[str]:
    result = await conn.execute(
        text("SELECT permissions FROM role WHERE id = :id"), {"id": role_id}
    )
    return list(result.scalar_one())


async def test_group_tables_created_and_dropped() -> None:
    assert _DB_URL is not None
    # Establish a known state first (mirrors the other migration tests): a clean
    # run has no ``alembic_version`` row, so ``downgrade_to`` would have no
    # current head to descend from and Alembic would raise CommandError.
    await downgrade_to("base", _DB_URL)
    await upgrade_to("0012_player_groups", _DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda sc: set(inspect(sc).get_table_names()))
        assert _GROUP_TABLES <= tables

        await downgrade_to("0011_user_active", _DB_URL)
        async with engine.connect() as conn:
            tables_after = await conn.run_sync(
                lambda sc: set(inspect(sc).get_table_names())
            )
        assert not (_GROUP_TABLES & tables_after)
    finally:
        await engine.dispose()
    await downgrade_to("base", _DB_URL)


async def test_backfill_adds_group_codes_to_preset_owner_only() -> None:
    assert _DB_URL is not None
    await downgrade_to("base", _DB_URL)
    await upgrade_to("0011_user_active", _DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        community_id = uuid.uuid4()
        async with engine.begin() as conn:
            ids = await _seed_owner_roles(conn, community_id)

        await upgrade_to("0012_player_groups", _DB_URL)

        async with engine.connect() as conn:
            owner_perms = await _perms(conn, ids["owner"])
            custom_perms = await _perms(conn, ids["custom"])
            other_perms = await _perms(conn, ids["other_preset"])
        for permission in _NEW_PERMISSIONS:
            assert permission in owner_perms
            assert permission not in custom_perms
            assert permission not in other_perms

        # Idempotent: re-running adds no duplicate.
        await downgrade_to("0011_user_active", _DB_URL)
        await upgrade_to("0012_player_groups", _DB_URL)
        async with engine.connect() as conn:
            owner_perms = await _perms(conn, ids["owner"])
        for permission in _NEW_PERMISSIONS:
            assert owner_perms.count(permission) == 1

        # Downgrade strips the codes from the preset Owner role again.
        await downgrade_to("0011_user_active", _DB_URL)
        async with engine.connect() as conn:
            owner_perms = await _perms(conn, ids["owner"])
        for permission in _NEW_PERMISSIONS:
            assert permission not in owner_perms
    finally:
        await engine.dispose()

    await downgrade_to("base", _DB_URL)
