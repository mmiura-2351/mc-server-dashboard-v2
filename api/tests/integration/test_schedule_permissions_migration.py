"""Round-trip for the 0030 backfill of the schedule permissions onto Owner roles.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Seeds an old-style preset Owner role
(its permission array missing ``schedule:read`` / ``schedule:manage``) at revision
0029, then upgrades to 0030 and asserts the codes were appended; also asserts the
migration leaves a non-preset role and a non-Owner preset role untouched, and that
downgrade strips the codes from the preset Owner role again (issue #1837).
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

_NOW = dt.datetime(2026, 7, 11, 12, 0, tzinfo=dt.timezone.utc)
# An old-style Owner permission set (missing the schedule codes).
_OLD_OWNER_PERMS = ["server:create", "server:read", "community:read"]
_NEW_PERMS = ("schedule:read", "schedule:manage")


async def _seed(conn: AsyncConnection, community_id: uuid.UUID) -> dict[str, uuid.UUID]:
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
        # The target: a preset Owner role missing the schedule codes.
        (owner_id, "Owner", _OLD_OWNER_PERMS, True),
        # A non-preset role named Owner: must be left untouched.
        (custom_id, "CustomOwner", _OLD_OWNER_PERMS, False),
        # A preset role with a different name: must be left untouched.
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


async def test_backfill_adds_schedule_codes_to_preset_owner_only() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_to("0029_schedules", _DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        community_id = uuid.uuid4()
        async with engine.begin() as conn:
            ids = await _seed(conn, community_id)

        # Apply the backfill.
        await upgrade_to("0030_schedule_permissions", _DB_URL)

        async with engine.connect() as conn:
            owner_perms = await _perms(conn, ids["owner"])
            assert set(_NEW_PERMS) <= set(owner_perms)
            assert set(_NEW_PERMS).isdisjoint(await _perms(conn, ids["custom"]))
            assert set(_NEW_PERMS).isdisjoint(await _perms(conn, ids["other_preset"]))

        # Idempotent: re-running upgrade adds no duplicate.
        await downgrade_to("0029_schedules", _DB_URL)
        await upgrade_to("0030_schedule_permissions", _DB_URL)
        async with engine.connect() as conn:
            owner_perms = await _perms(conn, ids["owner"])
        for perm in _NEW_PERMS:
            assert owner_perms.count(perm) == 1

        # Downgrade strips them from the preset Owner role again.
        await downgrade_to("0029_schedules", _DB_URL)
        async with engine.connect() as conn:
            assert set(_NEW_PERMS).isdisjoint(await _perms(conn, ids["owner"]))
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)
