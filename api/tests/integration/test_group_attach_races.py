"""A group change's list of affected servers cannot go stale under it (#3223).

A change to a group records an owed file regeneration for every server the group
is attached to. If a server could be attached after the change listed them and
before it committed, that server would be missed: the attachment's own mark can
be applied and cleared by a start before the change commits, and the change then
lands (a deleted group cascades the attachment away, a removed player leaves the
union) with nothing left owed. The removed operator stays in ``ops.json``.

Each test pauses one change right after it has listed the attached servers,
starts an attach of the same group on another connection, and requires that the
attach waits for the change instead of slipping in beside it.

The last three pin the lock order a group edit shares with a group delete
(issue #2965): ``save`` locks the group row before it touches a player row. An
edit paused right after its player-row DELETE holds off a delete and a second
edit until it commits, and an edit arriving behind a paused delete waits, then
finds the group gone.

Runs only when ``MCD_TEST_DATABASE_URL`` is set (a real PostgreSQL); skipped
otherwise (TESTING.md Section 5).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import Delete, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.servers.adapters.group_models import GroupPlayerModel
from mc_server_dashboard_api.servers.adapters.group_repository import (
    SqlAlchemyGroupRepository,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.groups import (
    AttachGroup,
    DeleteGroup,
    RemovePlayer,
    RenameGroup,
)
from mc_server_dashboard_api.servers.domain.errors import GroupNotFoundError
from mc_server_dashboard_api.servers.domain.groups import (
    GroupId,
    GroupKind,
    GroupName,
    Player,
    PlayerGroup,
)
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId
from tests.integration.races import SETTLE_TIMEOUT, await_settled, race_database
from tests.servers.fakes import FakeFileStore

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    async with race_database(_DB_URL) as eng:
        yield eng


class _Pause:
    """Holds a change after it listed the attached servers, or after its save."""

    def __init__(self, *, after_save: bool = False) -> None:
        self.after_save = after_save
        self.reached = asyncio.Event()
        self.resume = asyncio.Event()

    async def hold(self) -> None:
        self.reached.set()
        await self.resume.wait()


class _PausingGroupRepository(SqlAlchemyGroupRepository):
    def __init__(self, session: AsyncSession, pause: _Pause) -> None:
        super().__init__(session)
        self._pause = pause

    async def list_server_ids_for_group(self, group_id: GroupId) -> list[ServerId]:
        listed = await super().list_server_ids_for_group(group_id)
        if not self._pause.after_save:
            await self._pause.hold()
        return listed

    async def save(self, group: PlayerGroup) -> None:
        await super().save(group)
        if self._pause.after_save:
            await self._pause.hold()


class _PausingUnitOfWork(ServersUnitOfWork):
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], pause: _Pause
    ) -> None:
        super().__init__(session_factory)
        self._pause = pause

    async def __aenter__(self) -> _PausingUnitOfWork:
        await super().__aenter__()
        assert self._session is not None
        self.groups = _PausingGroupRepository(self._session, self._pause)
        return self


class _PauseMidSaveSession(AsyncSession):
    """Holds ``save`` right after its player-row DELETE, before the replacements.

    That is the point where an edit could close a cycle with a delete: it holds
    the group's player rows and has yet to insert their replacements, whose FK
    check must lock the group row (issue #2965).
    """

    def __init__(self, *args: Any, pause: _Pause, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pause = pause

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = await super().execute(statement, *args, **kwargs)
        if (
            isinstance(statement, Delete)
            and statement.entity_description["entity"] is GroupPlayerModel
            and not self._pause.reached.is_set()
        ):
            await self._pause.hold()
        return result


def _pausing_mid_save(
    engine: AsyncEngine, pause: _Pause
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine, expire_on_commit=False, class_=_PauseMidSaveSession, pause=pause
    )


class _PidUnitOfWork(ServersUnitOfWork):
    """Servers unit of work reporting its first transaction's backend pid."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        pid: asyncio.Future[int],
    ) -> None:
        super().__init__(session_factory)
        self._pid = pid

    async def __aenter__(self) -> _PidUnitOfWork:
        await super().__aenter__()
        assert self._session is not None
        if not self._pid.done():
            self._pid.set_result(
                (
                    await self._session.execute(text("SELECT pg_backend_pid()"))
                ).scalar_one()
            )
        return self


