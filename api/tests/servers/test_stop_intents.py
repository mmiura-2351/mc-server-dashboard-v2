"""A stop that withdraws a start's intent after the start's re-read (issue #3739).

The start re-reads the row just before it sends (issue #3737). Two things still
got past that re-read: a stop that commits after it and reaches the Worker
first, and a stop followed by a newer start that put the server back on the same
Worker. Either way the Worker answered the stop ``SERVER_NOT_FOUND``, the stop
released the row, and the start then launched an instance nobody owns.

The same count tells a failed start's revert whether the intent on the row is
still its own (issue #3741). Before, the revert undid whatever intent it found,
including a newer start's that had already been sent.

Each test runs the real use cases, each with its own unit of work over one shared
repository and one shared ``StopIntentCounter``, as in production. The competing
write is injected at an exact point: inside the start's re-read once the row
value is taken, inside the start's own commit, or with a stop paused just before
its write. The Worker double runs nothing concurrently. A start makes the server
live there, and a stop of a server that is not live answers ``SERVER_NOT_FOUND``.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

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
from mc_server_dashboard_api.servers.application.start_intent_lock import (
    StartIntentLock,
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
    ServerBusyError,
)
from mc_server_dashboard_api.servers.domain.lifecycle_lock import (
    LifecycleLock,
    NullLifecycleLock,
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
    """A unit of work whose ``at``-th commit lands, then raises ``error``.

    That is an ambiguous commit: a lost response raises a database error, and a
    client disconnect cancels the request task at the same await.
    """

    def __init__(
        self, *, servers: FakeServerRepository, error: BaseException, at: int = 1
    ) -> None:
        super().__init__(servers=servers)
        self._error = error
        self._at = at

    async def commit(self) -> None:
        await super().commit()
        if self.commits == self._at:
            raise self._error


class _CommitLandsThenWaits(FakeUnitOfWork):
    """A unit of work whose ``at``-th commit lands, then returns only on ``returns``."""

    def __init__(self, *, servers: FakeServerRepository, at: int) -> None:
        super().__init__(servers=servers)
        self._at = at
        self.landed = asyncio.Event()
        self.returns = asyncio.Event()

    async def commit(self) -> None:
        await super().commit()
        if self.commits == self._at:
            self.landed.set()
            await self.returns.wait()


class _ExclusiveLock(LifecycleLock):
    """A lifecycle lock that excludes, as the advisory lock does, for one server.

    While ``busy`` is set, ``hold`` gives up at once, as the advisory lock does
    once another holder has kept it past its bounded wait: a backup upload or
    delete, a prune, the integrity sweep.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.busy = False

    @contextlib.asynccontextmanager
    async def hold(self, server_id: ServerId) -> AsyncIterator[None]:
        if self.busy:
            raise ServerBusyError(str(server_id.value))
        async with self._lock:
            yield


