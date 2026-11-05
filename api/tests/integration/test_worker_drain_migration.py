"""Round-trip for the 0043 ``worker_drain`` migration (issue #3535).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Verifies that upgrade creates the table
keyed by ``worker_id`` with a mandatory ``drained_at``, and that downgrade drops
it again.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.migrate import downgrade_base, downgrade_to, upgrade_to

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_PRE_MIGRATION = "0042_server_community_restrict"
_MIGRATION = "0043_worker_drain"

_EXISTS = text("SELECT to_regclass('public.worker_drain') IS NOT NULL")


async def test_worker_drain_migration_round_trip() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    engine = create_async_engine(_DB_URL)
    worker_id = uuid.uuid4()
    try:
        await upgrade_to(_PRE_MIGRATION, _DB_URL)
        async with engine.connect() as conn:
            assert (await conn.execute(_EXISTS)).scalar_one() is False

        await upgrade_to(_MIGRATION, _DB_URL)
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO worker_drain VALUES (:id, now())"), {"id": worker_id}
            )
        # One row per Worker: the primary key refuses a second.
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO worker_drain VALUES (:id, now())"),
                    {"id": worker_id},
                )
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO worker_drain (worker_id) VALUES (:id)"),
                    {"id": uuid.uuid4()},
                )

        await downgrade_to(_PRE_MIGRATION, _DB_URL)
        async with engine.connect() as conn:
            assert (await conn.execute(_EXISTS)).scalar_one() is False
    finally:
        await engine.dispose()
        await downgrade_base(_DB_URL)
