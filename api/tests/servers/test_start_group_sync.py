"""StartServer applies the player-group file changes a server is owed (issue #3223).

A group change made while a server runs cannot be written to its authoritative
``ops.json`` / ``whitelist.json``, so it is recorded as owed and the next start
makes it. These tests pin that the start regenerates the owed file before it
decides whether to hydrate, that a same-worker start therefore cannot boot the
stale scratch, and that a start which cannot make the write does not launch.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid

import pytest

from mc_server_dashboard_api.servers.application.groups import RemovePlayer
from mc_server_dashboard_api.servers.application.lifecycle import (
    RestartServer,
    StartServer,
    StopServer,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    LifecycleTransitionConflictError,
    WorkingSetSeedFailedError,
)
from mc_server_dashboard_api.servers.domain.groups import (
    GroupId,
    GroupKind,
    GroupName,
    Player,
    PlayerGroup,
)
from mc_server_dashboard_api.servers.domain.jar_provisioner import (
    JarProvisioner,
    ProvisionedJar,
)
from mc_server_dashboard_api.servers.domain.store_generation import (
    StoreGenerationReader,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)
from tests.servers.fakes import (
    FakeClock,
    FakeControlPlane,
    FakeFileStore,
    FakeJarProvisioner,
    FakeUnitOfWork,
)

_NOW = dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.timezone.utc)
_COMMUNITY = CommunityId(uuid.uuid4())
_WORKER = WorkerId(uuid.uuid4())
_REMOVED = uuid.uuid4()
# The generation the store and the Worker's retained scratch both sit at once the
# previous run's final snapshot has published: the same-worker skip-hydrate case.
_GENERATION = 7
_STALE_OPS = json.dumps([{"uuid": str(_REMOVED), "name": "removed", "level": 4}])


class _VersionedFileStore(FakeFileStore, StoreGenerationReader):
    """A file store whose writes advance the generation, as Storage's do (#889)."""

    def __init__(self, *, fail_write: bool = False) -> None:
        super().__init__(fail_write=fail_write, seed_eula=True)
        self.generation = _GENERATION
        self.files["ops.json"] = _STALE_OPS.encode()

    async def write_file(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: str,
        content: bytes,
    ) -> None:
        await super().write_file(
            community_id=community_id,
            server_id=server_id,
            rel_path=rel_path,
            content=content,
        )
        self.generation += 1

    async def current_generation(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> int:
        return self.generation


def _server(*, worker: WorkerId | None = None) -> Server:
    return Server(
        id=ServerId.new(),
        community_id=_COMMUNITY,
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=_NOW,
        assigned_worker_id=worker,
        created_at=_NOW,
        updated_at=_NOW,
    )


async def _operator_removed_while_running(uow: FakeUnitOfWork, server: Server) -> None:
    """Remove the only operator while ``server`` runs, then stop it again."""

    group = PlayerGroup(
        id=GroupId.new(),
        community_id=_COMMUNITY,
        name=GroupName("admins"),
        kind=GroupKind.OP,
        players=[Player(_REMOVED, "removed")],
    )
    uow.groups.seed(group)
    await uow.groups.attach(group.id, server.id)
    stopped = (server.desired_state, server.observed_state)
    server.desired_state = DesiredState.RUNNING
    server.observed_state = ObservedState.RUNNING
    uow.servers.seed(server)
    await RemovePlayer(uow=uow, file_store=FakeFileStore())(
        community_id=_COMMUNITY, group_id=group.id, player_uuid=_REMOVED
    )
    server.desired_state, server.observed_state = stopped
    uow.servers.seed(server)


def _start(
    uow: FakeUnitOfWork,
    store: _VersionedFileStore,
    cp: FakeControlPlane,
    jar_provisioner: JarProvisioner | None = None,
) -> StartServer:
    return StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=jar_provisioner or FakeJarProvisioner(),
        store_generation=store,
        file_store=store,
    )


