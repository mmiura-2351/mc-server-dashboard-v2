"""Migration round-trip for the ``server_mods`` table against real PostgreSQL.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Proves migration 0020 upgrades on top of
0019 and downgrades cleanly, that ``UNIQUE(server_id, mod_id)`` rejects a
duplicate assignment, and that deleting the server cascades to its assignments
(issue #1262).
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

_INSERT_MOD = text(
    "INSERT INTO mods (id, filename, display_name, loader_type, mod_identifier, "
    "provides, version_number, mc_versions, side, dependencies, sha256_hash, "
    "size_bytes, source, uploaded_by, created_at, updated_at) VALUES "
    "(:id, 'sodium.jar', 'Sodium', 'fabric', 'sodium', '[]', '0.5.0', "
    "'[\"1.21\"]', 'both', '[]', :sha256, 2048, 'local', :uploaded_by, now(), now())"
)

_INSERT_COMMUNITY = text(
    "INSERT INTO community (id, name, created_at, updated_at) "
    "VALUES (:id, :name, now(), now())"
)

_INSERT_SERVER = text(
    "INSERT INTO server (id, community_id, name, mc_edition, mc_version, "
    "server_type, execution_backend, config, slug, desired_state, observed_state, "
    "created_at, updated_at) VALUES "
    "(:id, :community_id, :name, 'java', '1.21.1', 'fabric', 'container', '{}', "
    ":slug, 'stopped', 'unknown', now(), now())"
)

_INSERT_ASSIGNMENT = text(
    "INSERT INTO server_mods (id, server_id, mod_id, enabled, assigned_by, "
    "created_at, updated_at) VALUES "
    "(:id, :server_id, :mod_id, true, :assigned_by, now(), now())"
)


async def _seed_server_and_mod(conn: object) -> tuple[uuid.UUID, uuid.UUID]:
    community_id = uuid.uuid4()
    server_id = uuid.uuid4()
    mod_id = uuid.uuid4()
    await conn.execute(_INSERT_COMMUNITY, {"id": community_id, "name": "guild"})  # type: ignore[attr-defined]
    await conn.execute(  # type: ignore[attr-defined]
        _INSERT_SERVER,
        {
            "id": server_id,
            "community_id": community_id,
            "name": "survival",
            "slug": f"survival-{str(server_id)[:8]}-00",
        },
    )
    await conn.execute(  # type: ignore[attr-defined]
        _INSERT_MOD,
        {"id": mod_id, "sha256": "a" * 64, "uploaded_by": uuid.uuid4()},
    )
    return server_id, mod_id


async def test_upgrade_creates_server_mods_then_downgrade_drops_it() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
            assert "server_mods" in tables
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            tables = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
            assert "server_mods" not in tables
            assert "alembic_version" in tables
    finally:
        await engine.dispose()


async def test_unique_server_mod_rejects_duplicate() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            server_id, mod_id = await _seed_server_and_mod(conn)
            await conn.execute(
                _INSERT_ASSIGNMENT,
                {
                    "id": uuid.uuid4(),
                    "server_id": server_id,
                    "mod_id": mod_id,
                    "assigned_by": uuid.uuid4(),
                },
            )
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    _INSERT_ASSIGNMENT,
                    {
                        "id": uuid.uuid4(),
                        "server_id": server_id,
                        "mod_id": mod_id,
                        "assigned_by": uuid.uuid4(),
                    },
                )
    finally:
        await engine.dispose()
        await downgrade_base(_DB_URL)


async def test_delete_server_cascades_to_assignments() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            server_id, mod_id = await _seed_server_and_mod(conn)
            await conn.execute(
                _INSERT_ASSIGNMENT,
                {
                    "id": uuid.uuid4(),
                    "server_id": server_id,
                    "mod_id": mod_id,
                    "assigned_by": uuid.uuid4(),
                },
            )
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM server WHERE id = :id"), {"id": server_id}
            )
            remaining = await conn.scalar(
                text("SELECT count(*) FROM server_mods WHERE server_id = :id"),
                {"id": server_id},
            )
            assert remaining == 0
            # Remove the fabric server-type artefacts before teardown (see
            # test_server_migration for why a lingering fabric row breaks 0010).
            await conn.execute(text("DELETE FROM mods"))
            await conn.execute(text("DELETE FROM community"))
    finally:
        await engine.dispose()
        await downgrade_base(_DB_URL)