class _IntentLock(StartIntentLock):
    """The real start-intent lock, with two places to watch it.

    ``on_next_hold`` runs once, at the entry of the next ``hold``, before the lock
    is taken. ``waited`` is set once a ``hold`` finds the lock taken.
    """

    def __init__(self) -> None:
        super().__init__()
        self.on_next_hold: Hook | None = None
        self.waited = asyncio.Event()

    @contextlib.asynccontextmanager
    async def hold(self, server_id: ServerId) -> AsyncIterator[None]:
        hook, self.on_next_hold = self.on_next_hold, None
        if hook is not None:
            await hook()
        lock = self._locks.get(server_id)
        if lock is not None and lock.locked():
            self.waited.set()
        async with super().hold(server_id):
            yield


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
        # The starts' lifecycle lock and start-intent lock. No exclusion by
        # default: some tests run a whole start inside another start's commit,
        # which the real locks forbid. ``None`` gives each use case its own.
        self.lock: LifecycleLock = NullLifecycleLock()
        self.intent_lock: StartIntentLock | None = None

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

    def start_server(
        self,
        uow: FakeUnitOfWork | None = None,
        *,
        control_plane: _Worker | None = None,
    ) -> StartServer:
        return StartServer(
            uow=uow or FakeUnitOfWork(servers=self.repo),
            control_plane=control_plane or self.cp,
            clock=FakeClock(_NOW),
            jar_provisioner=FakeJarProvisioner(),
            store_generation=FakeStoreGenerationReader(generation=0),
            file_store=FakeFileStore(seed_eula=True),
            lifecycle_lock=self.lock,
            stop_intents=self.intents,
            start_intent_lock=self.intent_lock or StartIntentLock(),
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

    async def place(self) -> Server:
        return await self.start_server().place_and_start(
            community_id=self.community, server_id=self.server_id
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


_REVERTS = pytest.mark.parametrize("kind", ["start", "placement"])


def _seeded(kind: str) -> _World:
    """A world for a start (``_compensate``) or a placement (``_unassign``).

    A user start sets a stopped row running; the reconciler's placement assigns a
    desired-running row nobody holds. The starts here share excluding locks.
    """

    world = _World(
        desired=DesiredState.STOPPED if kind == "start" else DesiredState.RUNNING
    )
    world.lock = _ExclusiveLock()
    world.intent_lock = _IntentLock()
    return world


def _intent_lock(world: _World) -> _IntentLock:
    assert isinstance(world.intent_lock, _IntentLock)
    return world.intent_lock


class _Holder:
    """Holds a world's start-intent lock, as another start's commit would."""

    def __init__(self, world: _World) -> None:
        self._lock = _intent_lock(world)
        self._server_id = world.server_id
        self._held = asyncio.Event()
        self._release = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def take(self) -> None:
        self._task = asyncio.create_task(self._hold())
        await self._held.wait()

    async def _hold(self) -> None:
        async with self._lock.hold(self._server_id):
            self._held.set()
            await self._release.wait()

    async def release(self) -> None:
        self._release.set()
        assert self._task is not None
        await self._task


async def _either(task: asyncio.Task[Any], event: asyncio.Event) -> None:
    """Wait for ``event``, or for ``task`` to end first if it never gets there."""

    waiter = asyncio.create_task(event.wait())
    await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    waiter.cancel()


async def _fails(world: _World, kind: str, uow: FakeUnitOfWork | None = None) -> None:
    """Run the start or the placement, whose hydrate fails, and expect that failure."""

    use_case = world.start_server(uow)
    with pytest.raises(CommandDispatchError):
        if kind == "start":
            await use_case(community_id=world.community, server_id=world.server_id)
        else:
            await use_case.place_and_start(
                community_id=world.community, server_id=world.server_id
            )


@_REVERTS
async def test_a_failed_revert_does_not_hold_back_a_newer_start(kind: str) -> None:
    # F hydrates; a stop releases its row, and start A places it on the same Worker
    # again. F's hydrate then fails while A re-reads. The intent is A's now, so
    # F's revert leaves it alone and adds nothing to the count: A's start goes
    # out. (Before issue #3741 F undid A's intent, and only its bump held A back.)
    world = _seeded(kind)
    f_hydrating = asyncio.Event()
    f_fails = asyncio.Event()

    async def f_hydrate_fails_later() -> CommandOutcome:
        f_hydrating.set()
        await f_fails.wait()
        return _HYDRATE_FAILED

    world.cp.hydrate_hooks.append(f_hydrate_fails_later)
    f = asyncio.create_task(_fails(world, kind))
    await f_hydrating.wait()
    await world.stop()

    async def f_reverts() -> None:
        f_fails.set()
        await f

    world.after_hydrate_and_next_read(f_reverts)

    await world.start()

    assert world.kinds() == ["hydrate", "stop", "snapshot", "hydrate", "start"]
    assert world.stored() == (DesiredState.RUNNING, world.worker)
    assert world.cp.live == {world.server_id}
    assert world.cp.decremented == [world.worker]


@_REVERTS
async def test_a_failed_revert_leaves_a_newer_start_on_its_worker_alone(
    kind: str,
) -> None:
    # Issue #3741. While F hydrates, a stop releases the row, and a newer start
    # puts it back on the same Worker and sends its start. F's hydrate then fails.
    # The row reads desired=running on F's Worker, as F left it; only the count
    # shows the intent is not F's. Undoing it would leave the instance just
    # started under a stopped or unassigned row, and F's decrement would drop the
    # newer start's entry from the Worker's placement load.
    world = _seeded(kind)

    async def stop_and_restart_then_fail() -> CommandOutcome:
        await world.stop()
        await world.start()
        return _HYDRATE_FAILED

    world.cp.hydrate_hooks.append(stop_and_restart_then_fail)

    await _fails(world, kind)

    assert world.kinds() == ["hydrate", "stop", "snapshot", "hydrate", "start"]
    assert world.stored() == (DesiredState.RUNNING, world.worker)
    assert world.cp.live == {world.server_id}
    assert world.cp.incremented == [world.worker, world.worker]
    assert world.cp.decremented == [world.worker]


@_REVERTS
async def test_a_failed_revert_leaves_a_newer_start_on_another_worker_alone(
    kind: str,
) -> None:
    # The same, with the newer start placed on another Worker. Undoing it would
    # leave the newer start's entry in the other Worker's placement load, for a
    # row that no longer runs there.
    world = _seeded(kind)
    elsewhere = WorkerId(uuid.uuid4())
    other = _Worker(place_to=elsewhere)

    async def stop_and_restart_elsewhere_then_fail() -> CommandOutcome:
        await world.stop()
        await world.start_server(control_plane=other)(
            community_id=world.community, server_id=world.server_id
        )
        return _HYDRATE_FAILED

    world.cp.hydrate_hooks.append(stop_and_restart_elsewhere_then_fail)

    await _fails(world, kind)

    assert world.kinds() == ["hydrate", "stop", "snapshot"]
    assert [k for k, _, _ in other.dispatched] == ["hydrate", "start"]
    assert world.stored() == (DesiredState.RUNNING, elsewhere)
    assert other.live == {world.server_id}
    assert world.cp.decremented == [world.worker]
    assert other.decremented == []


@pytest.mark.parametrize(
    ("kind", "reverted"),
    [("start", DesiredState.STOPPED), ("placement", DesiredState.RUNNING)],
    ids=["start", "placement"],
)
async def test_a_failed_revert_with_nothing_in_between_undoes_its_own_intent(
    kind: str, reverted: DesiredState
) -> None:
    # Nothing came between: the intent is F's own, so F undoes it as before. A
    # start reverts to stopped; a placement only gives up its assignment.
    world = _seeded(kind)

    async def fail() -> CommandOutcome:
        return _HYDRATE_FAILED

    world.cp.hydrate_hooks.append(fail)

    await _fails(world, kind)

    assert world.kinds() == ["hydrate"]
    assert world.stored() == (reverted, None)
    assert world.cp.decremented == [world.worker]
    assert world.intents.current(world.server_id) == 1


@_REVERTS
async def test_a_failed_revert_leaves_a_row_re_placed_out_of_its_sight_alone(
    kind: str,
) -> None:
    # The count sees only this process's writes. A row moved to another Worker by
    # a write it did not see (one from outside the process) keeps the count as F
    # read it, but no longer names F's Worker: F leaves it alone.
    world = _seeded(kind)
    elsewhere = WorkerId(uuid.uuid4())

    async def re_placed_unseen_then_fail() -> CommandOutcome:
        world.repo.by_id[world.server_id].assigned_worker_id = elsewhere
        return _HYDRATE_FAILED

    world.cp.hydrate_hooks.append(re_placed_unseen_then_fail)

    await _fails(world, kind)

    assert world.stored() == (DesiredState.RUNNING, elsewhere)
    assert world.cp.decremented == []
    assert world.intents.current(world.server_id) == 0


@_REVERTS
async def test_a_newer_start_waits_for_a_revert_in_progress(kind: str) -> None:
    # The revert holds the start-intent lock across its check and its write, as
    # every start does from its count read to its commit. Here F's revert has
    # checked, and is about to write, when a stop releases the row and a newer
    # start begins. The newer start waits for the lock, and F's write then finds
    # the row stopped and does nothing. Had the newer start committed first, F's
    # write would undo it.
    world = _seeded(kind)
    write = asyncio.Event()

    async def fail_and_hold_the_revert_write() -> CommandOutcome:
        world.repo.pause_next_flip = write
        return _HYDRATE_FAILED

    world.cp.hydrate_hooks.append(fail_and_hold_the_revert_write)
    f = asyncio.create_task(_fails(world, kind))
    await world.repo.flip_paused.wait()
    await world.stop()
    newer = asyncio.create_task(world.start())
    await _either(newer, _intent_lock(world).waited)
    write.set()
    await f
    await newer

    assert world.kinds() == ["hydrate", "stop", "snapshot", "hydrate", "start"]
    assert world.stored() == (DesiredState.RUNNING, world.worker)
    assert world.cp.live == {world.server_id}
    assert world.cp.decremented == [world.worker]


@_REVERTS
async def test_a_start_queued_behind_a_revert_reads_the_count_after_it(
    kind: str,
) -> None:
    # F's revert has written, and its commit has landed but not returned, when a
    # newer start finds the row free and asks for the start-intent lock. It gets
    # the lock once the revert is done, and must read the count only then, after
    # the revert's bump. Read before, the count would move under it, and its own
    # check before it sends would hold it back, though the intent is its own.
    world = _seeded(kind)
    lock = _intent_lock(world)
    revert = _CommitLandsThenWaits(servers=world.repo, at=2)
    newer_hydrating = asyncio.Event()
    newer_hydrates = asyncio.Event()

    async def fail() -> CommandOutcome:
        return _HYDRATE_FAILED

    async def newer_hydrate_waits() -> None:
        newer_hydrating.set()
        await newer_hydrates.wait()

    world.cp.hydrate_hooks.append(fail)
    world.cp.hydrate_hooks.append(newer_hydrate_waits)
    f = asyncio.create_task(_fails(world, kind, revert))
    await revert.landed.wait()
    newer = asyncio.create_task(world.start() if kind == "start" else world.place())
    await _either(newer, lock.waited)
    revert.returns.set()
    await _either(newer, newer_hydrating)
    newer_hydrates.set()
    await f
    await newer

    # F's hydrate, then the newer start's hydrate and start.
    assert world.kinds() == ["hydrate", "hydrate", "start"]
    assert world.stored() == (DesiredState.RUNNING, world.worker)
    assert world.cp.live == {world.server_id}
    assert world.cp.decremented == [world.worker]


@_REVERTS
async def test_a_revert_checks_its_intent_once_it_holds_the_lock(kind: str) -> None:
    # The stop and the newer start land while F's revert waits for the
    # start-intent lock. A check made before F held the lock would still have
    # found F's intent.
    world = _seeded(kind)
    lock = _intent_lock(world)

    async def stop_and_restart() -> None:
        await world.stop()
        await world.start()

    async def fail_as_the_revert_asks_for_the_lock() -> CommandOutcome:
        lock.on_next_hold = stop_and_restart
        return _HYDRATE_FAILED

    world.cp.hydrate_hooks.append(fail_as_the_revert_asks_for_the_lock)

    await _fails(world, kind)

    assert world.kinds() == ["hydrate", "stop", "snapshot", "hydrate", "start"]
    assert world.stored() == (DesiredState.RUNNING, world.worker)
    assert world.cp.live == {world.server_id}
    assert world.cp.decremented == [world.worker]


@_REVERTS
async def test_a_revert_of_a_withdrawn_intent_does_not_wait_for_the_lock(
    kind: str,
) -> None:
    # F's revert, whose intent the count already shows withdrawn, has nothing to
    # undo, so it does not wait for the start-intent lock to find that out. Here
    # the lock is held elsewhere when F fails.
    world = _seeded(kind)
    lock = _intent_lock(world)
    holder = _Holder(world)

    async def stop_and_restart_then_fail_while_held() -> CommandOutcome:
        await world.stop()
        await world.start()
        await holder.take()
        return _HYDRATE_FAILED

    world.cp.hydrate_hooks.append(stop_and_restart_then_fail_while_held)
    f = asyncio.create_task(_fails(world, kind))
    await _either(f, lock.waited)
    await holder.release()
    await f

    assert not lock.waited.is_set()
    assert world.stored() == (DesiredState.RUNNING, world.worker)
    assert world.cp.decremented == [world.worker]


@pytest.mark.parametrize(
    ("kind", "reverted"),
    [("start", DesiredState.STOPPED), ("placement", DesiredState.RUNNING)],
    ids=["start", "placement"],
)
async def test_a_revert_does_not_wait_behind_an_unrelated_lifecycle_lock_holder(
    kind: str, reverted: DesiredState
) -> None:
    # Operations that can never create a start intent hold the lifecycle lock
    # across Storage I/O (a backup upload or delete, a prune, the integrity
    # sweep), and that lock gives up busy after a few seconds. F's revert of its
    # own intent does not take it, so the revert still happens, and the caller
    # sees F's own failure.
    world = _seeded(kind)
    lock = world.lock
    assert isinstance(lock, _ExclusiveLock)

    async def fail_while_the_lifecycle_lock_is_busy() -> CommandOutcome:
        lock.busy = True
        return _HYDRATE_FAILED

    world.cp.hydrate_hooks.append(fail_while_the_lifecycle_lock_is_busy)

    await _fails(world, kind)

    assert world.stored() == (reverted, None)
    assert world.cp.decremented == [world.worker]
    assert world.intents.current(world.server_id) == 1


@pytest.mark.parametrize(
    ("kind", "reverted"),
    [("start", DesiredState.STOPPED), ("placement", DesiredState.RUNNING)],
    ids=["start", "placement"],
)
@pytest.mark.parametrize(
    "error",
    [ConnectionError("commit landed, response lost"), asyncio.CancelledError()],
    ids=["lost-response", "cancelled"],
)
async def test_a_revert_whose_commit_landed_but_raised_still_bumps(
    kind: str, reverted: DesiredState, error: BaseException
) -> None:
    # As with a stop or a drain: the revert's commit may have landed though its
    # await raised, so it bumps, and a re-send of the reverted intent that has
    # not been queued yet is held back. It does not decrement: the state is not
    # known.
    world = _seeded(kind)
    use_case = world.start_server(
        _CommitLandsThenRaises(servers=world.repo, error=error, at=2)
    )

    async def fail() -> CommandOutcome:
        return _HYDRATE_FAILED

    world.cp.hydrate_hooks.append(fail)

    with pytest.raises(type(error)):
        if kind == "start":
            await use_case(community_id=world.community, server_id=world.server_id)
        else:
            await use_case.place_and_start(
                community_id=world.community, server_id=world.server_id
            )

    assert world.stored() == (reverted, None)
    assert world.intents.current(world.server_id) == 1
    assert world.cp.decremented == []


@_REVERTS
async def test_a_re_sent_start_sees_its_intent_undone_and_placed_again(
    kind: str,
) -> None:
    # A revert sends no stop. Its bump is what tells a start re-sending the same
    # intent that the intent is gone. Here F's revert lands while a re-send of F's
    # intent hydrates, and a newer start or placement puts the server back on the
    # same Worker, so the re-send's re-read finds desired=running there again.
    world = _seeded(kind)
    f_hydrating = asyncio.Event()
    f_fails = asyncio.Event()

    async def f_hydrate_fails_later() -> CommandOutcome:
        f_hydrating.set()
        await f_fails.wait()
        return _HYDRATE_FAILED

    world.cp.hydrate_hooks.append(f_hydrate_fails_later)
    f = asyncio.create_task(_fails(world, kind))
    await f_hydrating.wait()

    async def f_reverts_then_newer() -> None:
        f_fails.set()
        await f
        if kind == "start":
            await world.start()
        else:
            await world.place()

    world.after_hydrate(f_reverts_then_newer)

    with pytest.raises(LifecycleTransitionConflictError):
        await world.start_server().redispatch_start(
            community_id=world.community, server_id=world.server_id
        )

    # F's hydrate, the re-send's, then the newer one's hydrate and start.
    assert world.kinds() == ["hydrate", "hydrate", "hydrate", "start"]
    assert world.stored() == (DesiredState.RUNNING, world.worker)
    assert world.cp.live == {world.server_id}


async def test_a_start_whose_re_read_fails_leaves_a_newer_start_alone() -> None:
    # A re-read that raises is a failure before the send, so the start reverts,
    # as after a failed hydrate, and only its own intent. Here a stop and a newer
    # start came first.
    world = _seeded("start")

    async def database_error() -> None:
        raise RuntimeError("database unavailable")

    async def stop_and_restart_then_fail_the_re_read() -> None:
        await world.stop()
        await world.start()
        world.repo.after_next_read = database_error

    world.after_hydrate(stop_and_restart_then_fail_the_re_read)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await world.start()

    assert world.kinds() == ["hydrate", "stop", "snapshot", "hydrate", "start"]
    assert world.stored() == (DesiredState.RUNNING, world.worker)
    assert world.cp.live == {world.server_id}
    assert world.cp.decremented == [world.worker]


async def test_a_start_that_lost_its_intent_answers_the_conflict_untouched() -> None:
    # A start whose re-read finds the intent gone answers the conflict and leaves
    # the row to whatever moved it: it takes no lock to try a revert. Here the row
    # moved where the count cannot see, and the start-intent lock is held
    # elsewhere, so a revert attempt would wait for it.
    world = _seeded("start")
    elsewhere = WorkerId(uuid.uuid4())
    lock = _intent_lock(world)
    holder = _Holder(world)

    async def re_placed_unseen_while_held() -> None:
        world.repo.by_id[world.server_id].assigned_worker_id = elsewhere
        await holder.take()

    world.after_hydrate(re_placed_unseen_while_held)
    start = asyncio.create_task(world.start())
    await _either(start, lock.waited)
    await holder.release()

    with pytest.raises(LifecycleTransitionConflictError):
        await start

    assert not lock.waited.is_set()
    assert world.kinds() == ["hydrate"]
    assert world.stored() == (DesiredState.RUNNING, elsewhere)
    assert world.cp.decremented == []


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
    app.state.start_intent_lock = StartIntentLock()
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
    assert start.start_intent_lock is app.state.start_intent_lock
    assert stop.stop_intents is app.state.stop_intents
    assert isinstance(drain.stopper, ServersAssignedServerStopper)
    assert drain.stopper.stop_servers.stop_intents is app.state.stop_intents
