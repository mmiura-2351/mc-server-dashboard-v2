"""A community deletion must never take a server with it (#3218).

``fk_server_community_id_community`` is ``ON DELETE RESTRICT``: a community that
holds a server cannot be deleted, so no server row disappears without going
through ``DeleteServer``. The foreign key is also what serializes the deletion
with a concurrent server creation. Each test pauses one of the two transactions
mid-flight, lets the other run on a second connection until it has finished or
blocked on a lock, then resumes the paused one:

- the creation has inserted its server but not committed: the deletion waits for
  the community row (the INSERT's foreign-key check holds it ``FOR KEY SHARE``)
  and, once the creation commits, is refused with the typed error;
- the deletion has removed the community but not committed: the creation's
  INSERT waits for it and, once the deletion commits, fails its foreign key --
  the typed not-found the create routes already map to 404 (#2940).

Whichever order they land in, the result is a community with its server or
neither, never a server without a community or a deleted community's server.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (a real PostgreSQL); skipped
otherwise (TESTING.md Section 5).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from mc_server_dashboard_api.community.adapters.repositories import (
    SqlAlchemyCommunityRepository,
)
from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as CommunityUnitOfWork,
)
from mc_server_dashboard_api.community.application.manage_community import (
    DeleteCommunity,
)
from mc_server_dashboard_api.community.domain.entities import Community
from mc_server_dashboard_api.community.domain.errors import CommunityHasServersError
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    CommunityName,
)
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.fleet.adapters.real_time_events import (
    InProcessRealTimeEvents,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.manage_server import CreateServer
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import CommunityNotFoundError
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId as ServersCommunityId,
)
from tests.integration.races import SETTLE_TIMEOUT, await_settled, race_database
from tests.servers.fakes import FakeClock, FakeFileStore, FakeVersionValidator

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    async with race_database(_DB_URL) as eng:
        yield eng


class _Pause:
    """Holds a transaction at its pause point until released."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.resume = asyncio.Event()

    async def hold(self) -> None:
        self.reached.set()
        await self.resume.wait()


async def _report_pid(session: AsyncSession, pid: asyncio.Future[int]) -> None:
    result = await session.execute(text("SELECT pg_backend_pid()"))
    pid.set_result(result.scalar_one())


class _ServersUnitOfWork(ServersUnitOfWork):
    """Servers unit of work that reports its pid and can pause before COMMIT.

    The pause sits after the staged ``server`` INSERT has been sent, so the
    paused creation holds its row and the foreign-key lock on the community.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        pid: asyncio.Future[int] | None = None,
        pause: _Pause | None = None,
    ) -> None:
        super().__init__(session_factory, InProcessRealTimeEvents())
        self._pid = pid
        self._pause = pause

    async def __aenter__(self) -> _ServersUnitOfWork:
        await super().__aenter__()
        assert self._session is not None
        if self._pid is not None:
            await _report_pid(self._session, self._pid)
        return self

    async def commit(self) -> None:
        assert self._session is not None
        if self._pause is not None:
            await self._session.flush()
            await self._pause.hold()
        await super().commit()


class _PausingCommunityRepository(SqlAlchemyCommunityRepository):
    def __init__(self, session: AsyncSession, pause: _Pause, after: str) -> None:
        super().__init__(session)
        self._pause = pause
        self._after = after

    async def lock(self, community_id: CommunityId) -> bool:
        locked = await super().lock(community_id)
        if self._after == "lock":
            await self._pause.hold()
        return locked

    async def delete(self, community_id: CommunityId) -> None:
        await super().delete(community_id)
        if self._after == "delete":
            await self._pause.hold()


class _CommunityUnitOfWork(CommunityUnitOfWork):
    """Community unit of work that reports its pid and can pause.

    The pause sits after the community's ``lock`` or, by default, its DELETE.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        pid: asyncio.Future[int] | None = None,
        pause: _Pause | None = None,
        pause_after: str = "delete",
    ) -> None:
        super().__init__(session_factory)
        self._pid = pid
        self._pause = pause
        self._pause_after = pause_after

    async def __aenter__(self) -> _CommunityUnitOfWork:
        await super().__aenter__()
        assert self._session is not None
        if self._pid is not None:
            await _report_pid(self._session, self._pid)
        if self._pause is not None:
            self.communities = _PausingCommunityRepository(
                self._session, self._pause, self._pause_after
            )
        return self


