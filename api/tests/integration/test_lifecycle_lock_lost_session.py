"""The lifecycle lock notices that its session ended (issue #3927).

:class:`PgLifecycleLock` holds a session-level advisory lock on a dedicated
connection. When that session ends, PostgreSQL releases the lock and another
holder can take it. These tests end the session with ``pg_terminate_backend``
and pin what the held lock then reports: ``ensure_held`` raises
:class:`LifecycleLockLostError` (and keeps raising), while a loss that only the
release finds is logged and never raised over the body's own outcome.

DB-gated: skipped unless ``MCD_TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from mc_server_dashboard_api.servers.adapters import (
    lifecycle_lock as lifecycle_lock_mod,
)
from mc_server_dashboard_api.servers.adapters.lifecycle_lock import PgLifecycleLock
from mc_server_dashboard_api.servers.domain.errors import (
    LifecycleLockLostError,
    ServerNotStoppedError,
)
from mc_server_dashboard_api.servers.domain.lifecycle_lock import HeldLifecycleLock
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


_ADAPTER_LOGGER = lifecycle_lock_mod.__name__


def _release_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == _ADAPTER_LOGGER and r.levelno == logging.WARNING
    ]


async def test_a_loss_only_the_release_finds_is_logged_and_the_hold_ends_normally(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    # Every write the body made ran after a check that passed, and the window
    # after the last one is the documented one: the operation finished, so it
    # answers as finished. The loss is logged, and the lock is gone with the
    # session.
    server_id = ServerId(uuid.uuid4())
    lock = PgLifecycleLock(engine=engine)

    async with lock.hold(server_id):
        await terminate_lock_session(engine, server_id)

    assert len(_release_warnings(caplog)) == 1
    async with lock.hold(server_id) as held:
        await held.ensure_held()


async def test_the_release_never_replaces_what_the_body_raised(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    server_id = ServerId(uuid.uuid4())
    lock = PgLifecycleLock(engine=engine)

    with pytest.raises(ServerNotStoppedError):
        async with lock.hold(server_id):
            await terminate_lock_session(engine, server_id)
            raise ServerNotStoppedError(str(server_id.value))

    assert len(_release_warnings(caplog)) == 1


async def test_a_cancelled_body_stays_cancelled_when_the_release_finds_no_session(
    engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    server_id = ServerId(uuid.uuid4())
    lock = PgLifecycleLock(engine=engine)
    ended = asyncio.Event()

    async def _gated() -> None:
        async with lock.hold(server_id):
            await terminate_lock_session(engine, server_id)
            ended.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(_gated())
    await ended.wait()
    task.cancel()
    await asyncio.wait({task})

    assert task.cancelled()
    assert len(_release_warnings(caplog)) == 1


async def test_a_session_that_no_longer_holds_the_lock_reports_it_lost(
    engine: AsyncEngine,
) -> None:
    # The check asks whether THIS session still holds the lock, not merely
    # whether the connection answers: a live session can stop holding it, and a
    # plain ping would pass there. Released by hand on the lock's own connection
    # to get one.
    server_id = ServerId(uuid.uuid4())
    lock = PgLifecycleLock(engine=engine)

    with pytest.raises(LifecycleLockLostError):
        async with lock.hold(server_id) as held:
            await _release_by_hand(held, server_id)
            await held.ensure_held()


async def test_a_release_cancelled_mid_unlock_stays_a_cancellation(
    engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only a failed release is logged and swallowed: a cancellation that lands
    # while the unlock waits goes on as itself. The connection is closed rather
    # than pooled, so no session keeps the lock: a pooled one would hold it for
    # good, and only the same session could take it again.
    server_id = ServerId(uuid.uuid4())
    lock = PgLifecycleLock(engine=engine)
    unlocking = asyncio.Event()

    async def _never_answers(*_args: object, **_kwargs: object) -> None:
        unlocking.set()
        await asyncio.Event().wait()

    async def _hold() -> None:
        async with lock.hold(server_id):
            monkeypatch.setattr(AsyncConnection, "execute", _never_answers)

    task = asyncio.create_task(_hold())
    await unlocking.wait()
    task.cancel()
    await asyncio.wait({task})
    monkeypatch.undo()

    assert task.cancelled()
    assert _release_warnings(caplog) == []
    async with asyncio.timeout(5):
        while await _sessions_holding(engine, server_id):
            await asyncio.sleep(0.05)


async def _sessions_holding(engine: AsyncEngine, server_id: ServerId) -> int:
    """How many sessions hold ``server_id``'s lifecycle lock, pooled ones included."""

    async with engine.connect() as conn:
        count = await conn.scalar(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"
                " AND objsubid = 1"
                " AND ((classid::bigint << 32) | objid::bigint) = :key"
            ),
            {"key": lifecycle_lock_mod._lock_key(server_id)},
        )
    assert isinstance(count, int)
    return count


async def _release_by_hand(held: HeldLifecycleLock, server_id: ServerId) -> None:
    """Release the lock on the held lock's own connection, leaving it alive."""

    assert isinstance(held, lifecycle_lock_mod._PgHeldLifecycleLock)
    await held._conn.execute(
        text("SELECT pg_advisory_unlock(:key)"),
        {"key": lifecycle_lock_mod._lock_key(server_id)},
    )


async def test_the_key_held_by_another_session_is_not_held_here(
    engine: AsyncEngine,
) -> None:
    # Only this session's hold counts: once another session has the key, this
    # one is no longer excluding anybody, whoever holds it now.
    server_id = ServerId(uuid.uuid4())
    lock = PgLifecycleLock(engine=engine)

    async with engine.connect() as other:
        other = await other.execution_options(isolation_level="AUTOCOMMIT")
        with pytest.raises(LifecycleLockLostError):
            async with lock.hold(server_id) as held:
                await _release_by_hand(held, server_id)
                assert await other.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": lifecycle_lock_mod._lock_key(server_id)},
                )
                await held.ensure_held()


async def test_another_key_this_session_holds_does_not_count(
    engine: AsyncEngine,
) -> None:
    # Only the server's own key counts: an advisory lock this session holds on
    # any other key excludes nobody from this server.
    server_id = ServerId(uuid.uuid4())
    lock = PgLifecycleLock(engine=engine)

    with pytest.raises(LifecycleLockLostError):
        async with lock.hold(server_id) as held:
            await _release_by_hand(held, server_id)
            assert isinstance(held, lifecycle_lock_mod._PgHeldLifecycleLock)
            other_key = lifecycle_lock_mod._lock_key(ServerId(uuid.uuid4()))
            assert await held._conn.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": other_key}
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
