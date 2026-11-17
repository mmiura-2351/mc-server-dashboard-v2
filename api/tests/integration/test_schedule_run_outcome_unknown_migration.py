"""Round-trip for the 0044 schedule-run ``unknown`` outcome migration (issue #3660).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Verifies that:

- the widened ``ck_schedule_run_outcome`` admits ``unknown`` at head and still
  rejects a value outside the enum;
- upgrade leaves existing ``failure`` rows exactly as they are (no backfill: a
  timeout and a refusal recorded before the change cannot be told apart);
- downgrade remaps ``unknown`` rows to ``failure``, which the runner recorded for
  the same run before this change, so the re-narrowed CHECK never rejects data
  that was valid at head.
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

_PRE_MIGRATION = "0043_worker_drain"
_MIGRATION = "0044_schedule_run_unknown"


async def _seed_schedule(conn: AsyncConnection) -> uuid.UUID:
    community_id = uuid.uuid4()
    server_id = uuid.uuid4()
    schedule_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO community (id, name, created_at, updated_at) "
            "VALUES (:id, 'guild', now(), now())"
        ),
        {"id": community_id},
    )
    await conn.execute(
        text(
            "INSERT INTO server (id, community_id, name, mc_edition, "
            "mc_version, server_type, config, slug, desired_state, "
            "observed_state, created_at, updated_at) VALUES "
            "(:id, :community_id, 'survival', 'java', '1.21.1', 'vanilla', "
            "'{}', :slug, 'stopped', 'unknown', now(), now())"
        ),
        {
            "id": server_id,
            "community_id": community_id,
            "slug": f"survival-{str(server_id)[:8]}-00",
        },
    )
    await conn.execute(
        text(
            "INSERT INTO schedule (id, server_id, name, action, payload, cron, "
            "interval_seconds, enabled, created_at, updated_at) VALUES "
            "(:id, :server_id, 'nightly', 'restart', '{}', NULL, 3600, "
            "false, now(), now())"
        ),
        {"id": schedule_id, "server_id": server_id},
    )
    return schedule_id


async def _insert_run(
    conn: AsyncConnection, schedule_id: uuid.UUID, *, outcome: str
) -> uuid.UUID:
    run_id = uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO schedule_run (id, schedule_id, started_at, finished_at, "
            "outcome) VALUES (:id, :schedule_id, now(), now(), :outcome)"
        ),
        {"id": run_id, "schedule_id": schedule_id, "outcome": outcome},
    )
    return run_id


async def _outcome(conn: AsyncConnection, run_id: uuid.UUID) -> object:
    return await conn.scalar(
        text("SELECT outcome FROM schedule_run WHERE id = :id"), {"id": run_id}
    )


async def _cleanup(conn: AsyncConnection) -> None:
    await conn.execute(text("DELETE FROM schedule"))
    await conn.execute(text("DELETE FROM server"))
    await conn.execute(text("DELETE FROM community"))


async def test_upgrade_leaves_failure_rows_untouched() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_to(_PRE_MIGRATION, _DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            schedule_id = await _seed_schedule(conn)
            failure = await _insert_run(conn, schedule_id, outcome="failure")

        await upgrade_to(_MIGRATION, _DB_URL)

        async with engine.begin() as conn:
            assert await _outcome(conn, failure) == "failure"
            await _cleanup(conn)
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)


async def test_check_admits_unknown_and_still_rejects_other_values() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_to(_MIGRATION, _DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            schedule_id = await _seed_schedule(conn)
            unknown = await _insert_run(conn, schedule_id, outcome="unknown")
        async with engine.connect() as conn:
            with pytest.raises(IntegrityError):
                await _insert_run(conn, schedule_id, outcome="timed_out")
        async with engine.begin() as conn:
            assert await _outcome(conn, unknown) == "unknown"
            await _cleanup(conn)
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)


async def test_downgrade_remaps_unknown_rows_to_failure() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_to(_MIGRATION, _DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            schedule_id = await _seed_schedule(conn)
            unknown = await _insert_run(conn, schedule_id, outcome="unknown")
            skipped = await _insert_run(conn, schedule_id, outcome="skipped")

        await downgrade_to(_PRE_MIGRATION, _DB_URL)

        async with engine.begin() as conn:
            assert await _outcome(conn, unknown) == "failure"
            assert await _outcome(conn, skipped) == "skipped"
            await _cleanup(conn)
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)