async def _seed_community(factory: async_sessionmaker[AsyncSession]) -> CommunityId:
    community = Community(
        id=CommunityId(uuid.uuid4()),
        name=CommunityName("guild"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    async with CommunityUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.commit()
    return community.id


def _create_server(uow: ServersUnitOfWork, community_id: CommunityId) -> Any:
    return CreateServer(
        uow=uow,
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=PortRange(start=25565, end=25664),
    )(
        community_id=ServersCommunityId(community_id.value),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        config={},
    )


async def _race(
    engine: AsyncEngine,
    pause: _Pause,
    paused: asyncio.Task[Any],
    competitor: asyncio.Task[Any],
    pid: asyncio.Future[int],
) -> tuple[Any, Any]:
    """Resume ``paused`` once ``competitor`` is blocked on a lock behind it.

    The competitor must not have run to completion: what it has to react to is
    not committed yet, so it has to wait for the paused transaction.
    """

    await await_settled(
        engine, competitor, lambda: pid.result() if pid.done() else None
    )
    assert not competitor.done()
    pause.resume.set()
    outcomes = await asyncio.gather(paused, competitor, return_exceptions=True)
    return outcomes[0], outcomes[1]


async def _count(engine: AsyncEngine, table: str) -> int:
    async with engine.connect() as conn:
        return int(
            (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
        )


async def test_deletion_racing_an_uncommitted_server_creation_is_refused(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    community_id = await _seed_community(factory)

    pause = _Pause()
    creation: asyncio.Task[Server] = asyncio.create_task(
        _create_server(_ServersUnitOfWork(factory, pause=pause), community_id)
    )
    await pause.reached.wait()

    pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    deletion = asyncio.create_task(
        DeleteCommunity(uow=_CommunityUnitOfWork(factory, pid=pid))(
            community_id=community_id
        )
    )

    created, deleted = await _race(engine, pause, creation, deletion, pid)

    assert isinstance(created, Server)
    assert isinstance(deleted, CommunityHasServersError)
    assert await _count(engine, "community") == 1
    assert await _count(engine, "server") == 1


async def test_server_creation_racing_an_uncommitted_deletion_reports_not_found(
    engine: AsyncEngine,
) -> None:
    factory = create_session_factory(engine)
    community_id = await _seed_community(factory)

    pause = _Pause()
    deletion = asyncio.create_task(
        DeleteCommunity(uow=_CommunityUnitOfWork(factory, pause=pause))(
            community_id=community_id
        )
    )
    await pause.reached.wait()

    pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    creation: asyncio.Task[Server] = asyncio.create_task(
        _create_server(_ServersUnitOfWork(factory, pid=pid), community_id)
    )

    deleted, created = await _race(engine, pause, deletion, creation, pid)

    assert deleted is None
    assert isinstance(created, CommunityNotFoundError)
    assert await _count(engine, "community") == 0
    assert await _count(engine, "server") == 0


async def test_server_creation_waits_for_a_deletion_holding_the_community_row(
    engine: AsyncEngine,
) -> None:
    # The deletion locks the community row before it checks for servers, and a
    # creation's INSERT takes that row FOR KEY SHARE for its foreign key, which
    # the lock conflicts with: no creation commits between the check and the
    # DELETE (issue #4033).
    factory = create_session_factory(engine)
    community_id = await _seed_community(factory)

    pause = _Pause()
    deletion = asyncio.create_task(
        DeleteCommunity(
            uow=_CommunityUnitOfWork(factory, pause=pause, pause_after="lock")
        )(community_id=community_id)
    )
    await asyncio.wait_for(pause.reached.wait(), SETTLE_TIMEOUT)

    pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    creation: asyncio.Task[Server] = asyncio.create_task(
        _create_server(_ServersUnitOfWork(factory, pid=pid), community_id)
    )

    deleted, created = await _race(engine, pause, deletion, creation, pid)

    assert deleted is None
    assert isinstance(created, CommunityNotFoundError)
    assert await _count(engine, "community") == 0
    assert await _count(engine, "server") == 0


async def test_deletion_waiting_for_a_server_creation_is_refused_before_it_cascades(
    engine: AsyncEngine,
) -> None:
    # The deletion waits for the creation at the community row and checks for
    # servers only once it holds the row, so it sees the new server and is
    # refused before its DELETE runs (issue #4033). A check before the wait
    # misses the uncommitted server; the DELETE then cascades, and only the
    # foreign key refuses it, after it has waited for the rows of the cascade.
    # A third transaction holds one of them, a role of the community.
    factory = create_session_factory(engine)
    community_id = await _seed_community(factory)
    role_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO role "
                "(id, community_id, name, permissions, is_preset, "
                "created_at, updated_at) VALUES "
                "(:id, :cid, 'Builders', '{}', false, now(), now())"
            ),
            {"id": role_id, "cid": community_id.value},
        )

    pause = _Pause()
    creation: asyncio.Task[Server] = asyncio.create_task(
        _create_server(_ServersUnitOfWork(factory, pause=pause), community_id)
    )
    await asyncio.wait_for(pause.reached.wait(), SETTLE_TIMEOUT)
    pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    deletion = asyncio.create_task(
        DeleteCommunity(uow=_CommunityUnitOfWork(factory, pid=pid))(
            community_id=community_id
        )
    )
    await await_settled(engine, deletion, lambda: pid.result() if pid.done() else None)
    assert not deletion.done()

    async with engine.connect() as holder:
        await holder.execute(
            text("SELECT 1 FROM role WHERE id = :id FOR KEY SHARE"), {"id": role_id}
        )
        pause.resume.set()
        created = await creation
        finished, _ = await asyncio.wait({deletion}, timeout=SETTLE_TIMEOUT)
        assert deletion in finished, "the deletion waited for a row of its cascade"

    assert isinstance(created, Server)
    assert isinstance(deletion.exception(), CommunityHasServersError)
    assert await _count(engine, "community") == 1
    assert await _count(engine, "server") == 1
    assert await _count(engine, "role") == 1
