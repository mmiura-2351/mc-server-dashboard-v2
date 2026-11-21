"""A group change made while a server runs reaches its next launch (issue #3223).

The issue's reproduction, end to end on the real adapters: PostgreSQL through the
servers unit of work, ``FsStorage`` behind the file-store and store-generation
seams, and the real group and start use cases. Only the control plane is a fake,
standing in for a Worker that still holds the previous run's scratch.

Runs only when ``MCD_TEST_DATABASE_URL`` is set; skipped otherwise.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

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
from mc_server_dashboard_api.fleet.adapters.real_time_events import (
    InProcessRealTimeEvents,
)
from mc_server_dashboard_api.servers.adapters.file_store import StorageFileStoreAdapter
from mc_server_dashboard_api.servers.adapters.store_generation import (
    StorageGenerationReader,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.groups import (
    AddPlayer,
    AttachGroup,
    CreateGroup,
    DeleteGroup,
    DetachGroup,
    RemovePlayer,
)
from mc_server_dashboard_api.servers.application.lifecycle import StartServer
from mc_server_dashboard_api.servers.domain.control_plane import CommandOutcome
from mc_server_dashboard_api.servers.domain.errors import (
    CommandDispatchError,
    InvalidLifecycleTransitionError,
)
from mc_server_dashboard_api.servers.domain.groups import GroupId, GroupKind
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ObservedState,
    ServerId,
    WorkerId,
)
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.contract_table import worker_status
from tests.servers.fakes import FakeClock, FakeControlPlane, FakeJarProvisioner
from tests.storage.helpers import drain, read_tar

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.timezone.utc)
_WORKER = WorkerId(uuid.uuid4())


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


async def _seed_server(engine: AsyncEngine) -> tuple[CommunityId, ServerId]:
    community = Community(
        id=CommunityCommunityId(uuid.uuid4()),
        name=CommunityName("guild"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    async with CommunityUnitOfWork(create_session_factory(engine)) as uow:
        await uow.communities.add(community)
        await uow.commit()
    server_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO server (id, community_id, name, mc_edition, mc_version, "
                "server_type, config, slug, desired_state, "
                "observed_state, created_at, updated_at) VALUES "
                "(:id, :cid, 'srv', 'java', '1.21.1', 'vanilla', "
                "'{}', :slug, 'stopped', 'stopped', :at, :at)"
            ),
            {
                "id": server_id,
                "cid": community.id.value,
                "slug": f"srv-{str(server_id)[:8]}-00",
                "at": _NOW,
            },
        )
    return CommunityId(community.id.value), ServerId(server_id)


async def _set_state(engine: AsyncEngine, server_id: ServerId, state: str) -> None:
    """Stand in for the lifecycle: the row is running, or back at rest."""

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE server SET desired_state = :state, observed_state = :state "
                "WHERE id = :id"
            ),
            {"state": state, "id": server_id.value},
        )


class _World:
    """One server on the real adapters, plus the handles the tests assert on."""

    def __init__(
        self,
        engine: AsyncEngine,
        root: Path,
        community_id: CommunityId,
        server_id: ServerId,
    ) -> None:
        self.engine = engine
        self.factory = create_session_factory(engine)
        self.storage = FsStorage(root)
        self.files = StorageFileStoreAdapter(storage=self.storage)
        self.community_id = community_id
        self.server_id = server_id

    def uow(self) -> ServersUnitOfWork:
        return ServersUnitOfWork(self.factory, InProcessRealTimeEvents())

    async def generation(self) -> int:
        return await self.storage.current_generation(
            StorageCommunityId(self.community_id.value),
            StorageServerId(self.server_id.value),
        )

    async def hydrated(self) -> dict[str, bytes]:
        """The files the next hydrate ships to a Worker."""

        return read_tar(
            await drain(
                self.storage.open_hydrate_source(
                    StorageCommunityId(self.community_id.value),
                    StorageServerId(self.server_id.value),
                )
            )
        )

    async def start(self) -> FakeControlPlane:
        """Start on a Worker that holds the store's current generation."""

        cp = FakeControlPlane(
            place_to=_WORKER,
            held={(_WORKER, self.server_id): await self.generation()},
        )
        await StartServer(
            uow=self.uow(),
            control_plane=cp,
            clock=FakeClock(_NOW),
            jar_provisioner=FakeJarProvisioner(),
            store_generation=StorageGenerationReader(storage=self.storage),
            file_store=self.files,
        )(community_id=self.community_id, server_id=self.server_id)
        return cp


