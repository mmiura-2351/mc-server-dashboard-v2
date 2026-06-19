"""Migration round-trip for the ``mods`` table against real PostgreSQL.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Proves migration 0019 upgrades on top
of 0018 and downgrades cleanly, that the ``mods`` table exists after upgrade, and
that the ``sha256_hash`` unique index rejects a duplicate content address
(issue #1259).
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_INSERT = text(
    "INSERT INTO mods (id, filename, display_name, loader_type, mod_identifier, "
    "provides, version_number, mc_versions, side, dependencies, sha256_hash, "
    "size_bytes, source, uploaded_by, created_at, updated_at) VALUES "
    "(:id, 'sodium.jar', 'Sodium', 'fabric', 'sodium', '[]', '0.5.0', "
    "'[\"1.21\"]', 'both', '[]', :sha256, 2048, 'local', :uploaded_by, now(), now())"
)


async def test_upgrade_creates_mods_then_downgrade_drops_it() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
            assert "mods" in tables
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
            assert "mods" not in tables
            assert "alembic_version" in tables
    finally:
        await engine.dispose()


async def test_sha256_unique_index_rejects_duplicate() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        sha256 = "a" * 64
        uploaded_by = uuid.uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                _INSERT,
                {"id": uuid.uuid4(), "sha256": sha256, "uploaded_by": uploaded_by},
            )
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    _INSERT,
                    {"id": uuid.uuid4(), "sha256": sha256, "uploaded_by": uploaded_by},
                )
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM mods"))
    finally:
        await engine.dispose()
        await downgrade_base(_DB_URL)
