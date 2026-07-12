"""Data-migration round-trip for the FR-BAK-3 backup-cadence cutover (#1840).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Proves 0031 converts each server's
``backup_interval_hours`` config key into an equivalent enabled ``backup``
schedule (setting ``next_run_at`` within roughly an hour so the runner picks it
up), strips the key, is collision-safe against a schedule the operator already
named ``"Scheduled backup"``, and reverses cleanly on downgrade.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from typing import Any, cast

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

_AT_0030 = "0030_schedule_permissions"
_AT_0031 = "0031_retire_backup_interval"


async def _seed_server(conn: AsyncConnection, *, name: str, config: str) -> uuid.UUID:
    community_id = uuid.uuid4()
    server_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO community (id, name, created_at, updated_at) "
            "VALUES (:id, :name, now(), now())"
        ),
        {"id": community_id, "name": f"guild-{str(community_id)[:8]}"},
    )
    await conn.execute(
        text(
            "INSERT INTO server (id, community_id, name, mc_edition, mc_version, "
            "server_type, config, slug, desired_state, observed_state, created_at, "
            "updated_at) VALUES (:id, :community_id, :name, 'java', '1.21.1', "
            "'vanilla', CAST(:config AS jsonb), :slug, 'stopped', 'unknown', "
            "now(), now())"
        ),
        {
            "id": server_id,
            "community_id": community_id,
            "name": name,
            "config": config,
            "slug": f"{name}-{str(server_id)[:8]}-00",
        },
    )
    return server_id


async def _config(conn: AsyncConnection, server_id: uuid.UUID) -> dict[str, Any]:
    row = (
        await conn.execute(
            text("SELECT config FROM server WHERE id = :id"), {"id": server_id}
        )
    ).scalar_one()
    return cast("dict[str, Any]", row)


async def test_upgrade_converts_key_into_schedule_then_downgrade_restores() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_to(_AT_0030, _DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        # Server A carries the retired key with no pre-existing schedule.
        # Server B carries it AND already has a schedule named "Scheduled backup"
        # (a non-backup one, so downgrade leaves it alone) — the collision case.
        # Server C carries an ordinary override and must be untouched.
        async with engine.begin() as conn:
            server_a = await _seed_server(
                conn, name="alpha", config='{"backup_interval_hours": 6}'
            )
            server_b = await _seed_server(
                conn, name="bravo", config='{"backup_interval_hours": 3, "motd": "hi"}'
            )
            server_c = await _seed_server(conn, name="charlie", config='{"motd": "hi"}')
            await conn.execute(
                text(
                    "INSERT INTO schedule (id, server_id, name, action, payload, "
                    "cron, interval_seconds, enabled, created_at, updated_at) VALUES "
                    "(:id, :sid, 'Scheduled backup', 'command', "
                    "'{\"command\": \"say hi\"}'::jsonb, '0 3 * * *', NULL, true, "
                    "now(), now())"
                ),
                {"id": uuid.uuid4(), "sid": server_b},
            )
            t0 = (await conn.execute(text("SELECT now()"))).scalar_one()
    finally:
        await engine.dispose()

    await upgrade_to(_AT_0031, _DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            t1 = (await conn.execute(text("SELECT now()"))).scalar_one()
            # Server A: key stripped, one enabled backup schedule named
            # "Scheduled backup", interval 6h, next_run_at within ~1h.
            assert "backup_interval_hours" not in await _config(conn, server_a)
            row_a = (
                await conn.execute(
                    text(
                        "SELECT name, action, interval_seconds, cron, enabled, "
                        "next_run_at FROM schedule WHERE server_id = :sid"
                    ),
                    {"sid": server_a},
                )
            ).one()
            assert row_a.name == "Scheduled backup"
            assert row_a.action == "backup"
            assert row_a.interval_seconds == 6 * 3600
            assert row_a.cron is None
            assert row_a.enabled is True
            assert row_a.next_run_at is not None
            # Due within the next hour of the migration, not a full interval out.
            assert t0 <= row_a.next_run_at < t1 + dt.timedelta(seconds=3600)

            # Server B: key stripped; the migrated schedule dodged the name
            # collision, so the pre-existing "Scheduled backup" survives and the
            # backup one is "Scheduled backup 2".
            assert "backup_interval_hours" not in await _config(conn, server_b)
            names_b = {
                r.name: r.action
                for r in await conn.execute(
                    text("SELECT name, action FROM schedule WHERE server_id = :sid"),
                    {"sid": server_b},
                )
            }
            assert names_b == {
                "Scheduled backup": "command",
                "Scheduled backup 2": "backup",
            }
            interval_b = (
                await conn.execute(
                    text(
                        "SELECT interval_seconds FROM schedule "
                        "WHERE server_id = :sid AND action = 'backup'"
                    ),
                    {"sid": server_b},
                )
            ).scalar_one()
            assert interval_b == 3 * 3600

            # Server C: no backup key, so no schedule and config untouched.
            assert await _config(conn, server_c) == {"motd": "hi"}
            count_c = (
                await conn.execute(
                    text("SELECT count(*) FROM schedule WHERE server_id = :sid"),
                    {"sid": server_c},
                )
            ).scalar_one()
            assert count_c == 0
    finally:
        await engine.dispose()

    await downgrade_to(_AT_0030, _DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.connect() as conn:
            # The key is reconstructed and the migrated backup schedules are gone;
            # the pre-existing command schedule on B is left intact.
            assert (await _config(conn, server_a))["backup_interval_hours"] == 6
            assert (await _config(conn, server_b))["backup_interval_hours"] == 3
            assert await _config(conn, server_c) == {"motd": "hi"}
            remaining = {
                r.name: r.action
                for r in await conn.execute(
                    text("SELECT name, action FROM schedule WHERE server_id = :sid"),
                    {"sid": server_b},
                )
            }
            assert remaining == {"Scheduled backup": "command"}
            assert (
                await conn.execute(
                    text("SELECT count(*) FROM schedule WHERE server_id = :sid"),
                    {"sid": server_a},
                )
            ).scalar_one() == 0
    finally:
        await engine.dispose()

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM schedule"))
            await conn.execute(text("DELETE FROM server"))
            await conn.execute(text("DELETE FROM community"))
    finally:
        await engine.dispose()
    await downgrade_base(_DB_URL)
