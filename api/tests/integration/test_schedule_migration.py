"""Data-bearing schedule migration scenarios (issue #1835).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Exercises the action / outcome CHECK
enums, cron-XOR-interval CHECK, per-server name uniqueness, and timezone
default against migrated data.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.migrate import downgrade_base, upgrade_head

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)


async def _seed_server(engine_url: str) -> uuid.UUID:
    community_id = uuid.uuid4()
    server_id = uuid.uuid4()
    engine = create_async_engine(engine_url)
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
                    "mc_version, server_type, config, slug, desired_state, "
                    "observed_state, created_at, updated_at) VALUES "
                    "(:id, :community_id, :name, 'java', '1.21.1', 'vanilla', "
                    "'{}', :slug, 'stopped', 'unknown', now(), now())"
                ),
                {
                    "id": server_id,
                    "community_id": community_id,
                    "name": "survival",
                    "slug": f"survival-{str(server_id)[:8]}-00",
                },
            )
    finally:
        await engine.dispose()
    return server_id


_INSERT_SCHEDULE = text(
    "INSERT INTO schedule (id, server_id, name, action, payload, cron, "
    "interval_seconds, enabled, created_at, updated_at) VALUES "
    "(:id, :server_id, :name, :action, :payload, :cron, :interval_seconds, "
    "false, now(), now())"
)


async def test_checks_reject_bad_action_and_non_xor_cadence() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    server_id = await _seed_server(_DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        # An action outside the CHECK enum is rejected.
        async with engine.connect() as conn:
            with pytest.raises(IntegrityError):
                await conn.execute(
                    _INSERT_SCHEDULE,
                    {
                        "id": uuid.uuid4(),
                        "server_id": server_id,
                        "name": "bad-action",
                        "action": "explode",
                        "payload": "{}",
                        "cron": None,
                        "interval_seconds": 3600,
                    },
                )
        # Both cadence columns set violates the XOR CHECK; so do neither.
        for cron, interval in [("0 3 * * *", 3600), (None, None)]:
            async with engine.connect() as conn:
                with pytest.raises(IntegrityError):
                    await conn.execute(
                        _INSERT_SCHEDULE,
                        {
                            "id": uuid.uuid4(),
                            "server_id": server_id,
                            "name": "bad-cadence",
                            "action": "backup",
                            "payload": "{}",
                            "cron": cron,
                            "interval_seconds": interval,
                        },
                    )
        # timezone defaults to UTC at the column.
        schedule_id = uuid.uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                _INSERT_SCHEDULE,
                {
                    "id": schedule_id,
                    "server_id": server_id,
                    "name": "nightly",
                    "action": "backup",
                    "payload": "{}",
                    "cron": None,
                    "interval_seconds": 3600,
                },
            )
            tz = (
                await conn.execute(
                    text("SELECT timezone FROM schedule WHERE id = :id"),
                    {"id": schedule_id},
                )
            ).scalar_one()
            assert tz == "UTC"
        # Same (server_id, name) violates the uniqueness constraint.
        async with engine.connect() as conn:
            with pytest.raises(IntegrityError):
                await conn.execute(
                    _INSERT_SCHEDULE,
                    {
                        "id": uuid.uuid4(),
                        "server_id": server_id,
                        "name": "nightly",
                        "action": "backup",
                        "payload": "{}",
                        "cron": None,
                        "interval_seconds": 7200,
                    },
                )
    finally:
        await engine.dispose()

    # Remove the seeded rows before teardown so the walk down is clean.
    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM schedule"))
            await conn.execute(text("DELETE FROM server"))
            await conn.execute(text("DELETE FROM community"))
    finally:
        await engine.dispose()
    await downgrade_base(_DB_URL)


async def test_run_outcome_check_rejects_unknown_value() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    server_id = await _seed_server(_DB_URL)

    schedule_id = uuid.uuid4()
    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                _INSERT_SCHEDULE,
                {
                    "id": schedule_id,
                    "server_id": server_id,
                    "name": "nightly",
                    "action": "backup",
                    "payload": "{}",
                    "cron": None,
                    "interval_seconds": 3600,
                },
            )
        async with engine.connect() as conn:
            with pytest.raises(IntegrityError):
                await conn.execute(
                    text(
                        "INSERT INTO schedule_run (id, schedule_id, started_at, "
                        "finished_at, outcome) VALUES "
                        "(:id, :schedule_id, now(), now(), 'exploded')"
                    ),
                    {"id": uuid.uuid4(), "schedule_id": schedule_id},
                )
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