@pytest.fixture
async def world(engine: AsyncEngine, tmp_path: Path) -> _World:
    community_id, server_id = await _seed_server(engine)
    world = _World(engine, tmp_path, community_id, server_id)
    await world.files.write_file(
        community_id=community_id,
        server_id=server_id,
        rel_path="eula.txt",
        content=b"eula=true\n",
    )
    return world


async def _attached_op_group(world: _World, player: uuid.UUID) -> GroupId:
    """An OP group holding ``player``, attached while the server is at rest."""

    group = await CreateGroup(uow=world.uow())(
        community_id=world.community_id, name="admins", kind="op"
    )
    await AddPlayer(uow=world.uow(), file_store=world.files)(
        community_id=world.community_id,
        group_id=group.id,
        player_uuid=player,
        username="removed_player",
    )
    await AttachGroup(uow=world.uow(), file_store=world.files)(
        community_id=world.community_id,
        group_id=group.id,
        server_id=world.server_id,
    )
    return group.id


# 16 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(16 * 10 + 30)
async def test_operator_removed_while_running_is_gone_from_the_next_launch(
    world: _World,
) -> None:
    player = uuid.uuid4()
    group_id = await _attached_op_group(world, player)
    assert [e["uuid"] for e in json.loads((await world.hydrated())["ops.json"])] == [
        str(player)
    ]

    await _set_state(world.engine, world.server_id, "running")
    group = await RemovePlayer(uow=world.uow(), file_store=world.files)(
        community_id=world.community_id,
        group_id=group_id,
        player_uuid=player,
    )
    assert group.players == []
    # Not written while it runs: the stored file still names the operator.
    assert json.loads((await world.hydrated())["ops.json"]) != []
    await _set_state(world.engine, world.server_id, "stopped")

    cp = await world.start()

    assert json.loads((await world.hydrated())["ops.json"]) == []
    # The Worker held the generation the store was at before this start, and
    # would have skipped its hydrate and booted the old file from its scratch.
    assert cp.dispatched == [
        ("hydrate", _WORKER, world.server_id),
        ("start", _WORKER, world.server_id),
    ]
    async with world.uow() as uow:
        assert await uow.groups.list_sync_pending(world.server_id) == []


# 16 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(16 * 10 + 30)
async def test_group_deleted_while_running_empties_the_next_launch(
    world: _World,
) -> None:
    group_id = await _attached_op_group(world, uuid.uuid4())

    await _set_state(world.engine, world.server_id, "running")
    await DeleteGroup(uow=world.uow(), file_store=world.files)(
        community_id=world.community_id,
        group_id=group_id,
    )
    await _set_state(world.engine, world.server_id, "stopped")

    await world.start()

    assert json.loads((await world.hydrated())["ops.json"]) == []


# 10 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(10 * 10 + 30)
async def test_start_after_an_at_rest_edit_writes_nothing_more(world: _World) -> None:
    # The at-rest edit already wrote the file; the start finds it current, so it
    # neither advances the generation again nor forces a hydrate of its own.
    await _attached_op_group(world, uuid.uuid4())
    before = await world.generation()

    cp = await world.start()

    assert await world.generation() == before
    assert cp.dispatched == [("start", _WORKER, world.server_id)]
    async with world.uow() as uow:
        assert await uow.groups.list_sync_pending(world.server_id) == []


