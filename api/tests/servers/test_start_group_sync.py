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

from mc_server_dashboard_api.servers.application.groups import (
    DeleteGroup,
    DetachGroup,
    RemovePlayer,
)
from mc_server_dashboard_api.servers.application.lifecycle import (
    RestartServer,
    StartServer,
    StopServer,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    CommandDispatchError,
    InvalidLifecycleTransitionError,
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
from tests.servers.contract_table import worker_status
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

    async def uploads_settled(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        worker_id: WorkerId,
        timeout: float,
    ) -> bool:
        return True


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


async def _operator_removed_while_running(
    uow: FakeUnitOfWork, server: Server, *, by: str = "remove_player"
) -> None:
    """Remove the only operator while ``server`` runs, then stop it again.

    ``by`` says how: the player leaves the op group, or the group itself, the
    server's only op group, is detached or deleted (issue #3865).
    """

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
    if by == "remove_player":
        await RemovePlayer(uow=uow, file_store=FakeFileStore())(
            community_id=_COMMUNITY, group_id=group.id, player_uuid=_REMOVED
        )
    elif by == "detach_group":
        await DetachGroup(uow=uow, file_store=FakeFileStore())(
            community_id=_COMMUNITY, group_id=group.id, server_id=server.id
        )
    else:
        await DeleteGroup(uow=uow, file_store=FakeFileStore())(
            community_id=_COMMUNITY, group_id=group.id
        )
    server.desired_state, server.observed_state = stopped
    uow.servers.seed(server)


def _start(
    uow: FakeUnitOfWork,
    store: _VersionedFileStore,
    cp: FakeControlPlane,
    jar_provisioner: JarProvisioner | None = None,
    clock: FakeClock | None = None,
) -> StartServer:
    return StartServer(
        uow=uow,
        control_plane=cp,
        clock=clock or FakeClock(_NOW),
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


async def test_a_group_edit_during_the_final_snapshot_is_left_to_the_start() -> None:
    # The group fan-out skips a server at rest that a Worker still holds, as it
    # skips a running one (issue #3998): the final snapshot publishes over the
    # store and would overwrite the write. The change stays owed, and the start
    # that follows the release makes it.
    uow = FakeUnitOfWork()
    server = _server(worker=_WORKER)
    uow.servers.seed(server)
    group = PlayerGroup(
        id=GroupId.new(),
        community_id=_COMMUNITY,
        name=GroupName("admins"),
        kind=GroupKind.OP,
        players=[Player(_REMOVED, "removed")],
    )
    uow.groups.seed(group)
    await uow.groups.attach(group.id, server.id)
    store = _VersionedFileStore()

    await RemovePlayer(uow=uow, file_store=store)(
        community_id=_COMMUNITY, group_id=group.id, player_uuid=_REMOVED
    )

    assert store.files["ops.json"] == _STALE_OPS.encode()
    assert store.generation == _GENERATION
    assert [p.kind for p in await uow.groups.list_sync_pending(server.id)] == [
        GroupKind.OP
    ]

    # The final snapshot settles and the stop releases the server.
    assert await uow.servers.clear_assignment_after_final_snapshot(server.id, _WORKER)
    cp = FakeControlPlane(place_to=_WORKER, held={(_WORKER, server.id): _GENERATION})
    await _start(uow, store, cp)(community_id=_COMMUNITY, server_id=server.id)

    assert json.loads(store.files["ops.json"]) == []
    assert cp.dispatched == [
        ("hydrate", _WORKER, server.id),
        ("start", _WORKER, server.id),
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


async def test_restart_stays_in_place_and_leaves_the_change_owed() -> None:
    # An in-place restart relaunches the Worker's own working set, so it cannot
    # apply the change; it must not consume the mark either, or the next clean
    # start would have nothing left to apply (issue #3271).
    uow = FakeUnitOfWork()
    server = _server(worker=_WORKER)
    await _operator_removed_while_running(uow, server)
    server.desired_state = DesiredState.RUNNING
    server.observed_state = ObservedState.RUNNING
    uow.servers.seed(server)
    cp = FakeControlPlane(place_to=_WORKER)

    await RestartServer(uow=uow, control_plane=cp, clock=FakeClock(_NOW))(
        community_id=_COMMUNITY, server_id=server.id
    )

    assert cp.dispatched == [("restart", _WORKER, server.id)]
    assert [p.kind for p in await uow.groups.list_sync_pending(server.id)] == [
        GroupKind.OP
    ]


# --- a start whose hydrate finds the server already live (issue #3865) -------


class _WorkerAlreadyRunningIt(FakeControlPlane):
    """Worker A already runs an instance of the server, on its own working set.

    It refuses a hydrate while that instance lives, as the Worker does. Every
    snapshot it takes publishes the instance's own ``ops.json``, the stale one: the
    assigned Worker's snapshot is the one a stale-base publish is accepted from.
    """

    def __init__(self, store: _VersionedFileStore) -> None:
        super().__init__(place_to=_WORKER)
        self._store = store
        self._live = True

    async def hydrate(
        self,
        *,
        worker_id: WorkerId,
        community_id: CommunityId,
        server_id: ServerId,
        timeout_seconds: float | None = None,
    ) -> CommandOutcome:
        outcome = await super().hydrate(
            worker_id=worker_id, community_id=community_id, server_id=server_id
        )
        if not self._live:
            return outcome
        return CommandOutcome(
            status=worker_status("HydrateTrigger", "instance_running"),
            message="instancemanager: server already running",
        )

    async def stop(
        self, *, worker_id: WorkerId, server_id: ServerId, force: bool = False
    ) -> CommandOutcome:
        outcome = await super().stop(
            worker_id=worker_id, server_id=server_id, force=force
        )
        self._live = False
        return outcome

    async def snapshot(
        self,
        *,
        worker_id: WorkerId,
        community_id: CommunityId,
        server_id: ServerId,
        final: bool = False,
    ) -> CommandOutcome:
        outcome = await super().snapshot(
            worker_id=worker_id,
            community_id=community_id,
            server_id=server_id,
            final=final,
        )
        await self._store.write_file(
            community_id=community_id,
            server_id=server_id,
            rel_path="ops.json",
            content=_STALE_OPS.encode(),
        )
        return outcome


async def _adopt(entry: str, use_case: StartServer, server_id: ServerId) -> None:
    """Run the start ``entry`` names; the Worker turns out to run the server."""

    if entry == "start":
        with pytest.raises(InvalidLifecycleTransitionError):
            await use_case(community_id=_COMMUNITY, server_id=server_id)
    else:
        await use_case.place_and_start(community_id=_COMMUNITY, server_id=server_id)


@pytest.mark.parametrize("by", ["remove_player", "detach_group", "delete_group"])
@pytest.mark.parametrize("entry", ["start", "place_and_start"])
async def test_an_adopted_instance_leaves_the_owed_player_files_owed(
    entry: str, by: str
) -> None:
    # The start regenerated the owed ops.json into the store and cleared its mark
    # with the intent. Then the Worker refused the hydrate: it already runs the
    # server, on a working set holding the stale file, and keeps it. Its snapshots
    # put that file back in the store, so the regeneration must stay owed: the next
    # start, after a stop, makes it again. Once the last op group is detached or
    # deleted, no group is attached, and the mark the start cleared is the only
    # thing that says the file is owed.
    uow = FakeUnitOfWork()
    server = _server()
    await _operator_removed_while_running(uow, server, by=by)
    if entry == "place_and_start":
        server.desired_state = DesiredState.RUNNING
        uow.servers.seed(server)
    store = _VersionedFileStore()
    cp = _WorkerAlreadyRunningIt(store)
    later = FakeClock(_NOW + dt.timedelta(minutes=1))

    await _adopt(entry, _start(uow, store, cp, clock=later), server.id)
    owed_once_adopted = [p.kind for p in await uow.groups.list_sync_pending(server.id)]
    await StopServer(uow=uow, control_plane=cp, clock=later)(
        community_id=_COMMUNITY, server_id=server.id
    )
    assert json.loads(store.files["ops.json"]) != []
    await _start(uow, store, cp, clock=later)(
        community_id=_COMMUNITY, server_id=server.id
    )

    assert owed_once_adopted == [GroupKind.OP]
    assert json.loads(store.files["ops.json"]) == []
    assert await uow.groups.list_sync_pending(server.id) == []


async def _with_op_group_attached(uow: FakeUnitOfWork, server: Server) -> None:
    group = PlayerGroup(
        id=GroupId.new(),
        community_id=_COMMUNITY,
        name=GroupName("admins"),
        kind=GroupKind.OP,
        players=[Player(uuid.uuid4(), "admin")],
    )
    uow.groups.seed(group)
    await uow.groups.attach(group.id, server.id)


@pytest.mark.parametrize("entry", ["start", "place_and_start"])
@pytest.mark.parametrize(
    ("attached", "owed"), [(True, [GroupKind.OP]), (False, [])], ids=["op", "none"]
)
async def test_an_adopted_instance_owes_every_group_managed_file(
    entry: str, attached: bool, owed: list[GroupKind]
) -> None:
    # Nothing is owed when this start runs: an earlier start that never launched on
    # this Worker cleared the mark of a change the store holds and the instance
    # does not. So a file a group manages is owed again on adoption whether or not
    # this start cleared it. A file no group manages stays as the game left it.
    uow = FakeUnitOfWork()
    server = _server()
    if entry == "place_and_start":
        server.desired_state = DesiredState.RUNNING
    uow.servers.seed(server)
    if attached:
        await _with_op_group_attached(uow, server)
    store = _VersionedFileStore()

    await _adopt(entry, _start(uow, store, _WorkerAlreadyRunningIt(store)), server.id)

    assert [p.kind for p in await uow.groups.list_sync_pending(server.id)] == owed


class _LaunchInFlightThenLive(_WorkerAlreadyRunningIt):
    """Worker A is still launching the server for an earlier command (issue #3890).

    It refuses the first hydrate ``BUSY``, on that launch's reservation, and then
    runs the instance the launch started, on its own working set, as
    :class:`_WorkerAlreadyRunningIt` does.
    """

    def __init__(self, store: _VersionedFileStore) -> None:
        super().__init__(store)
        self._launching = True

    async def hydrate(
        self,
        *,
        worker_id: WorkerId,
        community_id: CommunityId,
        server_id: ServerId,
        timeout_seconds: float | None = None,
    ) -> CommandOutcome:
        if not self._launching:
            return await super().hydrate(
                worker_id=worker_id, community_id=community_id, server_id=server_id
            )
        self._launching = False
        await FakeControlPlane.hydrate(
            self, worker_id=worker_id, community_id=community_id, server_id=server_id
        )
        return CommandOutcome(
            status=worker_status("HydrateTrigger", "command_in_flight"),
            message="instancemanager: a lifecycle command is already in flight",
        )


@pytest.mark.parametrize("by", ["remove_player", "detach_group", "delete_group"])
@pytest.mark.parametrize("entry", ["start", "place_and_start"])
async def test_a_start_kept_on_a_busy_worker_leaves_the_owed_player_files_owed(
    entry: str, by: str
) -> None:
    # Issue #3890. The start regenerated the owed ops.json into the store and
    # cleared its mark with the intent. Then Worker A refused the hydrate BUSY: an
    # earlier launch of the server holds the id there. The start keeps A and
    # launches nothing, so the regeneration is owed again. That launch then runs
    # on A's own working set, with the stale file. The reconciler's re-sent start
    # finds it live and keeps it, and its snapshots put the stale file back in the
    # store. So the next start, after a stop, must make the change again.
    uow = FakeUnitOfWork()
    server = _server()
    await _operator_removed_while_running(uow, server, by=by)
    if entry == "place_and_start":
        server.desired_state = DesiredState.RUNNING
        uow.servers.seed(server)
    store = _VersionedFileStore()
    cp = _LaunchInFlightThenLive(store)
    clock = FakeClock(_NOW + dt.timedelta(minutes=1))
    use_case = _start(uow, store, cp, clock=clock)

    with pytest.raises(CommandDispatchError):
        if entry == "start":
            await use_case(community_id=_COMMUNITY, server_id=server.id)
        else:
            await use_case.place_and_start(community_id=_COMMUNITY, server_id=server.id)
    owed_once_kept = [p.kind for p in await uow.groups.list_sync_pending(server.id)]
    clock.set(_NOW + dt.timedelta(minutes=20))
    await use_case.redispatch_start(community_id=_COMMUNITY, server_id=server.id)
    await StopServer(uow=uow, control_plane=cp, clock=clock)(
        community_id=_COMMUNITY, server_id=server.id
    )
    await _start(uow, store, cp, clock=clock)(
        community_id=_COMMUNITY, server_id=server.id
    )

    assert owed_once_kept == [GroupKind.OP]
    assert json.loads(store.files["ops.json"]) == []
    assert await uow.groups.list_sync_pending(server.id) == []


@pytest.mark.parametrize("entry", ["start", "place_and_start"])
@pytest.mark.parametrize(
    ("attached", "owed"), [(True, [GroupKind.OP]), (False, [])], ids=["op", "none"]
)
async def test_a_start_kept_on_a_busy_worker_owes_every_group_managed_file(
    entry: str, attached: bool, owed: list[GroupKind]
) -> None:
    # Issue #3890, review r1 finding 2. Nothing is owed when this start runs, yet
    # the instance a BUSY Worker may turn out to run is the one #3888's adoption
    # owes every group-managed file for: an earlier start that never launched on
    # this Worker may have cleared a mark the instance never applied. The keep owes
    # them as the adoption does. A file no group manages stays as the game left it.
    uow = FakeUnitOfWork()
    server = _server()
    if entry == "place_and_start":
        server.desired_state = DesiredState.RUNNING
    uow.servers.seed(server)
    if attached:
        await _with_op_group_attached(uow, server)
    store = _VersionedFileStore()
    cp = FakeControlPlane(
        place_to=_WORKER,
        outcomes={
            "hydrate": CommandOutcome(
                status=worker_status("HydrateTrigger", "command_in_flight"),
                message="instancemanager: a lifecycle command is already in flight",
            )
        },
    )
    use_case = _start(uow, store, cp)

    with pytest.raises(CommandDispatchError):
        if entry == "start":
            await use_case(community_id=_COMMUNITY, server_id=server.id)
        else:
            await use_case.place_and_start(community_id=_COMMUNITY, server_id=server.id)

    assert [p.kind for p in await uow.groups.list_sync_pending(server.id)] == owed


class _FailsThenInFlightThenLive(_LaunchInFlightThenLive):
    """Worker A fails the first hydrate's transfer, then acts as its parent.

    The first start that reached it regenerated the owed file and cleared its
    mark, and was undone. The next one finds an earlier launch in flight.
    """

    def __init__(self, store: _VersionedFileStore) -> None:
        super().__init__(store)
        self._transfer_fails = True

    async def hydrate(
        self,
        *,
        worker_id: WorkerId,
        community_id: CommunityId,
        server_id: ServerId,
        timeout_seconds: float | None = None,
    ) -> CommandOutcome:
        if not self._transfer_fails:
            return await super().hydrate(
                worker_id=worker_id, community_id=community_id, server_id=server_id
            )
        self._transfer_fails = False
        await FakeControlPlane.hydrate(
            self, worker_id=worker_id, community_id=community_id, server_id=server_id
        )
        return CommandOutcome(
            status=CommandStatus.TRANSFER_FAILED, message="hydrate download failed"
        )


async def test_a_kept_start_after_an_undone_one_leaves_no_stale_operator() -> None:
    # Issue #3890, review r1 finding 2, the reviewer's probe. The operator is
    # removed while the server runs, and the op group stays attached.
    # 1. A start regenerates ops.json and clears the mark; its hydrate transfer
    #    fails and the start is undone. The mark stays cleared.
    # 2. The next start owes nothing, and Worker A refuses its hydrate BUSY: an
    #    earlier launch holds the id. The start keeps A.
    # 3. That launch turns out live with the stale file. The re-sent start adopts
    #    it, a stop publishes its ops.json, and the server is started again.
    # The keep owes the attached group's file, so the last start removes the
    # operator again.
    uow = FakeUnitOfWork()
    server = _server()
    await _operator_removed_while_running(uow, server)
    store = _VersionedFileStore()
    cp = _FailsThenInFlightThenLive(store)
    clock = FakeClock(_NOW + dt.timedelta(minutes=1))
    use_case = _start(uow, store, cp, clock=clock)

    with pytest.raises(CommandDispatchError):
        await use_case(community_id=_COMMUNITY, server_id=server.id)
    assert await uow.groups.list_sync_pending(server.id) == []
    clock.set(_NOW + dt.timedelta(minutes=2))
    with pytest.raises(CommandDispatchError):
        await use_case(community_id=_COMMUNITY, server_id=server.id)
    clock.set(_NOW + dt.timedelta(minutes=20))
    await use_case.redispatch_start(community_id=_COMMUNITY, server_id=server.id)
    await StopServer(uow=uow, control_plane=cp, clock=clock)(
        community_id=_COMMUNITY, server_id=server.id
    )
    assert json.loads(store.files["ops.json"]) != []
    await _start(uow, store, cp, clock=clock)(
        community_id=_COMMUNITY, server_id=server.id
    )

    assert json.loads(store.files["ops.json"]) == []
    assert await uow.groups.list_sync_pending(server.id) == []
