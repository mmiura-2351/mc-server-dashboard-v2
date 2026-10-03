"""Round-trip for the 0039 ``resource_grant`` parent-FK migration (issue #3216).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Verifies that:

- upgrade deletes the ghost grants the race could leave behind -- one whose
  membership is gone and one whose server is gone -- and keeps a valid grant,
  backfilling its ``membership_id`` from the membership of its pair;
- after upgrade both relationships are enforced foreign keys;
- downgrade drops the foreign keys and the column again.
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

_PRE_MIGRATION = "0038_backup_health_unreadable"
_MIGRATION = "0039_resource_grant_parent_fks"


async def _insert_user(conn: AsyncConnection, username: str) -> uuid.UUID:
    user_id = uuid.uuid4()
    await conn.execute(
        text(
            'INSERT INTO "user" '
            "(id, username, email, password_hash, is_platform_admin, "
            "created_at, updated_at) VALUES "
            "(:id, :username, :email, 'h', false, now(), now())"
        ),
        {"id": user_id, "username": username, "email": f"{username}@e.com"},
    )
    return user_id


async def _insert_grant(
    conn: AsyncConnection,
    user_id: uuid.UUID,
    community_id: uuid.UUID,
    resource_id: uuid.UUID,
    membership_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert a grant; ``membership_id`` is the post-0039 column, else omitted."""

    grant_id = uuid.uuid4()
    columns = "id, user_id, community_id, resource_type, resource_id, permissions"
    values = ":id, :uid, :cid, 'server', :rid, ARRAY['server:start']"
    if membership_id is not None:
        columns += ", membership_id"
        values += ", :mid"
    await conn.execute(
        text(
            f"INSERT INTO resource_grant ({columns}, created_at, updated_at) "
            f"VALUES ({values}, now(), now())"
        ),
        {
            "id": grant_id,
            "uid": user_id,
            "cid": community_id,
            "rid": resource_id,
            "mid": membership_id,
        },
    )
    return grant_id


async def _grant_ids(conn: AsyncConnection) -> set[uuid.UUID]:
    rows = await conn.execute(text("SELECT id FROM resource_grant"))
    return {row[0] for row in rows}


async def test_upgrade_deletes_ghost_grants_and_enforces_both_parents() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_to(_PRE_MIGRATION, _DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            member = await _insert_user(conn, "member")
            removed = await _insert_user(conn, "removed")
            community_id = uuid.uuid4()
            membership_id = uuid.uuid4()
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
                    "INSERT INTO membership (id, user_id, community_id, created_at) "
                    "VALUES (:id, :uid, :cid, now())"
                ),
                {"id": membership_id, "uid": member, "cid": community_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO server "
                    "(id, community_id, name, mc_edition, mc_version, server_type, "
                    "config, slug, desired_state, observed_state, "
                    "created_at, updated_at) VALUES "
                    "(:id, :cid, 'survival', 'java', '1.21', 'vanilla', "
                    "'{}'::jsonb, :slug, 'stopped', 'stopped', now(), now())"
                ),
                {
                    "id": server_id,
                    "cid": community_id,
                    "slug": f"srv-{str(server_id)[:8]}-00",
                },
            )
            valid = await _insert_grant(conn, member, community_id, server_id)
            # The member-removal ghost: no membership for (removed, community).
            await _insert_grant(conn, removed, community_id, server_id)
            # The server-deletion ghost: no server row for the resource id.
            await _insert_grant(conn, member, community_id, uuid.uuid4())

        await upgrade_to(_MIGRATION, _DB_URL)

        async with engine.connect() as conn:
            assert await _grant_ids(conn) == {valid}
            assert (
                await conn.scalar(
                    text("SELECT membership_id FROM resource_grant WHERE id = :id"),
                    {"id": valid},
                )
                == membership_id
            )
        # A removed membership (here: an id that names none) and a deleted
        # server are both rejected.
        for grant_membership, resource_id in (
            (uuid.uuid4(), server_id),
            (membership_id, uuid.uuid4()),
        ):
            with pytest.raises(IntegrityError):
                async with engine.begin() as conn:
                    await _insert_grant(
                        conn, member, community_id, resource_id, grant_membership
                    )

        await downgrade_to(_PRE_MIGRATION, _DB_URL)

        # Without the foreign keys a ghost row is accepted again.
        async with engine.begin() as conn:
            await _insert_grant(conn, removed, community_id, uuid.uuid4())
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)