class _World:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.factory = create_session_factory(engine)
        self.community_id = CommunityId(uuid.uuid4())
        self.server_id = ServerId(uuid.uuid4())
        self.player = uuid.uuid4()
        self.group = PlayerGroup(
            id=GroupId.new(),
            community_id=self.community_id,
            name=GroupName("admins"),
            kind=GroupKind.OP,
            players=[Player(self.player, "alice")],
        )

    async def seed(self) -> None:
        """A community, a stopped server, and an OP group attached to nothing."""

        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO community (id, name, created_at, updated_at) "
                    "VALUES (:id, 'guild', now(), now())"
                ),
                {"id": self.community_id.value},
            )
            await conn.execute(
                text(
                    "INSERT INTO server "
                    "(id, community_id, name, mc_edition, mc_version, server_type, "
                    "config, slug, desired_state, observed_state, "
                    "created_at, updated_at) VALUES "
                    "(:id, :cid, 'survival', 'java', '1.21', 'vanilla', "
                    "'{}'::jsonb, :slug, 'stopped', 'stopped', now(), now())"
                ),
                {
                    "id": self.server_id.value,
                    "cid": self.community_id.value,
                    "slug": f"srv-{str(self.server_id.value)[:8]}-00",
                },
            )
        async with ServersUnitOfWork(self.factory) as uow:
            await uow.groups.add(self.group)
            await uow.commit()

    def attach(self, pid: asyncio.Future[int]) -> asyncio.Task[None]:
        return asyncio.create_task(
            AttachGroup(
                uow=_PidUnitOfWork(self.factory, pid), file_store=FakeFileStore()
            )(
                community_id=self.community_id,
                group_id=self.group.id,
                server_id=self.server_id,
            )
        )

    async def count(self, table: str) -> int:
        async with self.engine.connect() as conn:
            return int(
                (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
            )


async def _attach_waits_for(
    world: _World, pause: _Pause, change: asyncio.Task[object]
) -> BaseException | None:
    """Race an attach against the paused ``change``; return the attach's error."""

    await pause.reached.wait()
    pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    attach = world.attach(pid)
    await await_settled(
        world.engine, attach, lambda: pid.result() if pid.done() else None
    )
    # The attach must be waiting on the change, not already committed beside it.
    assert not attach.done()
    pause.resume.set()
    await change
    (outcome,) = await asyncio.gather(attach, return_exceptions=True)
    return outcome


async def test_attach_racing_a_group_delete_is_refused(engine: AsyncEngine) -> None:
    world = _World(engine)
    await world.seed()
    pause = _Pause()
    deletion: asyncio.Task[object] = asyncio.create_task(
        DeleteGroup(
            uow=_PausingUnitOfWork(world.factory, pause), file_store=FakeFileStore()
        )(community_id=world.community_id, group_id=world.group.id)
    )

    outcome = await _attach_waits_for(world, pause, deletion)

    # The group was deleted with the servers it listed; the attach that came
    # after finds no group, so no attachment is cascaded away unrecorded.
    assert isinstance(outcome, GroupNotFoundError)
    assert await world.count("server_group") == 0


async def test_attach_racing_a_player_removal_lands_after_it(
    engine: AsyncEngine,
) -> None:
    world = _World(engine)
    await world.seed()
    pause = _Pause()
    removal: asyncio.Task[object] = asyncio.create_task(
        RemovePlayer(
            uow=_PausingUnitOfWork(world.factory, pause), file_store=FakeFileStore()
        )(
            community_id=world.community_id,
            group_id=world.group.id,
            player_uuid=world.player,
        )
    )

    outcome = await _attach_waits_for(world, pause, removal)

    # The attach commits after the removal, so the regeneration it records is
    # owed against a union that already excludes the removed player.
    assert outcome is None
    assert await world.count("server_group") == 1
    assert await world.count("server_group_sync_pending") == 1


async def test_player_removal_and_group_delete_do_not_deadlock(
    engine: AsyncEngine,
) -> None:
    # The removal has deleted the player row and the delete wants the group row.
    # Were the removal to take the group row only now, each would wait on the row
    # the other holds and PostgreSQL would abort one of them as a deadlock.
    world = _World(engine)
    await world.seed()
    pause = _Pause(after_save=True)
    removal = asyncio.create_task(
        RemovePlayer(
            uow=_PausingUnitOfWork(world.factory, pause), file_store=FakeFileStore()
        )(
            community_id=world.community_id,
            group_id=world.group.id,
            player_uuid=world.player,
        )
    )
    await pause.reached.wait()
    pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    deletion = asyncio.create_task(
        DeleteGroup(uow=_PidUnitOfWork(world.factory, pid), file_store=FakeFileStore())(
            community_id=world.community_id, group_id=world.group.id
        )
    )
    await await_settled(
        world.engine, deletion, lambda: pid.result() if pid.done() else None
    )
    pause.resume.set()

    # Both complete, in that order: the removal commits, then the delete it held
    # off removes the group.
    await removal
    await deletion
    assert await world.count("player_group") == 0


# --- one lock order for save and delete (issue #2965) --------------------------
# A rename to the name the group already has stages no UPDATE of the group row,
# so on its way to the player rows it takes no lock but the one ``save`` takes
# on its re-read. The tests pause it right after its player-row DELETE.


async def test_same_name_rename_and_group_delete_do_not_deadlock(
    engine: AsyncEngine,
) -> None:
    # The rename has deleted the player rows; the delete arrives. Were the group
    # row left to the replacement rows' FK check, the delete would lock it, wait
    # on the player rows, and the rename's INSERT would wait on the row.
    world = _World(engine)
    await world.seed()
    pause = _Pause()
    rename = asyncio.create_task(
        RenameGroup(uow=ServersUnitOfWork(_pausing_mid_save(engine, pause)))(
            community_id=world.community_id, group_id=world.group.id, name="admins"
        )
    )
    await asyncio.wait_for(pause.reached.wait(), SETTLE_TIMEOUT)
    pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    deletion = asyncio.create_task(
        DeleteGroup(uow=_PidUnitOfWork(world.factory, pid), file_store=FakeFileStore())(
            community_id=world.community_id, group_id=world.group.id
        )
    )
    await await_settled(
        world.engine, deletion, lambda: pid.result() if pid.done() else None
    )
    assert not deletion.done()
    pause.resume.set()

    # Both complete, in that order: the rename commits, then the delete it held
    # off removes the group.
    await rename
    await deletion
    assert await world.count("player_group") == 0


async def test_same_name_rename_racing_a_group_delete_reports_not_found(
    engine: AsyncEngine,
) -> None:
    # The other order: the delete holds the group row first. The rename waits on
    # the row before it touches a player row, then finds the group gone and
    # answers the not-found (404) a delete landing a moment earlier gets.
    world = _World(engine)
    await world.seed()
    pause = _Pause()
    deletion = asyncio.create_task(
        DeleteGroup(
            uow=_PausingUnitOfWork(world.factory, pause), file_store=FakeFileStore()
        )(community_id=world.community_id, group_id=world.group.id)
    )
    await asyncio.wait_for(pause.reached.wait(), SETTLE_TIMEOUT)
    pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    rename = asyncio.create_task(
        RenameGroup(uow=_PidUnitOfWork(world.factory, pid))(
            community_id=world.community_id, group_id=world.group.id, name="admins"
        )
    )
    await await_settled(
        world.engine, rename, lambda: pid.result() if pid.done() else None
    )
    assert not rename.done()
    pause.resume.set()

    await deletion
    with pytest.raises(GroupNotFoundError):
        await rename
    assert await world.count("player_group") == 0


async def test_interleaved_player_edits_of_one_group_both_land(
    engine: AsyncEngine,
) -> None:
    # Two edits of one group, the second arriving once the first has deleted the
    # player rows. The second waits on the group row the first locked, so its
    # wholesale DELETE runs after the first commits and removes what the first
    # wrote: it re-inserts nothing on top of it. Run side by side instead, the
    # second re-inserted onto the first's committed rows and violated
    # uq_group_player_group_uuid (issue #2613).
    world = _World(engine)
    await world.seed()
    contested = Player(uuid.uuid4(), "bob")

    async def edit(uow: ServersUnitOfWork) -> None:
        async with uow:
            loaded = await uow.groups.get_by_id(world.group.id)
            assert loaded is not None
            loaded.upsert_player(contested)
            await uow.groups.save(loaded)
            await uow.commit()

    pause = _Pause()
    first = asyncio.create_task(
        edit(ServersUnitOfWork(_pausing_mid_save(engine, pause)))
    )
    await asyncio.wait_for(pause.reached.wait(), SETTLE_TIMEOUT)
    pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    second = asyncio.create_task(edit(_PidUnitOfWork(world.factory, pid)))
    await await_settled(
        world.engine, second, lambda: pid.result() if pid.done() else None
    )
    assert not second.done()
    pause.resume.set()

    await first
    await second
    assert await world.count("group_player") == 2