async def _operator_removed_while_running(world: _World, by: str) -> None:
    """Remove the only operator while the server runs, then stop it again.

    ``by`` says how: the player leaves the op group, or the group, the server's
    only op group, is detached or deleted.
    """

    player = uuid.uuid4()
    group_id = await _attached_op_group(world, player)
    await _set_state(world.engine, world.server_id, "running")
    if by == "remove_player":
        await RemovePlayer(uow=world.uow(), file_store=world.files)(
            community_id=world.community_id,
            group_id=group_id,
            player_uuid=player,
        )
    elif by == "detach_group":
        await DetachGroup(uow=world.uow(), file_store=world.files)(
            community_id=world.community_id,
            group_id=group_id,
            server_id=world.server_id,
        )
    else:
        await DeleteGroup(uow=world.uow(), file_store=world.files)(
            community_id=world.community_id,
            group_id=group_id,
        )
    await _set_state(world.engine, world.server_id, "stopped")


# 16 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(16 * 10 + 30)
@pytest.mark.parametrize("by", ["remove_player", "detach_group", "delete_group"])
async def test_a_start_that_finds_the_server_live_leaves_the_change_owed(
    world: _World, by: str
) -> None:
    # Issue #3865. The start regenerates the owed file and clears its mark, then
    # the Worker refuses the hydrate: it already runs the server, on a working set
    # with the old file, which its snapshots will publish. The server is kept on
    # that Worker, and the change is owed again in the same transaction. Once the
    # only op group is detached or deleted, the cleared mark is all that says so.
    await _operator_removed_while_running(world, by)
    cp = FakeControlPlane(
        place_to=_WORKER,
        outcomes={
            "hydrate": CommandOutcome(
                status=worker_status("HydrateTrigger", "instance_running"),
                message="instancemanager: server already running",
            )
        },
    )

    with pytest.raises(InvalidLifecycleTransitionError):
        await StartServer(
            uow=world.uow(),
            control_plane=cp,
            clock=FakeClock(_NOW + dt.timedelta(minutes=1)),
            jar_provisioner=FakeJarProvisioner(),
            store_generation=StorageGenerationReader(storage=world.storage),
            file_store=world.files,
        )(community_id=world.community_id, server_id=world.server_id)

    async with world.uow() as uow:
        owed = await uow.groups.list_sync_pending(world.server_id)
        server = await uow.servers.get_by_id(world.server_id)
    assert [mark.kind for mark in owed] == [GroupKind.OP]
    assert server is not None
    assert server.assigned_worker_id == _WORKER
    assert server.observed_state is ObservedState.RUNNING


# 16 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(16 * 10 + 30)
@pytest.mark.parametrize("by", ["remove_player", "detach_group", "delete_group"])
async def test_a_start_kept_on_a_busy_worker_leaves_the_change_owed(
    world: _World, by: str
) -> None:
    # Issue #3890. The start regenerates the owed file and clears its mark, then
    # the Worker refuses the hydrate BUSY: another command of the server holds the
    # id there and may still launch it, on a working set with the old file. The
    # server is kept on that Worker with nothing launched, so the change is owed
    # again in the transaction that records observed=unknown.
    await _operator_removed_while_running(world, by)
    cp = FakeControlPlane(
        place_to=_WORKER,
        outcomes={
            "hydrate": CommandOutcome(
                status=worker_status("HydrateTrigger", "command_in_flight"),
                message="instancemanager: a lifecycle command is already in flight",
            )
        },
    )

    with pytest.raises(CommandDispatchError):
        await StartServer(
            uow=world.uow(),
            control_plane=cp,
            clock=FakeClock(_NOW + dt.timedelta(minutes=1)),
            jar_provisioner=FakeJarProvisioner(),
            store_generation=StorageGenerationReader(storage=world.storage),
            file_store=world.files,
        )(community_id=world.community_id, server_id=world.server_id)

    async with world.uow() as uow:
        owed = await uow.groups.list_sync_pending(world.server_id)
        server = await uow.servers.get_by_id(world.server_id)
    assert [mark.kind for mark in owed] == [GroupKind.OP]
    assert server is not None
    assert server.assigned_worker_id == _WORKER
    assert server.observed_state is ObservedState.UNKNOWN
