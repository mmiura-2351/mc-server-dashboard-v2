"""A stop that withdraws a start's intent after the start's re-read (issue #3739).

The start re-reads the row just before it sends (issue #3737). Two things still
got past that re-read: a stop that commits after it and reaches the Worker
first, and a stop followed by a newer start that put the server back on the same
Worker. Either way the Worker answered the stop ``SERVER_NOT_FOUND``, the stop
released the row, and the start then launched an instance nobody owns.

Each test runs the real use cases, each with its own unit of work over one shared
repository and one shared ``StopIntentCounter``, as in production. The competing
write is injected at an exact point: inside the start's re-read once the row
value is taken, inside the start's own commit, or with a stop paused just before
its write. The Worker double runs nothing concurrently. A start makes the server
live there, and a stop of a server that is not live answers ``SERVER_NOT_FOUND``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Awaitable, Callable

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.requests import Request

from mc_server_dashboard_api.dependencies import (
    get_set_worker_drain,
    get_start_server,
    get_stop_server,
)
from mc_server_dashboard_api.fleet.adapters.assigned_server_stopper import (
    ServersAssignedServerStopper,
)
from mc_server_dashboard_api.fleet.adapters.control_plane import (
    ControlPlaneState,
    GrpcControlPlane,
)
from mc_server_dashboard_api.fleet.adapters.real_time_events import (
    InProcessRealTimeEvents,
)
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.application.set_worker_drain import (
    WorkerDrainLocks,
)
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId as FleetWorkerId
from mc_server_dashboard_api.servers.adapters.control_plane import (
    FleetControlPlaneAdapter,
)
from mc_server_dashboard_api.servers.application.lifecycle import (
    StartServer,
    StopServer,
    StopServersAssignedToWorker,
)
from mc_server_dashboard_api.servers.application.stop_convergence import (
    StopConvergenceGuard,
)
from mc_server_dashboard_api.servers.application.stop_dispatch_refusals import (
    StopDispatchRefusals,
)
from mc_server_dashboard_api.servers.application.stop_intents import (
    StopIntentCounter,
)
from mc_server_dashboard_api.servers.application.unrecorded_stops import (
    UnrecordedStops,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    CommandDispatchError,
    LifecycleTransitionConflictError,
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
from tests.fleet.fakes import FakeClock as FleetFakeClock
from tests.fleet.fakes import make_worker
from tests.servers.contract_table import worker_message, worker_status
from tests.servers.fakes import (
    FakeClock,
    FakeControlPlane,
    FakeFileStore,
    FakeJarProvisioner,
    FakeServerRepository,
    FakeStoreGenerationReader,
    FakeUnitOfWork,
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_HYDRATE_FAILED = CommandOutcome(
    status=CommandStatus.TRANSFER_FAILED, message="hydrate download failed"
)

Hook = Callable[[], Awaitable[None]]


class _Repository(FakeServerRepository):
    """The shared row store, with two places to inject a competing write.

    ``after_next_read`` runs once, inside the next ``get_by_id``, after the row
    value has been taken: a write it makes is one the reader did not see.
    ``pause_next_flip`` holds the next ``update_lifecycle`` at its entry, so a stop
    that has read the row waits there, just before its write. While
    ``stage_flips`` is set, an ``update_lifecycle`` that matches is held back
    instead of written, until ``land_staged``: a commit that has not landed yet.
    """

    def __init__(self) -> None:
        super().__init__()
        self.after_next_read: Hook | None = None
        self.pause_next_flip: asyncio.Event | None = None
        self.flip_paused = asyncio.Event()
        self.stage_flips = False
        self._staged: list[tuple[Server, DesiredState, bool]] = []

    async def get_by_id(self, server_id: ServerId) -> Server | None:
        loaded = await super().get_by_id(server_id)
        hook, self.after_next_read = self.after_next_read, None
        if hook is not None:
            await hook()
        return loaded

    async def update_lifecycle(
        self,
        server: Server,
        *,
        expected_from: DesiredState,
        require_unassigned: bool = False,
    ) -> bool:
        gate, self.pause_next_flip = self.pause_next_flip, None
        if gate is not None:
            self.flip_paused.set()
            await gate.wait()
        if self.stage_flips:
            current = self.by_id.get(server.id)
            if current is None or current.desired_state is not expected_from:
                return False
            if require_unassigned and current.assigned_worker_id is not None:
                return False
            self._staged.append((server, expected_from, require_unassigned))
            return True
        return await super().update_lifecycle(
            server, expected_from=expected_from, require_unassigned=require_unassigned
        )

    async def land_staged(self) -> None:
        self.stage_flips = False
        staged, self._staged = self._staged, []
        for server, expected_from, require_unassigned in staged:
            assert await super().update_lifecycle(
                server,
                expected_from=expected_from,
                require_unassigned=require_unassigned,
            )


class _CommitRunsHook(FakeUnitOfWork):
    """A unit of work whose first commit runs ``hook`` before it returns."""

    def __init__(self, *, servers: FakeServerRepository, hook: Hook) -> None:
        super().__init__(servers=servers)
        self._hook: Hook | None = hook

    async def commit(self) -> None:
        await super().commit()
        hook, self._hook = self._hook, None
        if hook is not None:
            await hook()


class _ExitRunsHook(FakeUnitOfWork):
    """A unit of work that runs ``on_next_exit`` once, as its next block closes."""

    def __init__(self, *, servers: FakeServerRepository) -> None:
        super().__init__(servers=servers)
        self.on_next_exit: Hook | None = None

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        hook, self.on_next_exit = self.on_next_exit, None
        if hook is not None:
            await hook()


class _CommitLandsLater(FakeUnitOfWork):
    """A unit of work whose commit lands its staged writes only once ``land`` is set."""

    def __init__(self, *, servers: _Repository) -> None:
        super().__init__(servers=servers)
        self._repo = servers
        self.entered = asyncio.Event()
        self.land = asyncio.Event()

    async def commit(self) -> None:
        self.entered.set()
        await self.land.wait()
        await self._repo.land_staged()
        await super().commit()


class _CommitLandsThenRaises(FakeUnitOfWork):
    """A unit of work whose commit lands, then raises ``error`` (an ambiguous commit).

    A lost response raises a database error; a client disconnect cancels the
    request task at the same await.
    """

    def __init__(self, *, servers: FakeServerRepository, error: BaseException) -> None:
        super().__init__(servers=servers)
        self._error = error

    async def commit(self) -> None:
        await super().commit()
        raise self._error


class _Worker(FakeControlPlane):
    """One Worker, as far as these races need it.

    A start makes the server live; a stop of a live server ends it, and a stop of
    one that is not live answers ``SERVER_NOT_FOUND`` (instancemanager
    handleStop). Each hydrate takes the next entry of ``hydrate_hooks``, if any:
    it runs before the hydrate answers, and a returned outcome replaces the answer.
    Each start takes the next entry of ``start_hooks`` the same way; a start whose
    answer is replaced launches nothing. ``stop_sent`` is set once a stop has run
    on the Worker; while ``hold_stop_answer`` is set, its answer waits for it.
    """

    def __init__(self, *, place_to: WorkerId | None = None) -> None:
        super().__init__(place_to=place_to)
        self.live: set[ServerId] = set()
        self.hydrate_hooks: list[Callable[[], Awaitable[CommandOutcome | None]]] = []
        self.start_hooks: list[Callable[[], Awaitable[CommandOutcome | None]]] = []
        self.stop_sent = asyncio.Event()
        self.hold_stop_answer: asyncio.Event | None = None

    async def hydrate(
        self, *, worker_id: WorkerId, community_id: CommunityId, server_id: ServerId
    ) -> CommandOutcome:
        outcome = await super().hydrate(
            worker_id=worker_id, community_id=community_id, server_id=server_id
        )
        if self.hydrate_hooks:
            replaced = await self.hydrate_hooks.pop(0)()
            if replaced is not None:
                return replaced
        return outcome

    async def start(
        self,
        *,
        worker_id: WorkerId,
        server_id: ServerId,
        server_type: ServerType,
        jar_relpath: str,
        minecraft_version: str,
        memory_limit_bytes: int,
        cpu_millis: int,
    ) -> CommandOutcome:
        outcome = await super().start(
            worker_id=worker_id,
            server_id=server_id,
            server_type=server_type,
            jar_relpath=jar_relpath,
            minecraft_version=minecraft_version,
            memory_limit_bytes=memory_limit_bytes,
            cpu_millis=cpu_millis,
        )
        if self.start_hooks:
            replaced = await self.start_hooks.pop(0)()
            if replaced is not None:
                return replaced
        self.live.add(server_id)
        return outcome

    async def stop(
        self, *, worker_id: WorkerId, server_id: ServerId, force: bool = False
    ) -> CommandOutcome:
        await super().stop(worker_id=worker_id, server_id=server_id, force=force)
        if server_id in self.live:
            self.live.discard(server_id)
            outcome = CommandOutcome(status=CommandStatus.OK)
        else:
            outcome = CommandOutcome(
                status=worker_status("StopServer", "instance_stopped"),
                message="instancemanager: server not running",
            )
        self.stop_sent.set()
        if self.hold_stop_answer is not None:
            await self.hold_stop_answer.wait()
        return outcome


class _World:
    """One server, one Worker, and the use cases that race over them."""

    def __init__(
        self,
        *,
        desired: DesiredState = DesiredState.STOPPED,
        assigned: bool = False,
    ) -> None:
        self.community = CommunityId(uuid.uuid4())
        self.server_id = ServerId(uuid.uuid4())
        self.worker = WorkerId(uuid.uuid4())
        self.repo = _Repository()
        self.repo.seed(self.row(self.server_id, desired=desired, assigned=assigned))
        self.cp = _Worker(place_to=self.worker)
        self.intents = StopIntentCounter()

    def row(
        self, server_id: ServerId, *, desired: DesiredState, assigned: bool
    ) -> Server:
        return Server(
            id=server_id,
            community_id=self.community,
            name=ServerName("survival"),
            mc_edition="java",
            mc_version="1.21.1",
            server_type=ServerType.VANILLA,
            config={},
            desired_state=desired,
            observed_state=(
                ObservedState.UNKNOWN
                if desired is DesiredState.RUNNING
                else ObservedState.STOPPED
            ),
            observed_at=None,
            assigned_worker_id=self.worker if assigned else None,
            created_at=_NOW,
            updated_at=_NOW,
        )

    def start_server(self, uow: FakeUnitOfWork | None = None) -> StartServer:
        return StartServer(
            uow=uow or FakeUnitOfWork(servers=self.repo),
            control_plane=self.cp,
            clock=FakeClock(_NOW),
            jar_provisioner=FakeJarProvisioner(),
            store_generation=FakeStoreGenerationReader(generation=0),
            file_store=FakeFileStore(seed_eula=True),
            stop_intents=self.intents,
        )

    def stop_server(self) -> StopServer:
        return StopServer(
            uow=FakeUnitOfWork(servers=self.repo),
            control_plane=self.cp,
            clock=FakeClock(_NOW),
            stop_intents=self.intents,
        )

    async def start(self, server_id: ServerId | None = None) -> Server:
        return await self.start_server()(
            community_id=self.community, server_id=server_id or self.server_id
        )

    async def stop(self, server_id: ServerId | None = None) -> Server:
        return await self.stop_server()(
            community_id=self.community, server_id=server_id or self.server_id
        )

    async def drain(self, uow: FakeUnitOfWork | None = None) -> list[ServerId]:
        return await StopServersAssignedToWorker(
            uow=uow or FakeUnitOfWork(servers=self.repo),
            clock=FakeClock(_NOW),
            stop_intents=self.intents,
        )(worker_id=self.worker)

    def after_hydrate_and_next_read(self, hook: Hook) -> None:
        """Run ``hook`` inside the read that follows the next hydrate: the re-read."""

        async def arm() -> None:
            self.repo.after_next_read = hook

        self.after_hydrate(arm)

    def after_hydrate(self, hook: Hook) -> None:
        async def run() -> None:
            await hook()

        self.cp.hydrate_hooks.append(run)

    def kinds(self) -> list[str]:
        return [kind for kind, _, _ in self.cp.dispatched]

    def stored(self) -> tuple[DesiredState, WorkerId | None]:
        row = self.repo.by_id[self.server_id]
        return row.desired_state, row.assigned_worker_id


async def test_a_stop_that_lands_during_the_re_read_keeps_the_start_unsent() -> None:
    # The window #3737 left open: the stop commits once the start's re-read has
    # taken the row, and is sent first. The Worker answers it SERVER_NOT_FOUND and
    # the stop releases the row, so a start sent now would run under (stopped,
    # unassigned). The start sees the bump instead and sends nothing.
    world = _World()

    async def stop() -> None:
        await world.stop()

    world.after_hydrate_and_next_read(stop)

    with pytest.raises(LifecycleTransitionConflictError):
        await world.start()

    assert world.kinds() == ["hydrate", "stop", "snapshot"]
    assert world.stored() == (DesiredState.STOPPED, None)
    assert world.cp.live == set()


async def test_a_re_sent_start_sees_a_stop_that_was_waiting_on_its_write() -> None:
    # A re-sent start reads an intent another start committed, so a stop of that
    # intent can already be past its own read when the re-send begins. Here the
    # stop waits just before its write, the re-send reads the row as running, and
    # the stop then commits and is sent while the re-send re-reads. A bump made
    # before the write would precede the re-send's read of the counter, and the
    # start would follow the stop out. The bump follows the commit, so it is seen.
    world = _World(desired=DesiredState.RUNNING, assigned=True)
    write = asyncio.Event()
    world.repo.pause_next_flip = write
    stop = asyncio.create_task(world.stop())
    await world.repo.flip_paused.wait()

    async def stop_completes() -> None:
        write.set()
        await stop

    world.after_hydrate_and_next_read(stop_completes)

    with pytest.raises(LifecycleTransitionConflictError):
        await world.start_server().redispatch_start(
            community_id=world.community, server_id=world.server_id
        )

    assert world.kinds() == ["hydrate", "stop", "snapshot"]
    assert world.stored() == (DesiredState.STOPPED, None)
    assert world.cp.live == set()


async def test_a_re_sent_start_reads_the_counter_before_it_finds_the_intent() -> None:
    # The re-sent start's first read finds the intent it re-sends. A stop and a
    # newer start on the same Worker can land while that read's unit of work
    # closes. Read after it, the counter would already include the stop, and the
    # re-sent start would go out next to the newer start's.
    world = _World(desired=DesiredState.RUNNING, assigned=True)

    async def stop_then_restart() -> None:
        await world.stop()
        await world.start()

    world.repo.after_next_read = stop_then_restart

    with pytest.raises(LifecycleTransitionConflictError):
        await world.start_server().redispatch_start(
            community_id=world.community, server_id=world.server_id
        )

    # The stop and its release, then the newer start. The re-sent start skips its
    # hydrate (the newer one's left the Worker holding the store's generation)
    # and sends nothing.
    assert world.kinds() == ["stop", "snapshot", "hydrate", "start"]
    assert world.stored() == (DesiredState.RUNNING, world.worker)
    assert world.cp.live == {world.server_id}


async def test_a_stop_that_lands_as_the_re_read_closes_keeps_the_start_unsent() -> None:
    # The re-read's unit of work awaits as it closes. A stop that commits there
    # was not seen by the re-read, so the count must be compared after the block
    # has closed, not inside it.
    world = _World()
    uow = _ExitRunsHook(servers=world.repo)

    async def stop() -> None:
        await world.stop()

    async def arm() -> None:
        uow.on_next_exit = stop

    world.after_hydrate(arm)

    with pytest.raises(LifecycleTransitionConflictError):
        await world.start_server(uow)(
            community_id=world.community, server_id=world.server_id
        )

    assert world.kinds() == ["hydrate", "stop", "snapshot"]
    assert world.stored() == (DesiredState.STOPPED, None)
    assert world.cp.live == set()


async def test_a_stop_runnable_at_the_compare_is_sent_after_the_start() -> None:
    # The other branch: the stop is ready to run when the start compares, but it
    # has not run yet. Nothing between the compare and the enqueue may await, so
    # the start goes out first, and the stop behind it is a real stop.
    world = _World()
    uow = _ExitRunsHook(servers=world.repo)
    stops: list[asyncio.Task[Server]] = []

    async def schedule_stop() -> None:
        stops.append(asyncio.create_task(world.stop()))

    async def arm() -> None:
        uow.on_next_exit = schedule_stop

    world.after_hydrate(arm)

    await world.start_server(uow)(
        community_id=world.community, server_id=world.server_id
    )
    await stops[0]

    assert world.kinds() == ["hydrate", "start", "stop", "snapshot"]
    assert world.stored() == (DesiredState.STOPPED, None)
    assert world.cp.live == set()


async def test_a_stop_still_waiting_on_the_worker_has_already_bumped() -> None:
    # The stop is on the Worker, which has answered nothing yet, when the start
    # compares. It must have bumped before it was sent: a stop that bumped only on
    # the answer would let the start follow it out.
    world = _World()
    answer = asyncio.Event()
    world.cp.hold_stop_answer = answer
    stops: list[asyncio.Task[Server]] = []

    async def stop_is_sent() -> None:
        stops.append(asyncio.create_task(world.stop()))
        await world.cp.stop_sent.wait()

    world.after_hydrate_and_next_read(stop_is_sent)

    with pytest.raises(LifecycleTransitionConflictError):
        await world.start()
    answer.set()
    await stops[0]

    assert world.kinds() == ["hydrate", "stop", "snapshot"]
    assert world.stored() == (DesiredState.STOPPED, None)
    assert world.cp.live == set()


@pytest.mark.parametrize(
    "error",
    [ConnectionError("commit landed, response lost"), asyncio.CancelledError()],
    ids=["lost-response", "cancelled"],
)
async def test_a_stop_whose_commit_landed_but_raised_keeps_the_start_unsent(
    error: BaseException,
) -> None:
    # An ambiguous commit: the flip lands, then the await raises, and no stop is
    # sent. The flip still bumps. Otherwise the start would go out under the
    # stopped row, and an operator who retried the stop before the Worker's first
    # report would release the row under it: the retry takes the final snapshot
    # and clears the assignment, and sends no stop.
    world = _World()

    async def ambiguous_stop() -> None:
        stop = StopServer(
            uow=_CommitLandsThenRaises(servers=world.repo, error=error),
            control_plane=world.cp,
            clock=FakeClock(_NOW),
            stop_intents=world.intents,
        )
        with pytest.raises(type(error)):
            await stop(community_id=world.community, server_id=world.server_id)

    world.after_hydrate_and_next_read(ambiguous_stop)

    with pytest.raises(LifecycleTransitionConflictError):
        await world.start()
    assert world.kinds() == ["hydrate"]
    assert world.stored() == (DesiredState.STOPPED, world.worker)

    await world.stop()

    assert world.kinds() == ["hydrate", "snapshot"]
    assert world.stored() == (DesiredState.STOPPED, None)
    assert world.cp.live == set()


@pytest.mark.parametrize(
    "error",
    [ConnectionError("commit landed, response lost"), asyncio.CancelledError()],
    ids=["lost-response", "cancelled"],
)
async def test_a_drain_whose_commit_landed_but_raised_keeps_the_start_unsent(
    error: BaseException,
) -> None:
    world = _World()

    async def ambiguous_drain() -> None:
        with pytest.raises(type(error)):
            await world.drain(_CommitLandsThenRaises(servers=world.repo, error=error))

    world.after_hydrate_and_next_read(ambiguous_drain)

    with pytest.raises(LifecycleTransitionConflictError):
        await world.start()

    assert world.kinds() == ["hydrate"]
    assert world.stored() == (DesiredState.STOPPED, world.worker)
    assert world.cp.live == set()


async def test_a_re_sent_start_sees_a_drain_that_was_waiting_on_its_commit() -> None:
    # The drain's form of the stop test above: its commit is in flight while a
    # re-send reads the count and the row, and lands while the re-send re-reads. A
    # drain that bumped before its commit would have bumped before the re-send
    # read the count, and the start would go out under the drained row.
    world = _World(desired=DesiredState.RUNNING, assigned=True)
    world.repo.stage_flips = True
    uow = _CommitLandsLater(servers=world.repo)
    drain = asyncio.create_task(world.drain(uow))
    await uow.entered.wait()

    async def drain_lands() -> None:
        uow.land.set()
        assert await drain == [world.server_id]

    world.after_hydrate_and_next_read(drain_lands)

    with pytest.raises(LifecycleTransitionConflictError):
        await world.start_server().redispatch_start(
            community_id=world.community, server_id=world.server_id
        )

    assert world.kinds() == ["hydrate"]
    assert world.stored() == (DesiredState.STOPPED, world.worker)
    assert world.cp.live == set()


async def test_the_working_set_replay_compares_the_count_its_start_read() -> None:
    # The #2499 replay: the Worker refuses a skip-hydrate start because the working
    # set is gone, and the start is re-sent with a hydrate. A stop and a newer
    # start on the same Worker land while the refused start is out. The replay
    # must compare the count the start read at first, not one read at the replay.
    world = _World(desired=DesiredState.RUNNING, assigned=True)
    world.cp.record_held_generation(
        worker_id=world.worker, server_id=world.server_id, generation=0
    )

    async def stop_and_restart_then_refuse() -> CommandOutcome:
        await world.stop()
        await world.start()
        return CommandOutcome(
            status=worker_status("StartServer", "working_set_absent"),
            message=worker_message("working_set_absent.launch")
            % "/var/lib/mcsd/scratch/s",
        )

    world.cp.start_hooks.append(stop_and_restart_then_refuse)

    with pytest.raises(LifecycleTransitionConflictError):
        await world.start_server().redispatch_start(
            community_id=world.community, server_id=world.server_id
        )

    # The refused start, the stop and its release, the newer start, then the
    # replay's hydrate. The replay sends no start.
    assert world.kinds() == ["start", "stop", "snapshot", "start", "hydrate"]
    assert world.stored() == (DesiredState.RUNNING, world.worker)
    assert world.cp.live == {world.server_id}


async def test_a_start_whose_server_was_re_placed_on_its_worker_is_not_sent() -> None:
    # The re-read cannot see this one: a stop released the row during the start's
    # hydrate, and a newer start put it back on the SAME Worker. The row reads
    # desired=running on this start's Worker again, but the intent is the newer
    # start's. On a single-Worker deployment every re-placement looks like this.
    world = _World()

    async def stop_then_restart() -> None:
        await world.stop()
        await world.start()

    world.after_hydrate(stop_then_restart)

    with pytest.raises(LifecycleTransitionConflictError):
        await world.start()

    # The first hydrate, the stop and its release, then the newer start. One start.
    assert world.kinds() == ["hydrate", "stop", "snapshot", "hydrate", "start"]
    assert world.stored() == (DesiredState.RUNNING, world.worker)
    assert world.cp.live == {world.server_id}


async def test_a_start_reads_the_counter_before_its_own_commit() -> None:
    # The same re-placement, landing while the start's own commit returns. Read
    # after that commit, the counter would already include the stop, and the
    # start would go out next to the newer start's.
    world = _World()

    async def stop_then_restart() -> None:
        await world.stop()
        await world.start()

    start = world.start_server(
        _CommitRunsHook(servers=world.repo, hook=stop_then_restart)
    )

    with pytest.raises(LifecycleTransitionConflictError):
        await start(community_id=world.community, server_id=world.server_id)

    # All inside the first start's commit: the stop and its release, then the newer
    # start. The first start then skips its hydrate (the newer one's hydrate left
    # the Worker holding the store's generation) and sends nothing.
    assert world.kinds() == ["stop", "snapshot", "hydrate", "start"]
    assert world.stored() == (DesiredState.RUNNING, world.worker)
    assert world.cp.live == {world.server_id}


async def test_a_placement_reads_the_counter_before_its_own_commit() -> None:
    # The reconciler's placement of an unassigned desired-running row commits its
    # assignment the same way, so the same re-placement inside that commit must
    # hold it back too.
    world = _World(desired=DesiredState.RUNNING)

    async def stop_then_restart() -> None:
        await world.stop()
        await world.start()

    place = world.start_server(
        _CommitRunsHook(servers=world.repo, hook=stop_then_restart)
    )

    with pytest.raises(LifecycleTransitionConflictError):
        await place.place_and_start(
            community_id=world.community, server_id=world.server_id
        )

    # The placement always hydrates; it then sends nothing.
    assert world.kinds() == ["stop", "snapshot", "hydrate", "start", "hydrate"]
    assert world.stored() == (DesiredState.RUNNING, world.worker)
    assert world.cp.live == {world.server_id}


async def test_a_drain_that_lands_after_the_re_read_keeps_the_start_unsent() -> None:
    # A drain withdraws the intent without sending a stop (the reconciler sends
    # it later). The start is not sent under the drained row either.
    world = _World()

    async def drain() -> None:
        assert await world.drain() == [world.server_id]

    world.after_hydrate_and_next_read(drain)

    with pytest.raises(LifecycleTransitionConflictError):
        await world.start()

    assert world.kinds() == ["hydrate"]
    assert world.stored() == (DesiredState.STOPPED, world.worker)
    assert world.cp.live == set()


async def test_a_stop_of_another_server_does_not_hold_the_start_back() -> None:
    world = _World()
    other = ServerId(uuid.uuid4())
    world.repo.seed(world.row(other, desired=DesiredState.RUNNING, assigned=True))

    async def stop_other() -> None:
        await world.stop(other)

    world.after_hydrate_and_next_read(stop_other)

    await world.start()

    assert world.kinds() == ["hydrate", "stop", "snapshot", "start"]
    assert world.stored() == (DesiredState.RUNNING, world.worker)
    assert world.cp.live == {world.server_id}


async def test_a_failed_start_reverting_a_newer_intent_keeps_that_start_unsent() -> (
    None
):
    # The interleaving of issue #3741, closed here only where it meets the window.
    # Start B's hydrate is in flight, a stop releases B's row, and start A places
    # it again. B's hydrate then fails while A re-reads, and B's compensation
    # reverts A's intent: (stopped, unassigned), with nothing sent to stop A's
    # start. The compensation bumps, so A sends nothing.
    world = _World()
    b_hydrating = asyncio.Event()
    b_fails = asyncio.Event()

    async def b_hydrate_fails_later() -> CommandOutcome:
        b_hydrating.set()
        await b_fails.wait()
        return _HYDRATE_FAILED

    world.cp.hydrate_hooks.append(b_hydrate_fails_later)
    b = asyncio.create_task(world.start())
    await b_hydrating.wait()
    await world.stop()

    async def b_compensates() -> None:
        b_fails.set()
        with pytest.raises(CommandDispatchError):
            await b

    world.after_hydrate_and_next_read(b_compensates)

    with pytest.raises(LifecycleTransitionConflictError):
        await world.start()

    assert world.kinds() == ["hydrate", "stop", "snapshot", "hydrate"]
    assert world.stored() == (DesiredState.STOPPED, None)
    assert world.cp.live == set()


async def test_a_failed_placement_undoing_a_newer_intent_keeps_that_start_unsent() -> (
    None
):
    # The same interleaving through the reconciler's placement of an unassigned
    # row: its undo clears the assignment of start A's intent on the same Worker.
    # A start sent now would run under (running, unassigned), and the next tick
    # would place a second instance. The undo bumps, so A sends nothing.
    world = _World(desired=DesiredState.RUNNING)
    p_hydrating = asyncio.Event()
    p_fails = asyncio.Event()

    async def p_hydrate_fails_later() -> CommandOutcome:
        p_hydrating.set()
        await p_fails.wait()
        return _HYDRATE_FAILED

    world.cp.hydrate_hooks.append(p_hydrate_fails_later)
    p = asyncio.create_task(
        world.start_server().place_and_start(
            community_id=world.community, server_id=world.server_id
        )
    )
    await p_hydrating.wait()
    await world.stop()

    async def p_unassigns() -> None:
        p_fails.set()
        with pytest.raises(CommandDispatchError):
            await p

    world.after_hydrate_and_next_read(p_unassigns)

    with pytest.raises(LifecycleTransitionConflictError):
        await world.start()

    assert world.kinds() == ["hydrate", "stop", "snapshot", "hydrate"]
    assert world.stored() == (DesiredState.RUNNING, None)
    assert world.cp.live == set()


async def test_the_start_command_is_enqueued_before_the_dispatch_first_suspends() -> (
    None
):
    # The start's last check is synchronous with this enqueue only because nothing
    # between the call and the outbound queue awaits. Step the real dispatch once:
    # the command must already be on the session's queue.
    worker = FleetWorkerId(str(uuid.uuid4()))
    registry = InMemoryWorkerRegistry(
        clock=FleetFakeClock(_NOW), heartbeat_timeout=dt.timedelta(seconds=30)
    )
    session = registry.register(make_worker(worker_id=worker.value, at=_NOW))
    state = ControlPlaneState()
    queue = state.open_session(worker, session)
    cp = FleetControlPlaneAdapter(
        registry=registry,
        control_plane=GrpcControlPlane(
            state, clock=FleetFakeClock(_NOW), timeout_seconds=30
        ),
        data_plane_base_url="http://data-plane.test",
        worker_credential="test-credential",
    )

    dispatch = cp.start(
        worker_id=WorkerId(uuid.UUID(worker.value)),
        server_id=ServerId(uuid.uuid4()),
        server_type=ServerType.VANILLA,
        jar_relpath="server.jar",
        minecraft_version="1.21.1",
        memory_limit_bytes=0,
        cpu_millis=0,
    )
    try:
        dispatch.send(None)
        assert queue.qsize() == 1
        assert queue.get_nowait().api_command.WhichOneof("command") == "start"
    finally:
        dispatch.close()


async def test_the_http_start_stop_and_drain_share_the_app_counter() -> None:
    # The counter only works if the start and the stops bump and read the same
    # instance; a construction site left on its private default fails silently.
    app = FastAPI()
    app.state.stop_intents = StopIntentCounter()
    app.state.engine = create_async_engine("postgresql+asyncpg://u:p@h/db")
    app.state.storage = object()
    app.state.real_time_events = InProcessRealTimeEvents()
    app.state.stop_dispatch_refusals = StopDispatchRefusals()
    app.state.unrecorded_stops = UnrecordedStops()
    app.state.stop_convergence = StopConvergenceGuard()
    app.state.worker_drain_locks = WorkerDrainLocks()
    request = Request(scope={"type": "http", "app": app})
    registry = InMemoryWorkerRegistry(
        clock=FleetFakeClock(_NOW), heartbeat_timeout=dt.timedelta(seconds=30)
    )

    try:
        start = get_start_server(
            request,
            control_plane=FakeControlPlane(),
            ensure_jar=object(),  # type: ignore[arg-type]
            file_store=FakeFileStore(),
        )
        stop = get_stop_server(request, control_plane=FakeControlPlane())
        drain = get_set_worker_drain(request, registry=registry)
    finally:
        await app.state.engine.dispose()

    assert start.stop_intents is app.state.stop_intents
    assert stop.stop_intents is app.state.stop_intents
    assert isinstance(drain.stopper, ServersAssignedServerStopper)
    assert drain.stopper.stop_servers.stop_intents is app.state.stop_intents
