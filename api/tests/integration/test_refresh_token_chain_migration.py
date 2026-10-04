"""Round-trip for the 0040 ``refresh_token.chain_id`` migration (issue #3249).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Verifies that:

- upgrade backfills every existing token as its own chain (``chain_id = id``)
  and makes the column mandatory;
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

_PRE_MIGRATION = "0039_resource_grant_parent_fks"
_MIGRATION = "0040_refresh_token_chain"


async def _insert_token(
    conn: AsyncConnection, user_id: uuid.UUID, token_hash: str
) -> uuid.UUID:
    token_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO refresh_token "
            "(id, user_id, token_hash, issued_at, expires_at) VALUES "
            "(:id, :uid, :hash, now(), now() + interval '14 days')"
        ),
        {"id": token_id, "uid": user_id, "hash": token_hash},
    )
    return token_id


async def test_upgrade_backfills_each_token_as_its_own_chain() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_to(_PRE_MIGRATION, _DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            user_id = uuid.uuid4()
            await conn.execute(
                text(
                    'INSERT INTO "user" '
                    "(id, username, email, password_hash, is_platform_admin, "
                    "created_at, updated_at) VALUES "
                    "(:id, 'alice', 'alice@e.com', 'h', false, now(), now())"
                ),
                {"id": user_id},
            )
            first = await _insert_token(conn, user_id, "first")
            second = await _insert_token(conn, user_id, "second")

        await upgrade_to(_MIGRATION, _DB_URL)

        async with engine.connect() as conn:
            rows = await conn.execute(text("SELECT id, chain_id FROM refresh_token"))
            assert {row[0]: row[1] for row in rows} == {first: first, second: second}
        # A token without a chain is rejected.
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await _insert_token(conn, user_id, "chainless")

        await downgrade_to(_PRE_MIGRATION, _DB_URL)

        async with engine.connect() as conn:
            columns = await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'refresh_token'"
                )
            )
            assert "chain_id" not in {row[0] for row in columns}
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)
