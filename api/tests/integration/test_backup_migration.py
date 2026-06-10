"""Migration round-trip for the ``backup`` table against real PostgreSQL.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Proves the 0006 migration upgrades and
downgrades cleanly and that the ``backup`` table exists after upgrade
(DATABASE.md Section 8).
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

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


async def test_upgrade_creates_backup_then_downgrade_drops_it() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
            assert "backup" in tables
            indexes = await conn.run_sync(
                lambda sync_conn: {
                    ix["name"] for ix in inspect(sync_conn).get_indexes("backup")
                }
            )
            assert "ix_backup_server_id_created_at" in indexes
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
            assert "backup" not in tables
            assert "alembic_version" in tables
    finally:
        await engine.dispose()


async def test_health_backfills_existing_rows_to_unknown() -> None:
    """The 0015 migration adds ``health`` NOT NULL and backfills legacy rows.

    A backup row that predates the health column (created at 0014) must land
    ``unknown`` after the upgrade -- the honest "not yet checked" state for rows
    older than any integrity check (issue #742).
    """

    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_to("0014_refresh_token_reason", _DB_URL)

    community_id = uuid.uuid4()
    server_id = uuid.uuid4()
    backup_id = uuid.uuid4()

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO community (id, name, created_at, updated_at) "
                    "VALUES (:id, :name, now(), now())"
                ),
                {"id": community_id, "name": "guild"},
            )
            await conn.execute(
                text(
                    "INSERT INTO server (id, community_id, name, mc_edition, "
                    "mc_version, server_type, execution_backend, config, "
                    "desired_state, observed_state, created_at, updated_at) VALUES "
                    "(:id, :community_id, :name, 'java', '1.21.1', 'vanilla', "
                    "'container', '{}', 'stopped', 'unknown', now(), now())"
                ),
                {"id": server_id, "community_id": community_id, "name": "survival"},
            )
            await conn.execute(
                text(
                    "INSERT INTO backup (id, server_id, storage_ref, source, "
                    "created_at) VALUES (:id, :server_id, 'ref', 'manual', now())"
                ),
                {"id": backup_id, "server_id": server_id},
            )
    finally:
        await engine.dispose()

    await upgrade_head(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            health = (
                await conn.execute(
                    text("SELECT health FROM backup WHERE id = :id"),
                    {"id": backup_id},
                )
            ).scalar_one()
            assert health == "unknown"
            # Remove the rows before teardown: ``downgrade_base`` walks 0015's
            # downgrade (drop the column) before 0005 drops the table.
            await conn.execute(text("DELETE FROM backup"))
            await conn.execute(text("DELETE FROM server"))
            await conn.execute(text("DELETE FROM community"))
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)


async def test_health_check_admits_allowed_values_after_upgrade() -> None:
    """The 0015 migration installs ``ck_backup_health`` over the three states."""

    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            clause = (
                await conn.execute(
                    text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname = 'ck_backup_health'"
                    )
                )
            ).scalar_one()
            assert "healthy" in clause
            assert "quarantined" in clause
            assert "unknown" in clause
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)


async def test_source_check_admits_uploaded_after_upgrade() -> None:
    """The 0013 migration widens ``ck_backup_source`` to include ``uploaded``."""

    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            clause = (
                await conn.execute(
                    text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname = 'ck_backup_source'"
                    )
                )
            ).scalar_one()
            assert "uploaded" in clause
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)


async def test_downgrade_remaps_uploaded_rows_before_narrowing_check() -> None:
    """0013's downgrade must not reject ``uploaded`` rows valid at head (issue #758).

    An ``uploaded`` backup is admissible at head; the downgrade re-narrows
    ``ck_backup_source`` to the 0006 three-value set, so it must first remap such
    rows to ``manual`` or the re-applied CHECK would fail mid-ALTER and poison the
    batched integration teardown.
    """

    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)

    community_id = uuid.uuid4()
    server_id = uuid.uuid4()
    backup_id = uuid.uuid4()

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO community (id, name, created_at, updated_at) "
                    "VALUES (:id, :name, now(), now())"
                ),
                {"id": community_id, "name": "guild"},
            )
            await conn.execute(
                text(
                    "INSERT INTO server (id, community_id, name, mc_edition, "
                    "mc_version, server_type, execution_backend, config, "
                    "desired_state, observed_state, created_at, updated_at) VALUES "
                    "(:id, :community_id, :name, 'java', '1.21.1', 'vanilla', "
                    "'container', '{}', 'stopped', 'unknown', now(), now())"
                ),
                {"id": server_id, "community_id": community_id, "name": "survival"},
            )
            await conn.execute(
                text(
                    "INSERT INTO backup (id, server_id, storage_ref, source, "
                    "health, created_at) VALUES (:id, :server_id, 'ref', "
                    "'uploaded', 'unknown', now())"
                ),
                {"id": backup_id, "server_id": server_id},
            )
    finally:
        await engine.dispose()

    # Walk 0013's downgrade (target 0012, just past it). The re-narrowed CHECK
    # would reject the ``uploaded`` row unless the downgrade remapped it first.
    await downgrade_to("0012_player_groups", _DB_URL)

    await upgrade_head(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            source = (
                await conn.execute(
                    text("SELECT source FROM backup WHERE id = :id"),
                    {"id": backup_id},
                )
            ).scalar_one()
            assert source == "manual"
            await conn.execute(text("DELETE FROM backup"))
            await conn.execute(text("DELETE FROM server"))
            await conn.execute(text("DELETE FROM community"))
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)
