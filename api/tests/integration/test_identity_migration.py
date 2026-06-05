"""Migration round-trip for the identity tables against real PostgreSQL.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Proves the 0002 migration upgrades and
downgrades cleanly and that the documented tables/indexes exist after upgrade
(DATABASE.md Section 4).
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)


async def test_upgrade_creates_tables_then_downgrade_drops_them() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
            assert {"user", "refresh_token"} <= tables

            indexes = await conn.run_sync(
                lambda sync_conn: {
                    ix["name"] for ix in inspect(sync_conn).get_indexes("refresh_token")
                }
            )
            assert "ix_refresh_token_user_id" in indexes
            assert "ix_refresh_token_expires_at" in indexes
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
            assert "user" not in tables
            assert "refresh_token" not in tables
            # The alembic bookkeeping table survives a downgrade-to-base; that
            # is expected and is not part of the application schema.
            assert "alembic_version" in tables
    finally:
        await engine.dispose()


async def test_user_active_column_present_and_defaults_true() -> None:
    # The 0011 migration adds ``user.active`` NOT NULL defaulting to ``true`` so
    # existing rows backfill active (#278). Insert a row without the column and
    # read it back to prove the server default applies.
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            columns = await conn.run_sync(
                lambda sync_conn: {
                    c["name"] for c in inspect(sync_conn).get_columns("user")
                }
            )
            assert "active" in columns
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    'INSERT INTO "user" '
                    "(id, username, email, password_hash, is_platform_admin, "
                    "created_at, updated_at) VALUES "
                    "(gen_random_uuid(), 'carol', 'c@example.com', 'h', false, "
                    "now(), now())"
                )
            )
        async with engine.connect() as conn:
            active = (
                await conn.execute(
                    text("SELECT active FROM \"user\" WHERE username = 'carol'")
                )
            ).scalar_one()
            assert active is True
    finally:
        await engine.dispose()
        await downgrade_base(_DB_URL)


async def test_case_insensitive_username_uniqueness_is_enforced() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    'INSERT INTO "user" '
                    "(id, username, email, password_hash, is_platform_admin, "
                    "created_at, updated_at) VALUES "
                    "(gen_random_uuid(), 'Alice', 'a@example.com', 'h', false, "
                    "now(), now())"
                )
            )
        with pytest.raises(Exception):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        'INSERT INTO "user" '
                        "(id, username, email, password_hash, is_platform_admin, "
                        "created_at, updated_at) VALUES "
                        "(gen_random_uuid(), 'alice', 'b@example.com', 'h', false, "
                        "now(), now())"
                    )
                )
    finally:
        await engine.dispose()
        await downgrade_base(_DB_URL)
