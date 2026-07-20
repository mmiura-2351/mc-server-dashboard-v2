"""Round-trip for the 0036 floodgate slug rewrite migration (issue #2145).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Verifies that:

- GeyserMC-sourced ``source_project_id='floodgate'`` rows are rewritten to
  ``'geysermc-floodgate'`` on upgrade.
- Modrinth-sourced ``source_project_id='floodgate'`` rows are NOT touched.
- Downgrade restores the original slug for GeyserMC rows.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.migrate import (
    downgrade_base,
    downgrade_to,
    upgrade_to,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.timezone.utc)
_PRE_MIGRATION = "0035_plugin_source_unknown"
_MIGRATION = "0036_floodgate_slug_rewrite"


async def test_upgrade_rewrites_geyser_slug_and_leaves_modrinth() -> None:
    """GeyserMC rows get the new slug; Modrinth rows stay untouched."""
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_to(_PRE_MIGRATION, _DB_URL)

    engine = create_async_engine(_DB_URL)
    community_id = uuid.uuid4()
    server_id = uuid.uuid4()
    geyser_plugin_id = uuid.uuid4()
    modrinth_plugin_id = uuid.uuid4()

    try:
        # Seed a community, server, and two plugin rows with the old slug.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO community (id, name, created_at, updated_at) "
                    "VALUES (:id, :name, :at, :at)"
                ),
                {"id": community_id, "name": "guild", "at": _NOW},
            )
            await conn.execute(
                text(
                    "INSERT INTO server "
                    "(id, community_id, name, mc_edition, mc_version, "
                    "server_type, config, slug, desired_state, observed_state, "
                    "created_at, updated_at) "
                    "VALUES (:id, :cid, :name, 'java', '1.21.1', 'paper', "
                    "'{}', :slug, 'stopped', 'unknown', :at, :at)"
                ),
                {
                    "id": server_id,
                    "cid": community_id,
                    "name": "survival",
                    "slug": f"survival-{str(server_id)[:8]}-00",
                    "at": _NOW,
                },
            )
            # GeyserMC-sourced Floodgate (should be rewritten).
            await conn.execute(
                text(
                    "INSERT INTO server_plugin "
                    "(id, server_id, rel_path, filename, display_name, "
                    "loader_type, source, source_project_id, "
                    "created_at, updated_at) "
                    "VALUES (:id, :sid, :path, :fn, :dn, "
                    "'plugin', 'geyser', 'floodgate', :at, :at)"
                ),
                {
                    "id": geyser_plugin_id,
                    "sid": server_id,
                    "path": "plugins/floodgate-spigot.jar",
                    "fn": "floodgate-spigot.jar",
                    "dn": "Floodgate",
                    "at": _NOW,
                },
            )
            # Modrinth-sourced Floodgate (must NOT be touched).
            await conn.execute(
                text(
                    "INSERT INTO server_plugin "
                    "(id, server_id, rel_path, filename, display_name, "
                    "loader_type, source, source_project_id, "
                    "created_at, updated_at) "
                    "VALUES (:id, :sid, :path, :fn, :dn, "
                    "'mod', 'modrinth', 'floodgate', :at, :at)"
                ),
                {
                    "id": modrinth_plugin_id,
                    "sid": server_id,
                    "path": "mods/floodgate-fabric.jar",
                    "fn": "floodgate-fabric.jar",
                    "dn": "Floodgate",
                    "at": _NOW,
                },
            )

        # Run the migration.
        await upgrade_to(_MIGRATION, _DB_URL)

        async with engine.connect() as conn:
            geyser_slug = await conn.scalar(
                text("SELECT source_project_id FROM server_plugin WHERE id = :id"),
                {"id": geyser_plugin_id},
            )
            modrinth_slug = await conn.scalar(
                text("SELECT source_project_id FROM server_plugin WHERE id = :id"),
                {"id": modrinth_plugin_id},
            )
            assert geyser_slug == "geysermc-floodgate"
            assert modrinth_slug == "floodgate"

        # Downgrade restores the old slug for GeyserMC rows only.
        await downgrade_to(_PRE_MIGRATION, _DB_URL)

        async with engine.connect() as conn:
            geyser_slug = await conn.scalar(
                text("SELECT source_project_id FROM server_plugin WHERE id = :id"),
                {"id": geyser_plugin_id},
            )
            modrinth_slug = await conn.scalar(
                text("SELECT source_project_id FROM server_plugin WHERE id = :id"),
                {"id": modrinth_plugin_id},
            )
            assert geyser_slug == "floodgate"
            assert modrinth_slug == "floodgate"
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)
