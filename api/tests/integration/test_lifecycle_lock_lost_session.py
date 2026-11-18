"""The lifecycle lock notices that its session ended (issue #3927).

:class:`PgLifecycleLock` holds a session-level advisory lock on a dedicated
connection. When that session ends, PostgreSQL releases the lock and another
holder can take it. These tests end the session with ``pg_terminate_backend``
and pin what the held lock then reports: ``ensure_held`` raises
:class:`LifecycleLockLostError` (and keeps raising), and leaving the ``hold``
surfaces that typed error rather than a raw driver error from the release.

DB-gated: skipped unless ``MCD_TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.servers.adapters import (
    lifecycle_lock as lifecycle_lock_mod,
)
from mc_server_dashboard_api.servers.adapters.lifecycle_lock import PgLifecycleLock
from mc_server_dashboard_api.servers.domain.errors import LifecycleLockLostError
from mc_server_dashboard_api.servers.domain.value_objects import ServerId

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    eng = create_async_engine(_DB_URL)
    try:
        yield eng
    finally:
        await eng.dispose()


async def terminate_lock_session(engine: AsyncEngine, server_id: ServerId) -> None:
    """End the session that holds ``server_id``'s lifecycle lock, and wait for it."""

    async with engine.connect() as conn:
        pid = await conn.scalar(
            text(
                "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted"
                " AND objsubid = 1"
                " AND ((classid::bigint << 32) | objid::bigint) = :key"
            ),
            {"key": lifecycle_lock_mod._lock_key(server_id)},
        )
        assert pid is not None, "no session holds the lock"
        assert await conn.scalar(
            text("SELECT pg_terminate_backend(:pid, 5000)"), {"pid": pid}
        )


async def test_a_held_lock_confirms_it_is_held(engine: AsyncEngine) -> None:
    lock = PgLifecycleLock(engine=engine)

    async with lock.hold(ServerId(uuid.uuid4())) as held:
        await held.ensure_held()


async def test_a_lock_whose_session_ended_reports_itself_lost(
    engine: AsyncEngine,
) -> None:
    server_id = ServerId(uuid.uuid4())
    lock = PgLifecycleLock(engine=engine)

    with pytest.raises(LifecycleLockLostError):
        async with lock.hold(server_id) as held:
            await terminate_lock_session(engine, server_id)
            with pytest.raises(LifecycleLockLostError):
                await held.ensure_held()
            # Lost stays lost, although the dead connection now fails differently.
            await held.ensure_held()

    # The ended session released it: another holder takes it at once.
    async with lock.hold(server_id) as held:
        await held.ensure_held()


async def test_leaving_a_lock_whose_session_ended_reports_it_lost(
    engine: AsyncEngine,
) -> None:
    # The body wrote without checking again, so its last writes may have run
    # outside the exclusion: the release tells the caller instead of passing.
    server_id = ServerId(uuid.uuid4())
    lock = PgLifecycleLock(engine=engine)

    with pytest.raises(LifecycleLockLostError):
        async with lock.hold(server_id):
            await terminate_lock_session(engine, server_id)


async def test_a_session_that_no_longer_holds_the_lock_reports_it_lost(
    engine: AsyncEngine,
) -> None:
    # The check asks whether THIS session still holds the lock, not merely
    # whether the connection answers: a live session can stop holding it (a
    # pooler handing the connection a different backend), and a plain ping would
    # pass there. Released by hand on the lock's own connection to get one.
    server_id = ServerId(uuid.uuid4())
    lock = PgLifecycleLock(engine=engine)

    with pytest.raises(LifecycleLockLostError):
        async with lock.hold(server_id) as held:
            assert isinstance(held, lifecycle_lock_mod._PgHeldLifecycleLock)
            await held._conn.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": lifecycle_lock_mod._lock_key(server_id)},
            )
            await held.ensure_held()


async def test_a_check_that_gets_no_answer_in_time_reports_the_lock_lost(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A connection that stops answering (a silent network drop) cannot confirm
    # the lock either, and a check waiting on it would hold the storage lock the
    # fences run under. The check gives up, and leaving the hold does not wait on
    # that connection again.
    monkeypatch.setattr(lifecycle_lock_mod, "_CHECK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(
        lifecycle_lock_mod,
        "_HELD_SQL",
        text("SELECT true FROM pg_sleep(10) WHERE CAST(:key AS bigint) IS NOT NULL"),
    )
    lock = PgLifecycleLock(engine=engine)

    async with asyncio.timeout(5):
        with pytest.raises(LifecycleLockLostError):
            async with lock.hold(ServerId(uuid.uuid4())) as held:
                with pytest.raises(LifecycleLockLostError):
                    await held.ensure_held()
                await held.ensure_held()


async def test_a_check_cancelled_mid_query_stays_a_cancellation_and_frees_the_lock(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A request whose client goes away is cancelled wherever it waits, the
    # check's query included. The cancellation must reach the caller as itself,
    # and the connection the query was cut off on must not keep the lock.
    monkeypatch.setattr(
        lifecycle_lock_mod,
        "_HELD_SQL",
        text("SELECT true FROM pg_sleep(10) WHERE CAST(:key AS bigint) IS NOT NULL"),
    )
    server_id = ServerId(uuid.uuid4())
    lock = PgLifecycleLock(engine=engine)
    checking = asyncio.Event()

    async def _gated() -> None:
        async with lock.hold(server_id) as held:
            checking.set()
            await held.ensure_held()

    task = asyncio.create_task(_gated())
    await checking.wait()
    await asyncio.sleep(0.2)  # let the check's query reach the server
    task.cancel()
    async with asyncio.timeout(5):
        with pytest.raises(asyncio.CancelledError):
            await task
        monkeypatch.undo()
        async with lock.hold(server_id) as held:
            await held.ensure_held()