class _ReleasingJarProvisioner(FakeJarProvisioner):
    """Lets the final snapshot settle while the JAR is being provisioned.

    The stop's deferred clear takes no lifecycle lock, so it can land between a
    start's first read of the row and its compare-and-set (issue #3223 review).
    """

    def __init__(self, uow: FakeUnitOfWork, server_id: ServerId) -> None:
        super().__init__()
        self._uow = uow
        self._server_id = server_id

    async def ensure(
        self,
        *,
        server_type: str,
        version: str,
        known_key: str | None,
        known_source: str | None = None,
    ) -> ProvisionedJar:
        assert await self._uow.servers.clear_assignment_after_final_snapshot(
            self._server_id, _WORKER
        )
        return await super().ensure(
            server_type=server_type,
            version=version,
            known_key=known_key,
            known_source=known_source,
        )


async def test_start_removes_an_operator_removed_while_the_server_ran() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    await _operator_removed_while_running(uow, server)
    store = _VersionedFileStore()
    # The Worker still holds the scratch of the previous run at the store's
    # generation, so without the regeneration this start would skip the hydrate
    # and boot that scratch's ops.json.
    cp = FakeControlPlane(place_to=_WORKER, held={(_WORKER, server.id): _GENERATION})

    await _start(uow, store, cp)(community_id=_COMMUNITY, server_id=server.id)

    assert json.loads(store.files["ops.json"]) == []
    assert cp.dispatched == [
        ("hydrate", _WORKER, server.id),
        ("start", _WORKER, server.id),
    ]
    assert await uow.groups.list_sync_pending(server.id) == []


async def test_start_leaves_player_files_alone_when_nothing_is_owed() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    uow.servers.seed(server)
    store = _VersionedFileStore()
    cp = FakeControlPlane(place_to=_WORKER, held={(_WORKER, server.id): _GENERATION})

    await _start(uow, store, cp)(community_id=_COMMUNITY, server_id=server.id)

    # No group ever managed this file: it stays as the game and the operator left
    # it, and the same-worker start still skips its hydrate.
    assert store.files["ops.json"] == _STALE_OPS.encode()
    assert cp.dispatched == [("start", _WORKER, server.id)]


async def test_start_fails_without_launching_when_the_owed_write_fails() -> None:
    uow = FakeUnitOfWork()
    server = _server()
    await _operator_removed_while_running(uow, server)
    cp = FakeControlPlane(place_to=_WORKER)

    with pytest.raises(WorkingSetSeedFailedError):
        await _start(uow, _VersionedFileStore(fail_write=True), cp)(
            community_id=_COMMUNITY, server_id=server.id
        )

    stored = await uow.servers.get_by_id(server.id)
    assert stored is not None
    assert stored.desired_state is DesiredState.STOPPED
    assert stored.assigned_worker_id is None
    assert cp.dispatched == []
    assert cp.reserved == []
    # Still owed: the next start tries again.
    assert [p.kind for p in await uow.groups.list_sync_pending(server.id)] == [
        GroupKind.OP
    ]


async def test_start_does_not_write_while_the_final_snapshot_holds_the_server() -> None:
    # A stop keeps the assignment until its final snapshot settles, and a write
    # now would advance the generation under that upload. The start is refused by
    # its own compare-and-set; the owed write waits for the start that goes ahead.
    uow = FakeUnitOfWork()
    server = _server(worker=_WORKER)
    await _operator_removed_while_running(uow, server)
    store = _VersionedFileStore()

    with pytest.raises(LifecycleTransitionConflictError):
        await _start(uow, store, FakeControlPlane(place_to=_WORKER))(
            community_id=_COMMUNITY, server_id=server.id
        )

    assert store.files["ops.json"] == _STALE_OPS.encode()
    assert [p.kind for p in await uow.groups.list_sync_pending(server.id)] == [
        GroupKind.OP
    ]


async def test_start_is_refused_when_the_snapshot_hold_is_released_mid_start() -> None:
    # The start read the row while the final snapshot still held it, so it did
    # not regenerate. Were it then allowed to win its compare-and-set against the
    # row the snapshot has since released, it would launch the stale file.
    uow = FakeUnitOfWork()
    server = _server(worker=_WORKER)
    await _operator_removed_while_running(uow, server)
    store = _VersionedFileStore()
    cp = FakeControlPlane(place_to=_WORKER, held={(_WORKER, server.id): _GENERATION})

    with pytest.raises(LifecycleTransitionConflictError):
        await _start(uow, store, cp, _ReleasingJarProvisioner(uow, server.id))(
            community_id=_COMMUNITY, server_id=server.id
        )

    stored = await uow.servers.get_by_id(server.id)
    assert stored is not None
    assert stored.desired_state is DesiredState.STOPPED
    assert cp.dispatched == []
    assert [p.kind for p in await uow.groups.list_sync_pending(server.id)] == [
        GroupKind.OP
    ]


