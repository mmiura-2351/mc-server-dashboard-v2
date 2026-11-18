"""The database's installation id (issue #3870).

The 0045 migration creates the one-row ``installation`` table and mints the id the
store owner marker records, so a bucket can tell which database it belongs to.
The id is created once and never changes: every read returns the same value, and
the table admits no second row.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5).
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.core.adapters.installation import read_installation_id
from tests.integration.migrate import (
    downgrade_base,
    downgrade_to,
    upgrade_head,
    upgrade_to,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_PRE_MIGRATION = "0044_schedule_run_unknown"
_MIGRATION = "0045_installation"

_EXISTS = text("SELECT to_regclass('public.installation') IS NOT NULL")


async def test_installation_migration_mints_one_id_and_admits_no_second() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    engine = create_async_engine(_DB_URL)
    try:
        await upgrade_to(_PRE_MIGRATION, _DB_URL)
        async with engine.connect() as conn:
            assert (await conn.execute(_EXISTS)).scalar_one() is False

        await upgrade_to(_MIGRATION, _DB_URL)
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT id FROM installation"))).all()
        assert len(rows) == 1
        assert isinstance(rows[0].id, uuid.UUID)

        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO installation (singleton, id, created_at) "
                        "VALUES (true, :id, now())"
                    ),
                    {"id": uuid.uuid4()},
                )
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO installation (singleton, id, created_at) "
                        "VALUES (false, :id, now())"
                    ),
                    {"id": uuid.uuid4()},
                )

        await downgrade_to(_PRE_MIGRATION, _DB_URL)
        async with engine.connect() as conn:
            assert (await conn.execute(_EXISTS)).scalar_one() is False
    finally:
        await engine.dispose()
        await downgrade_base(_DB_URL)


async def test_read_installation_id_returns_the_migrated_id_every_time() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            minted = (
                await conn.execute(text("SELECT id FROM installation"))
            ).scalar_one()
        session_factory = create_session_factory(engine)

        assert await read_installation_id(session_factory) == minted
        assert await read_installation_id(session_factory) == minted
    finally:
        await engine.dispose()
        await downgrade_base(_DB_URL)
