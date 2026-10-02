"""Round-trip for the 0037 backup ``only_when_running`` migration (issue #2236).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). Verifies that:

- every existing ``backup`` schedule gets ``only_when_running: true`` merged
  into its payload on upgrade (owner decision: existing rows take the new
  default), while non-backup payloads are untouched;
- the ``ck_schedule_backup_only_when_running`` CHECK then holds the flag to
  backup rows: a backup row must carry a boolean, any other row must not carry
  the key at all;
- downgrade strips the key again.
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

_PRE_MIGRATION = "0036_floodgate_slug_rewrite"
_MIGRATION = "0037_backup_only_when_running"

_INSERT_SCHEDULE = text(
    "INSERT INTO schedule (id, server_id, name, action, payload, cron, "
    "interval_seconds, enabled, created_at, updated_at) VALUES "
    "(:id, :server_id, :name, :action, CAST(:payload AS jsonb), NULL, 3600, "
    "false, now(), now())"
)


async def _seed_server(conn: AsyncConnection) -> uuid.UUID:
    community_id = uuid.uuid4()
    server_id = uuid.uuid4()
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
    return server_id


async def _insert(
    conn: AsyncConnection,
    server_id: uuid.UUID,
    *,
    name: str,
    action: str,
    payload: str,
) -> uuid.UUID:
    schedule_id = uuid.uuid4()
    await conn.execute(
        _INSERT_SCHEDULE,
        {
            "id": schedule_id,
            "server_id": server_id,
            "name": name,
            "action": action,
            "payload": payload,
        },
    )
    return schedule_id


async def _payload(conn: AsyncConnection, schedule_id: uuid.UUID) -> object:
    return await conn.scalar(
        text("SELECT payload FROM schedule WHERE id = :id"), {"id": schedule_id}
    )


async def test_upgrade_turns_the_flag_on_for_existing_backup_rows() -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_to(_PRE_MIGRATION, _DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            server_id = await _seed_server(conn)
            backup_id = await _insert(
                conn, server_id, name="nightly", action="backup", payload="{}"
            )
            command_id = await _insert(
                conn,
                server_id,
                name="greet",
                action="command",
                payload='{"command": "say hi"}',
            )
            stop_id = await _insert(
                conn, server_id, name="stop", action="stop", payload="{}"
            )

        await upgrade_to(_MIGRATION, _DB_URL)

        async with engine.connect() as conn:
            assert await _payload(conn, backup_id) == {"only_when_running": True}
            assert await _payload(conn, command_id) == {"command": "say hi"}
            assert await _payload(conn, stop_id) == {}

        await downgrade_to(_PRE_MIGRATION, _DB_URL)

        async with engine.connect() as conn:
            assert await _payload(conn, backup_id) == {}
            assert await _payload(conn, command_id) == {"command": "say hi"}
            assert await _payload(conn, stop_id) == {}
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)


@pytest.mark.parametrize(
    ("action", "payload"),
    [
        # A backup row must carry the flag, as a boolean.
        ("backup", "{}"),
        ("backup", '{"only_when_running": null}'),
        ("backup", '{"only_when_running": "yes"}'),
        # Any other row must not carry the key at all.
        ("start", '{"only_when_running": true}'),
        ("stop", '{"only_when_running": false}'),
    ],
)
async def test_check_holds_the_flag_to_backup_rows(action: str, payload: str) -> None:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_to(_MIGRATION, _DB_URL)

    engine = create_async_engine(_DB_URL)
    try:
        async with engine.begin() as conn:
            server_id = await _seed_server(conn)
        async with engine.connect() as conn:
            with pytest.raises(IntegrityError):
                await _insert(
                    conn, server_id, name="bad", action=action, payload=payload
                )
        # The well-formed shapes are accepted.
        async with engine.begin() as conn:
            await _insert(
                conn,
                server_id,
                name="on",
                action="backup",
                payload='{"only_when_running": true}',
            )
            await _insert(
                conn,
                server_id,
                name="off",
                action="backup",
                payload='{"only_when_running": false}',
            )
            await _insert(conn, server_id, name="start", action="start", payload="{}")
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM schedule"))
            await conn.execute(text("DELETE FROM server"))
            await conn.execute(text("DELETE FROM community"))
    finally:
        await engine.dispose()

    await downgrade_base(_DB_URL)