# --- the reconciler's placement of an unassigned server ----------------------


async def _orphaned_running_server(uow: FakeUnitOfWork) -> Server:
    """Desired-running with no Worker, and an operator removal still owed."""

    server = _server()
    await _operator_removed_while_running(uow, server)
    server.desired_state = DesiredState.RUNNING
    uow.servers.seed(server)
    return server


async def test_place_and_start_applies_owed_files_before_it_hydrates() -> None:
    uow = FakeUnitOfWork()
    server = await _orphaned_running_server(uow)
    store = _VersionedFileStore()
    cp = FakeControlPlane(place_to=_WORKER)

    await _start(uow, store, cp).place_and_start(
        community_id=_COMMUNITY, server_id=server.id
    )

    assert json.loads(store.files["ops.json"]) == []
    assert cp.dispatched == [
        ("hydrate", _WORKER, server.id),
        ("start", _WORKER, server.id),
    ]
    assert await uow.groups.list_sync_pending(server.id) == []


async def test_place_and_start_does_not_place_when_the_owed_write_fails() -> None:
    uow = FakeUnitOfWork()
    server = await _orphaned_running_server(uow)
    cp = FakeControlPlane(place_to=_WORKER)

    with pytest.raises(WorkingSetSeedFailedError):
        await _start(uow, _VersionedFileStore(fail_write=True), cp).place_and_start(
            community_id=_COMMUNITY, server_id=server.id
        )

    assert cp.reserved == []
    assert cp.dispatched == []
    assert [p.kind for p in await uow.groups.list_sync_pending(server.id)] == [
        GroupKind.OP
    ]


# --- restart -----------------------------------------------------------------


async def _running_server(uow: FakeUnitOfWork, *, owed: bool) -> Server:
    server = _server(worker=_WORKER)
    if owed:
        await _operator_removed_while_running(uow, server)
    server.desired_state = DesiredState.RUNNING
    server.observed_state = ObservedState.RUNNING
    uow.servers.seed(server)
    return server


def _restart(
    uow: FakeUnitOfWork, store: _VersionedFileStore, cp: FakeControlPlane
) -> RestartServer:
    return RestartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        stop_server=StopServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW)),
        start_server=_start(uow, store, cp),
    )


async def test_restart_with_owed_files_stops_regenerates_and_starts() -> None:
    uow = FakeUnitOfWork()
    server = await _running_server(uow, owed=True)
    store = _VersionedFileStore()
    cp = FakeControlPlane(place_to=_WORKER, held={(_WORKER, server.id): _GENERATION})

    result = await _restart(uow, store, cp)(
        community_id=_COMMUNITY, server_id=server.id
    )

    assert json.loads(store.files["ops.json"]) == []
    # A clean stop with its final snapshot, so nothing the Worker holds is lost
    # to the hydrate the regenerated file then forces; never the in-place restart,
    # which relaunches the Worker's stale copy.
    assert cp.dispatched == [
        ("stop", _WORKER, server.id),
        ("snapshot", _WORKER, server.id),
        ("hydrate", _WORKER, server.id),
        ("start", _WORKER, server.id),
    ]
    assert result.desired_state is DesiredState.RUNNING
    assert result.assigned_worker_id == _WORKER
    assert await uow.groups.list_sync_pending(server.id) == []


async def test_restart_with_nothing_owed_stays_in_place() -> None:
    uow = FakeUnitOfWork()
    server = await _running_server(uow, owed=False)
    store = _VersionedFileStore()
    cp = FakeControlPlane(place_to=_WORKER)

    await _restart(uow, store, cp)(community_id=_COMMUNITY, server_id=server.id)

    assert cp.dispatched == [("restart", _WORKER, server.id)]
    assert store.files["ops.json"] == _STALE_OPS.encode()
