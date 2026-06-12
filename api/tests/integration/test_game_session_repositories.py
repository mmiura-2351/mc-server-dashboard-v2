"""Integration tests for game-session ingestion + read/prune on PostgreSQL.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service);
skipped otherwise (TESTING.md Section 5). The schema is created/torn down per
test via the real migrations. Exercises the ``ServersSessionSink`` ingestion
(idempotent start/end, end-before-start, orphan healing) and the
``GameSessionRepository`` read/prune (newest-first pagination, retention window),
plus the server-delete cascade (issue #957, RELAY.md Sections 6, 8, 13).
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as CommunityUnitOfWork,
)
from mc_server_dashboard_api.community.domain.entities import Community
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId as CommunityCommunityId,
)
from mc_server_dashboard_api.community.domain.value_objects import CommunityName
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.fleet.domain.session_sink import SessionStart
from mc_server_dashboard_api.servers.adapters.session_sink import ServersSessionSink
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.manage_server import CreateServer
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId as ServersCommunityId,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.fakes import FakeClock, FakeFileStore, FakeVersionValidator

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 12, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    eng = create_async_engine(_DB_URL)
    try:
        yield eng
    finally:
        await eng.dispose()
        await downgrade_base(_DB_URL)


async def _seed_server(engine: AsyncEngine) -> uuid.UUID:
    community = Community(
        id=CommunityCommunityId(uuid.uuid4()),
        name=CommunityName("guild"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    factory = create_session_factory(engine)
    async with CommunityUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.commit()
    server = await CreateServer(
        uow=ServersUnitOfWork(factory),
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=PortRange(start=25565, end=25664),
    )(
        community_id=ServersCommunityId(community.id.value),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="container",
        config={},
    )
    return server.id.value


def _start(
    server_id: uuid.UUID,
    *,
    session_id: str,
    started_at: dt.datetime,
    username: str | None = "steve",
    player_uuid: str | None = "66666666-6666-6666-6666-666666666666",
) -> SessionStart:
    return SessionStart(
        session_id=session_id,
        server_id=str(server_id),
        hostname="amber-falcon-42",
        player_ip="203.0.113.7",
        username=username,
        player_uuid=player_uuid,
        started_at=started_at,
    )


async def _count_rows(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT count(*) FROM game_session"))
        return int(result.scalar_one())


async def test_record_start_is_idempotent(engine: AsyncEngine) -> None:
    server_id = await _seed_server(engine)
    sink = ServersSessionSink(create_session_factory(engine))
    sid = str(uuid.uuid4())
    await sink.record_start(_start(server_id, session_id=sid, started_at=_NOW))
    # A duplicate start (a retry) does not overwrite or duplicate the row.
    await sink.record_start(
        _start(server_id, session_id=sid, started_at=_NOW + dt.timedelta(hours=1))
    )
    assert await _count_rows(engine) == 1
    factory = create_session_factory(engine)
    async with ServersUnitOfWork(factory) as uow:
        rows = await uow.game_sessions.list_for_server(
            ServerId(server_id), limit=10, offset=0
        )
    assert len(rows) == 1
    assert rows[0].started_at == _NOW
    assert rows[0].username == "steve"
    assert rows[0].ended_at is None


async def test_record_end_then_dup_end_is_idempotent(engine: AsyncEngine) -> None:
    server_id = await _seed_server(engine)
    sink = ServersSessionSink(create_session_factory(engine))
    sid = str(uuid.uuid4())
    await sink.record_start(_start(server_id, session_id=sid, started_at=_NOW))
    first_end = _NOW + dt.timedelta(minutes=5)
    await sink.record_end(session_id=sid, ended_at=first_end)
    # A duplicate end keeps the first recorded ended_at.
    await sink.record_end(session_id=sid, ended_at=_NOW + dt.timedelta(minutes=9))
    factory = create_session_factory(engine)
    async with ServersUnitOfWork(factory) as uow:
        rows = await uow.game_sessions.list_for_server(
            ServerId(server_id), limit=10, offset=0
        )
    assert rows[0].ended_at == first_end


async def test_end_before_start_reconciles(engine: AsyncEngine) -> None:
    server_id = await _seed_server(engine)
    sink = ServersSessionSink(create_session_factory(engine))
    sid = str(uuid.uuid4())
    ended = _NOW + dt.timedelta(minutes=5)
    # End arrives first: a placeholder row carrying only id + ended_at (no
    # server_id yet, so it is not in the server listing until the start reconciles).
    await sink.record_end(session_id=sid, ended_at=ended)
    factory = create_session_factory(engine)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT server_id, started_at, ended_at "
                    "FROM game_session WHERE id = :id"
                ),
                {"id": uuid.UUID(sid)},
            )
        ).one()
    assert row.server_id is None
    assert row.started_at is None
    assert row.ended_at == ended
    # The start fills in the rest without clearing ended_at.
    await sink.record_start(_start(server_id, session_id=sid, started_at=_NOW))
    async with ServersUnitOfWork(factory) as uow:
        rows = await uow.game_sessions.list_for_server(
            ServerId(server_id), limit=10, offset=0
        )
    assert rows[0].started_at == _NOW
    assert rows[0].player_ip == "203.0.113.7"
    assert rows[0].ended_at == ended


async def test_close_absent_heals_orphans(engine: AsyncEngine) -> None:
    server_id = await _seed_server(engine)
    sink = ServersSessionSink(create_session_factory(engine))
    kept = str(uuid.uuid4())
    orphan = str(uuid.uuid4())
    closed_already = str(uuid.uuid4())
    await sink.record_start(_start(server_id, session_id=kept, started_at=_NOW))
    await sink.record_start(_start(server_id, session_id=orphan, started_at=_NOW))
    await sink.record_start(
        _start(server_id, session_id=closed_already, started_at=_NOW)
    )
    await sink.record_end(
        session_id=closed_already, ended_at=_NOW + dt.timedelta(minutes=1)
    )

    healed_at = _NOW + dt.timedelta(minutes=10)
    # Only `kept` is still active; `orphan` is open-but-absent and must be closed.
    n = await sink.close_absent(active_session_ids=[kept], ended_at=healed_at)
    assert n == 1

    factory = create_session_factory(engine)
    async with ServersUnitOfWork(factory) as uow:
        rows = {
            str(r.id): r
            for r in await uow.game_sessions.list_for_server(
                ServerId(server_id), limit=10, offset=0
            )
        }
    assert rows[kept].ended_at is None
    assert rows[orphan].ended_at == healed_at
    # An already-closed row keeps its original ended_at (only open rows are touched).
    assert rows[closed_already].ended_at == _NOW + dt.timedelta(minutes=1)


async def test_close_absent_with_empty_active_set_closes_all_open(
    engine: AsyncEngine,
) -> None:
    server_id = await _seed_server(engine)
    sink = ServersSessionSink(create_session_factory(engine))
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    await sink.record_start(_start(server_id, session_id=a, started_at=_NOW))
    await sink.record_start(_start(server_id, session_id=b, started_at=_NOW))
    healed_at = _NOW + dt.timedelta(minutes=10)
    n = await sink.close_absent(active_session_ids=[], ended_at=healed_at)
    assert n == 2


async def test_list_is_newest_first_and_paginated(engine: AsyncEngine) -> None:
    server_id = await _seed_server(engine)
    sink = ServersSessionSink(create_session_factory(engine))
    ids = []
    for i in range(3):
        sid = str(uuid.uuid4())
        ids.append(sid)
        await sink.record_start(
            _start(server_id, session_id=sid, started_at=_NOW + dt.timedelta(hours=i))
        )
    # ids[2] is newest. Page size 2 -> newest two, then the remaining one.
    factory = create_session_factory(engine)
    async with ServersUnitOfWork(factory) as uow:
        page1 = await uow.game_sessions.list_for_server(
            ServerId(server_id), limit=2, offset=0
        )
        page2 = await uow.game_sessions.list_for_server(
            ServerId(server_id), limit=2, offset=2
        )
    assert [str(r.id) for r in page1] == [ids[2], ids[1]]
    assert [str(r.id) for r in page2] == [ids[0]]


async def test_prune_deletes_only_rows_older_than_cutoff(engine: AsyncEngine) -> None:
    server_id = await _seed_server(engine)
    sink = ServersSessionSink(create_session_factory(engine))
    old = str(uuid.uuid4())
    fresh = str(uuid.uuid4())
    await sink.record_start(
        _start(server_id, session_id=old, started_at=_NOW - dt.timedelta(days=100))
    )
    await sink.record_start(
        _start(server_id, session_id=fresh, started_at=_NOW - dt.timedelta(days=10))
    )
    cutoff = _NOW - dt.timedelta(days=90)
    factory = create_session_factory(engine)
    async with ServersUnitOfWork(factory) as uow:
        deleted = await uow.game_sessions.delete_started_before(cutoff)
        await uow.commit()
    assert deleted == 1
    async with ServersUnitOfWork(factory) as uow:
        rows = await uow.game_sessions.list_for_server(
            ServerId(server_id), limit=10, offset=0
        )
    assert [str(r.id) for r in rows] == [fresh]


async def test_start_for_deleted_server_is_dropped_mid_batch(
    engine: AsyncEngine,
) -> None:
    # A start referencing a server that no longer exists violates the FK; it must
    # be dropped (the docstring promise) without wedging the batch, so a following
    # good start still ingests (poison-event isolation, issue #957).
    server_id = await _seed_server(engine)
    sink = ServersSessionSink(create_session_factory(engine))
    missing_server = uuid.uuid4()
    poison = str(uuid.uuid4())
    good = str(uuid.uuid4())
    await sink.record_start(_start(missing_server, session_id=poison, started_at=_NOW))
    await sink.record_start(_start(server_id, session_id=good, started_at=_NOW))
    # The poison row was dropped; the good row after it still ingested.
    assert await _count_rows(engine) == 1
    factory = create_session_factory(engine)
    async with ServersUnitOfWork(factory) as uow:
        rows = await uow.game_sessions.list_for_server(
            ServerId(server_id), limit=10, offset=0
        )
    assert [str(r.id) for r in rows] == [good]


async def test_start_with_malformed_ip_is_dropped_mid_batch(
    engine: AsyncEngine,
) -> None:
    # A non-INET player_ip raises DataError on the cast; it is dropped without
    # wedging the batch, and a following good start still ingests.
    server_id = await _seed_server(engine)
    sink = ServersSessionSink(create_session_factory(engine))
    poison = SessionStart(
        session_id=str(uuid.uuid4()),
        server_id=str(server_id),
        hostname="amber-falcon-42",
        player_ip="not-an-ip",
        username="steve",
        player_uuid=None,
        started_at=_NOW,
    )
    good = str(uuid.uuid4())
    await sink.record_start(poison)
    await sink.record_start(_start(server_id, session_id=good, started_at=_NOW))
    assert await _count_rows(engine) == 1
    factory = create_session_factory(engine)
    async with ServersUnitOfWork(factory) as uow:
        rows = await uow.game_sessions.list_for_server(
            ServerId(server_id), limit=10, offset=0
        )
    assert [str(r.id) for r in rows] == [good]


async def test_record_start_truncates_oversized_username_and_hostname(
    engine: AsyncEngine,
) -> None:
    # Defensive truncation at the seam: a username past the 16-char protocol max
    # and a hostname past the 253-char DNS max are clipped, not rejected.
    server_id = await _seed_server(engine)
    sink = ServersSessionSink(create_session_factory(engine))
    sid = str(uuid.uuid4())
    long_username = "a" * 40
    long_hostname = "h" * 300
    start = SessionStart(
        session_id=sid,
        server_id=str(server_id),
        hostname=long_hostname,
        player_ip="203.0.113.7",
        username=long_username,
        player_uuid=None,
        started_at=_NOW,
    )
    await sink.record_start(start)
    factory = create_session_factory(engine)
    async with ServersUnitOfWork(factory) as uow:
        rows = await uow.game_sessions.list_for_server(
            ServerId(server_id), limit=10, offset=0
        )
    assert rows[0].username == "a" * 16
    assert rows[0].hostname == "h" * 253


async def test_prune_deletes_stale_end_only_placeholder(engine: AsyncEngine) -> None:
    # An end-only placeholder (started_at IS NULL) whose start never arrived must
    # be pruned by its ended_at so it does not live forever (issue #957).
    sink = ServersSessionSink(create_session_factory(engine))
    stale = str(uuid.uuid4())
    fresh = str(uuid.uuid4())
    await sink.record_end(
        session_id=stale, ended_at=_NOW - dt.timedelta(days=100)
    )
    await sink.record_end(
        session_id=fresh, ended_at=_NOW - dt.timedelta(days=10)
    )
    cutoff = _NOW - dt.timedelta(days=90)
    factory = create_session_factory(engine)
    async with ServersUnitOfWork(factory) as uow:
        deleted = await uow.game_sessions.delete_started_before(cutoff)
        await uow.commit()
    assert deleted == 1
    async with engine.connect() as conn:
        remaining = {
            str(r.id)
            for r in (
                await conn.execute(text("SELECT id FROM game_session"))
            ).all()
        }
    assert remaining == {fresh}


async def test_sessions_cascade_on_server_delete(engine: AsyncEngine) -> None:
    server_id = await _seed_server(engine)
    sink = ServersSessionSink(create_session_factory(engine))
    await sink.record_start(
        _start(server_id, session_id=str(uuid.uuid4()), started_at=_NOW)
    )
    assert await _count_rows(engine) == 1
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM server WHERE id = :id"), {"id": server_id})
    assert await _count_rows(engine) == 0
