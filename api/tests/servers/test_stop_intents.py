"""A stop that withdraws a start's intent after the start's re-read (issue #3739).

The start re-reads the row just before it sends (issue #3737). Two things still
got past that re-read: a stop that commits after it and reaches the Worker
first, and a stop followed by a newer start that put the server back on the same
Worker. Either way the Worker answered the stop ``SERVER_NOT_FOUND``, the stop
released the row, and the start then launched an instance nobody owns.

The same count tells a failed start's revert whether the intent on the row is
still its own (issue #3741). Before, the revert undid whatever intent it found,
including a newer start's that had already been sent.

A lifecycle write compares the assignment it read as well as the desired state
(issue #3792). Before, a stop, a drain or a restart that read the row before a
failed placement released it wrote the released Worker back, and a write that
does not own the config wrote back the config it read over a later edit.

A stop convergence owed for a stopped intent released a row on its cached
observed state (issue #3766): under a start the Worker had not reported on yet,
and before the write that stopped the row had bumped the count.

A stop refused a start intent no Worker held yet (issue #3815), so an operator
could not withdraw one whose placement kept failing.

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
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.requests import Request

from mc_server_dashboard_api.dependencies import (
    get_set_worker_drain,
    get_snapshot_uploads,
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
from mc_server_dashboard_api.servers.adapters.store_generation import (
    StorageGenerationReader,
)
from mc_server_dashboard_api.servers.application.lifecycle import (
    RestartServer,
    StartServer,
    StopServer,
    StopServersAssignedToWorker,
)
from mc_server_dashboard_api.servers.application.snapshot_scheduler import (
    RunSnapshotCadenceTick,
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
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    CommandDispatchError,
    InvalidLifecycleTransitionError,
    LifecycleTransitionConflictError,
    ServerBusyError,
)
from mc_server_dashboard_api.servers.domain.lifecycle_lock import (
    HeldLifecycleLock,
    LifecycleLock,
    NullHeldLifecycleLock,
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
    ``before_next_clear`` runs once, inside the next ``clear_awaiting_hydrate``,
    before its write: a write it makes lands between a start's compare and its
    clear.
    ``pause_next_flip`` holds the next ``update_lifecycle`` at its entry, so a stop
    that has read the row waits there, just before its write. While
    ``stage_flips`` is set, an ``update_lifecycle`` that matches is held back
    instead of written, until ``land_staged``: a commit that has not landed yet.
    """

    def __init__(self) -> None:
        super().__init__()
        self.after_next_read: Hook | None = None
        self.before_next_clear: Hook | None = None
        self.pause_next_flip: asyncio.Event | None = None
        self.flip_paused = asyncio.Event()
        self.stage_flips = False
        self._staged: list[tuple[Server, DesiredState, WorkerId | None, bool]] = []

    async def clear_awaiting_hydrate(
        self, server_id: ServerId, worker_id: WorkerId, *, updated_at: dt.datetime
    ) -> bool:
        hook, self.before_next_clear = self.before_next_clear, None
        if hook is not None:
            await hook()
        return await super().clear_awaiting_hydrate(
            server_id, worker_id, updated_at=updated_at
        )

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
        expected_assigned_worker_id: WorkerId | None,
        write_config: bool = False,
    ) -> bool:
        gate, self.pause_next_flip = self.pause_next_flip, None
        if gate is not None:
            self.flip_paused.set()
            await gate.wait()
        if self.stage_flips:
            if not self.lifecycle_matches(
                server.id,
                expected_from=expected_from,
                expected_assigned_worker_id=expected_assigned_worker_id,
            ):
                return False
            self._staged.append(
                (server, expected_from, expected_assigned_worker_id, write_config)
            )
            return True
        return await super().update_lifecycle(
            server,
            expected_from=expected_from,
            expected_assigned_worker_id=expected_assigned_worker_id,
            write_config=write_config,
        )

    async def land_staged(self) -> None:
        self.stage_flips = False
        staged, self._staged = self._staged, []
        for server, expected_from, expected_worker, write_config in staged:
            assert await super().update_lifecycle(
                server,
                expected_from=expected_from,
                expected_assigned_worker_id=expected_worker,
                write_config=write_config,
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
    task cancelled at the same await raises ``CancelledError``. A client
    disconnect does not cancel it on the shipped server stack unless the handler
    reads the request body (issue #3950).
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
    delete, a prune, the integrity sweep. ``waited`` is set once a ``hold`` finds
    the lock taken.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.busy = False
        self.waited = asyncio.Event()

    @contextlib.asynccontextmanager
    async def hold(self, server_id: ServerId) -> AsyncIterator[HeldLifecycleLock]:
        if self.busy:
            raise ServerBusyError(str(server_id.value))
        if self._lock.locked():
            self.waited.set()
        async with self._lock:
            yield NullHeldLifecycleLock()


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
        if self.is_held(server_id):
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
    answer is replaced launches nothing. Each stop takes the next entry of
    ``stop_refusals``, if any, as its answer, and leaves any instance running.
    ``stop_sent`` is set once a stop has run on the Worker; while
    ``hold_stop_answer`` is set, its answer waits for it. Each snapshot runs the
    next entry of ``snapshot_hooks``, if any, before it answers; one that raises
    fails the snapshot. ``load`` is the placement load's confirms and drops, in
    the order made.
    """

    def __init__(self, *, place_to: WorkerId | None = None) -> None:
        super().__init__(place_to=place_to)
        self.load: list[str] = []
        self.live: set[ServerId] = set()
        self.hydrate_hooks: list[Callable[[], Awaitable[CommandOutcome | None]]] = []
        self.start_hooks: list[Callable[[], Awaitable[CommandOutcome | None]]] = []
        self.stop_refusals: list[CommandOutcome] = []
        self.snapshot_hooks: list[Hook] = []
        self.stop_sent = asyncio.Event()
        self.hold_stop_answer: asyncio.Event | None = None

    def increment_assignment(self, *, worker_id: WorkerId, server_id: ServerId) -> None:
        super().increment_assignment(worker_id=worker_id, server_id=server_id)
        self.load.append("confirm")

    def decrement_assignment(self, *, worker_id: WorkerId, server_id: ServerId) -> None:
        super().decrement_assignment(worker_id=worker_id, server_id=server_id)
        self.load.append("drop")

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
        if self.stop_refusals:
            outcome = self.stop_refusals.pop(0)
        elif server_id in self.live:
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
        if self.snapshot_hooks:
            await self.snapshot_hooks.pop(0)()
        return outcome


class _World:
    """One server, one Worker, and the use cases that race over them."""

    def __init__(
        self,
        *,
        desired: DesiredState = DesiredState.STOPPED,
        assigned: bool = False,
        observed: ObservedState | None = None,
    ) -> None:
        self.community = CommunityId(uuid.uuid4())
        self.server_id = ServerId(uuid.uuid4())
        self.worker = WorkerId(uuid.uuid4())
        self.repo = _Repository()
        self.repo.seed(
            self.row(
                self.server_id, desired=desired, assigned=assigned, observed=observed
            )
        )
        self.cp = _Worker(place_to=self.worker)
        self.intents = StopIntentCounter()
        # The starts' lifecycle lock and start-intent lock. No exclusion by
        # default: some tests run a whole start inside another start's commit,
        # which the real locks forbid. ``None`` gives each use case its own.
        self.lock: LifecycleLock = NullLifecycleLock()
        self.intent_lock: StartIntentLock | None = None
        # The store generation the starts read; a test moves it as an at-rest edit
        # or a restore would.
        self.store_generation = 0

    def row(
        self,
        server_id: ServerId,
        *,
        desired: DesiredState,
        assigned: bool,
        observed: ObservedState | None = None,
    ) -> Server:
        if observed is None:
            observed = (
                ObservedState.UNKNOWN
                if desired is DesiredState.RUNNING
                else ObservedState.STOPPED
            )
        return Server(
            id=server_id,
            community_id=self.community,
            name=ServerName("survival"),
            mc_edition="java",
            mc_version="1.21.1",
            server_type=ServerType.VANILLA,
            config={},
            desired_state=desired,
            observed_state=observed,
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
            store_generation=FakeStoreGenerationReader(
                generation=self.store_generation
            ),
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

    async def restart(self) -> Server:
        return await RestartServer(
            uow=FakeUnitOfWork(servers=self.repo),
            control_plane=self.cp,
            clock=FakeClock(_NOW),
        )(community_id=self.community, server_id=self.server_id)

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
    # unassigned). The start sees the bump instead and sends nothing. Nor does it
    # clear the row's mark, since the row is no longer its own, so the release
    # takes no snapshot (issue #3890).
    world = _World()

    async def stop() -> None:
        await world.stop()

    world.after_hydrate_and_next_read(stop)

    with pytest.raises(LifecycleTransitionConflictError):
        await world.start()

    assert world.kinds() == ["hydrate", "stop"]
    assert world.stored() == (DesiredState.STOPPED, None)
    assert world.cp.live == set()


async def test_a_re_sent_start_sees_a_stop_that_was_waiting_on_its_write() -> None:
    # A re-sent start reads an intent another start committed, so a stop of that
    # intent can already be past its own read when the re-send begins. Here the
    # stop waits just before its write, the re-send reads the row as running, and
    # the stop then commits and is sent while the re-send re-reads. A bump made
    # before the write would precede the re-send's read of the counter, and the
    # start would follow the stop out. The bump follows the commit, so it is seen.
    # The re-send marked the row before its hydrate, which landed, so the mark
    # stays: the re-send does not clear the mark of a row no longer its own, and
    # the release takes no snapshot, as after a user start (issue #4024).
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

    assert world.kinds() == ["hydrate", "stop"]
    assert world.stored() == (DesiredState.STOPPED, None)
    assert world.cp.live == set()


class _StopDuringTheWait(FakeStoreGenerationReader):
    """A store whose wait for the Worker's uploads runs ``during`` first."""

    def __init__(self, during: Hook) -> None:
        super().__init__()
        self._during = during

    async def uploads_settled(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        worker_id: WorkerId,
        timeout: float,
    ) -> bool:
        await self._during()
        return True


async def test_a_stop_during_a_re_sent_starts_wait_takes_its_final_snapshot() -> None:
    # Issue #4024. The re-sent start marked the row before its wait for the
    # Worker's uploads, and an operator's stop commits during the wait and is
    # sent. The start then answers the conflict without sending its hydrate, so
    # the Worker's working set is as it was, and the start puts the mark back.
    # The stop, whose answer comes after that, takes the final snapshot it would
    # have taken without the start. Had its answer come first, it would have
    # found the mark and taken none.
    world = _World(desired=DesiredState.RUNNING, assigned=True)
    world.cp.hold_stop_answer = asyncio.Event()
    stops: list[asyncio.Task[Server]] = []

    async def stop_is_sent() -> None:
        stops.append(asyncio.create_task(world.stop()))
        await world.cp.stop_sent.wait()

    start = replace(
        world.start_server(), store_generation=_StopDuringTheWait(stop_is_sent)
    )

    with pytest.raises(LifecycleTransitionConflictError):
        await start.redispatch_start(
            community_id=world.community, server_id=world.server_id
        )
    world.cp.hold_stop_answer.set()
    await stops[0]

    assert world.kinds() == ["stop", "snapshot"]
    assert world.stored() == (DesiredState.STOPPED, None)


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
    # the answer would let the start follow it out. The start clears no mark, so
    # the release takes no snapshot (issue #3890).
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

    assert world.kinds() == ["hydrate", "stop"]
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
    # and clears the assignment, and sends no stop. The refused start cleared no
    # mark, so that retry takes no snapshot (issue #3890).
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

    assert world.kinds() == ["hydrate"]
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

    # The refused start, the stop and its release, then the newer start. The
    # replay's re-read after its wait for the Worker's uploads (issue #3950)
    # finds the count moved, so it sends neither a hydrate nor a start.
    assert world.kinds() == ["start", "stop", "snapshot", "start"]
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
    # The release takes no snapshot: the first start had not recorded its hydrate
    # yet, so the row still read awaiting_hydrate (issue #3890).
    assert world.kinds() == ["hydrate", "stop", "hydrate", "start"]
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
    # start. The release takes no snapshot (issue #3890): nothing had hydrated, so
    # the Worker's scratch is whatever it held before, which the row's mark cannot
    # tell from a stale leftover. The first start then skips its hydrate (the newer
    # one's hydrate left the Worker holding the store's generation) and sends
    # nothing.
    assert world.kinds() == ["stop", "hydrate", "start"]
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

    # The release takes no snapshot, as above. The placement would hydrate, but
    # its re-read after the wait for the Worker's uploads (issue #3950) finds the
    # count moved, so it sends nothing.
    assert world.kinds() == ["stop", "hydrate", "start"]
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


def _lifecycle_lock(world: _World) -> _ExclusiveLock:
    assert isinstance(world.lock, _ExclusiveLock)
    return world.lock


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


async def _either(task: asyncio.Task[Any], *events: asyncio.Event) -> None:
    """Wait for one of ``events``, or for ``task`` to end first.

    A test waits for the step it expects next, and also for the steps that a
    broken ordering takes instead, so that it fails rather than hangs.
    """

    waiters = {asyncio.create_task(event.wait()) for event in events}
    await asyncio.wait({task, *waiters}, return_when=asyncio.FIRST_COMPLETED)
    for waiter in waiters:
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

    assert world.kinds() == ["hydrate", "stop", "hydrate", "start"]
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

    assert world.kinds() == ["hydrate", "stop", "hydrate", "start"]
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

    assert world.kinds() == ["hydrate", "stop"]
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
    await _either(newer, _intent_lock(world).waited, _lifecycle_lock(world).waited)
    write.set()
    await f
    await newer

    assert world.kinds() == ["hydrate", "stop", "hydrate", "start"]
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
    # The revert's drop of F's load entry must come before the newer start
    # confirms its own, too: the registry keys the entry by Worker and server, so
    # a drop after the confirm would remove the newer start's.
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
    await _either(newer, lock.waited, _lifecycle_lock(world).waited, newer_hydrating)
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
    # F's confirm, F's revert dropping it, then the newer start's confirm.
    assert world.cp.load == ["confirm", "drop", "confirm"]


@pytest.mark.parametrize(
    ("kind", "untouched"),
    [("start", DesiredState.STOPPED), ("placement", DesiredState.RUNNING)],
    ids=["start", "placement"],
)
async def test_a_start_cancelled_while_it_waits_for_the_lock_releases_its_slot(
    kind: str, untouched: DesiredState
) -> None:
    # A start reserves its slot on the Worker when it places the server, and asks
    # for the start-intent lock only then. A start cancelled during that wait (a
    # client disconnect) must release the reservation, or the slot leaks for
    # good: an intent that never committed is in no tally a reconnect rebuilds
    # from (issue #778). The lock must not stay held by the cancelled start.
    world = _seeded(kind)
    lock = _intent_lock(world)
    holder = _Holder(world)
    await holder.take()
    start = asyncio.create_task(world.start() if kind == "start" else world.place())
    await _either(start, lock.waited)
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    await holder.release()

    assert world.cp.reserved == [(world.worker, world.server_id)]
    assert world.cp.released == [(world.worker, world.server_id)]
    assert world.cp.incremented == []
    assert world.stored() == (untouched, None)
    assert world.kinds() == []
    assert not lock.is_held(world.server_id)

    # A later start still gets the lock, and goes out.
    await (world.start() if kind == "start" else world.place())

    assert world.cp.live == {world.server_id}


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

    assert world.kinds() == ["hydrate", "stop", "hydrate", "start"]
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
    lock = _lifecycle_lock(world)

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


@pytest.mark.parametrize(
    ("kind", "reverted"),
    [("start", DesiredState.STOPPED), ("placement", DesiredState.RUNNING)],
    ids=["start", "placement"],
)
@pytest.mark.parametrize(
    "error",
    [ConnectionError("close failed"), asyncio.CancelledError()],
    ids=["close-failed", "cancelled"],
)
async def test_a_revert_whose_commit_returned_drops_the_load_though_the_exit_fails(
    kind: str,
    reverted: DesiredState,
    error: BaseException,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The revert's commit returned, so the row is released, and only the unit of
    # work's exit (its rollback and close) then raises or is cancelled. The drop
    # of the start's load entry stands: it runs before that exit, as the start's
    # own confirm does (#840). Made after the exit, it would be skipped, and the
    # entry would stay counted on the Worker for a released row until a reconnect
    # rebuild.
    world = _seeded(kind)
    uow = _ExitRunsHook(servers=world.repo)
    use_case = world.start_server(uow)

    async def close_fails() -> None:
        raise error

    async def fail() -> CommandOutcome:
        # The start's own block has closed, so this fires as the revert's closes.
        uow.on_next_exit = close_fails
        return _HYDRATE_FAILED

    world.cp.hydrate_hooks.append(fail)

    with caplog.at_level(logging.ERROR), pytest.raises(type(error)):
        if kind == "start":
            await use_case(community_id=world.community, server_id=world.server_id)
        else:
            await use_case.place_and_start(
                community_id=world.community, server_id=world.server_id
            )

    assert world.stored() == (reverted, None)
    assert world.intents.current(world.server_id) == 1
    assert not _intent_lock(world).is_held(world.server_id)
    assert world.cp.decremented == [world.worker]
    # The log says the row was released, not that it is left as the start set it.
    # A cancellation is not logged.
    logged = [
        record.getMessage()
        for record in caplog.records
        if record.name == "mc_server_dashboard_api.servers.application.lifecycle"
        and record.levelno == logging.ERROR
    ]
    if isinstance(error, asyncio.CancelledError):
        assert logged == []
    else:
        [message] = logged
        assert "but closing its unit of work failed" in message
        assert "left" not in message


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

    assert world.kinds() == ["hydrate", "stop", "hydrate", "start"]
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


# Issue #3792: a stop, a drain or a restart reads the row as a placement left
# it, (running, W), and waits just before its write. The placement's hydrate then
# fails and its ``_unassign`` releases W; with ``re-placed``, a newer placement
# then puts the server on another Worker and starts it there. The write compares
# the assignment it read, so it matches nothing, and the row, the placement load
# and the count stay as the later writes left them.
_LATER_WRITES = pytest.mark.parametrize("then", ["unassigned", "re-placed"])


async def _write_straddles_a_failed_placement(
    world: _World,
    write: Callable[[], Coroutine[Any, Any, Any]],
    *,
    then: str,
    other: _Worker,
) -> asyncio.Task[Any]:
    """Run ``write`` up to its compare-and-set, then fail a placement under it.

    Returns the write's task, released from its wait but not yet awaited.
    """

    gate = asyncio.Event()
    task: asyncio.Task[Any] | None = None

    async def write_waits_then_fail() -> CommandOutcome:
        nonlocal task
        world.repo.pause_next_flip = gate
        task = asyncio.create_task(write())
        await _either(task, world.repo.flip_paused)
        return _HYDRATE_FAILED

    world.cp.hydrate_hooks.append(write_waits_then_fail)
    await _fails(world, "placement")
    if then == "re-placed":
        await world.start_server(control_plane=other).place_and_start(
            community_id=world.community, server_id=world.server_id
        )
    gate.set()
    assert task is not None
    return task


def _owner_after(then: str, elsewhere: WorkerId) -> WorkerId | None:
    return None if then == "unassigned" else elsewhere


@_LATER_WRITES
async def test_a_stop_that_read_the_row_before_a_placement_undo_conflicts(
    then: str,
) -> None:
    # On main the stop wrote (stopped, W) over the released row, decremented W a
    # second time and sent W a stop; with ``re-placed`` it also took the row off
    # the Worker the server now runs on.
    world = _seeded("placement")
    elsewhere = WorkerId(uuid.uuid4())
    other = _Worker(place_to=elsewhere)
    stop = await _write_straddles_a_failed_placement(
        world, world.stop, then=then, other=other
    )

    with pytest.raises(LifecycleTransitionConflictError):
        await stop

    assert world.stored() == (DesiredState.RUNNING, _owner_after(then, elsewhere))
    assert world.kinds() == ["hydrate"]
    assert world.cp.decremented == [world.worker]
    assert other.decremented == []
    assert world.intents.current(world.server_id) == 1


@_LATER_WRITES
async def test_a_drain_that_read_the_row_before_a_placement_undo_skips_it(
    then: str,
) -> None:
    # The drain reports only the flips it made, and its caller sheds the load of
    # those alone, so an empty report is no second decrement of W.
    world = _seeded("placement")
    elsewhere = WorkerId(uuid.uuid4())
    other = _Worker(place_to=elsewhere)
    drain = await _write_straddles_a_failed_placement(
        world, world.drain, then=then, other=other
    )

    assert await drain == []

    assert world.stored() == (DesiredState.RUNNING, _owner_after(then, elsewhere))
    assert world.cp.decremented == [world.worker]
    assert world.intents.current(world.server_id) == 1


@_LATER_WRITES
async def test_a_restart_that_read_the_row_before_a_placement_undo_conflicts(
    then: str,
) -> None:
    # On main the restart wrote W back onto the released row, with no load entry
    # and no bump, and restarted a server W does not run.
    world = _seeded("placement")
    elsewhere = WorkerId(uuid.uuid4())
    other = _Worker(place_to=elsewhere)
    restart = await _write_straddles_a_failed_placement(
        world, world.restart, then=then, other=other
    )

    with pytest.raises(LifecycleTransitionConflictError):
        await restart

    assert world.stored() == (DesiredState.RUNNING, _owner_after(then, elsewhere))
    assert world.kinds() == ["hydrate"]
    assert [k for k, _, _ in other.dispatched] == (
        [] if then == "unassigned" else ["hydrate", "start"]
    )


@_REVERTS
async def test_a_revert_leaves_a_row_moved_after_its_check_alone(kind: str) -> None:
    # One step later than ``..._re_placed_out_of_its_sight_alone``: the row moves
    # to another Worker after the revert has read and checked it, before its
    # write. The write compares the Worker it checked, so it matches nothing.
    world = _seeded(kind)
    elsewhere = WorkerId(uuid.uuid4())

    async def re_placed_unseen() -> None:
        world.repo.by_id[world.server_id].assigned_worker_id = elsewhere

    async def fail_then_move_once_the_revert_reads() -> CommandOutcome:
        world.repo.after_next_read = re_placed_unseen
        return _HYDRATE_FAILED

    world.cp.hydrate_hooks.append(fail_then_move_once_the_revert_reads)

    await _fails(world, kind)

    assert world.stored() == (DesiredState.RUNNING, elsewhere)
    assert world.cp.decremented == []
    assert world.intents.current(world.server_id) == 0


# A config edit of safe keys may commit while a server runs (UpdateServer, under
# the lifecycle lock, which none of these writers take), so it can land between
# their read and their write. Only a start owns the config: it records the JAR it
# resolved. Every other lifecycle write leaves the config as the row holds it.
_EDIT = {"motd": "edited while running"}


async def _edit_config(world: _World) -> None:
    row = world.repo.by_id[world.server_id]
    await world.repo.update(replace(row, config={**row.config, **_EDIT}))


def _kept_the_edit(world: _World) -> bool:
    return _EDIT.items() <= world.repo.by_id[world.server_id].config.items()


@pytest.mark.parametrize("writer", ["stop", "restart"])
async def test_a_stop_or_restart_keeps_a_config_edit_made_after_its_read(
    writer: str,
) -> None:
    world = _World(desired=DesiredState.RUNNING, assigned=True)

    async def edit() -> None:
        await _edit_config(world)

    world.repo.after_next_read = edit

    await (world.stop() if writer == "stop" else world.restart())

    assert _kept_the_edit(world)


async def test_a_drain_keeps_a_config_edit_made_after_its_read() -> None:
    world = _World(desired=DesiredState.RUNNING, assigned=True)
    gate = asyncio.Event()
    world.repo.pause_next_flip = gate
    drain = asyncio.create_task(world.drain())
    await _either(drain, world.repo.flip_paused)
    await _edit_config(world)
    gate.set()

    assert await drain == [world.server_id]

    assert _kept_the_edit(world)


@_REVERTS
async def test_a_revert_keeps_a_config_edit_made_after_its_read(kind: str) -> None:
    world = _seeded(kind)

    async def edit() -> None:
        await _edit_config(world)

    async def fail_then_edit_once_the_revert_reads() -> CommandOutcome:
        world.repo.after_next_read = edit
        return _HYDRATE_FAILED

    world.cp.hydrate_hooks.append(fail_then_edit_once_the_revert_reads)

    await _fails(world, kind)

    assert world.stored()[1] is None
    assert _kept_the_edit(world)


# Issue #3766: a stop convergence owed for a row whose intent is already stopped,
# run at once by an operator stop (issue #3491). For a row that reads
# observed=stopped or crashed, that convergence used to take the final snapshot and
# release the row, trusting the cached state. It did so under a start the Worker
# had not reported on yet, and before the write that stopped the row had moved the
# count.


def _launches_then(world: _World, *writes: Hook) -> None:
    """The next start launches its instance, then ``writes`` run before it answers.

    The instance is up, but the Worker has reported nothing yet, so the row's
    observed state is still the one the server had before the start.
    """

    async def launch() -> CommandOutcome:
        world.cp.live.add(world.server_id)
        for write in writes:
            await write()
        return CommandOutcome(status=CommandStatus.OK)

    world.cp.start_hooks.append(launch)


async def _withdraw_raising(
    world: _World, withdrawal: str, error: BaseException
) -> None:
    """Withdraw the intent with a drain or a stop whose commit raises ``error``.

    Neither sends a stop: a drain never does, and a stop whose commit raises
    does not get as far.
    """

    uow = _CommitLandsThenRaises(servers=world.repo, error=error)
    with pytest.raises(type(error)):
        if withdrawal == "drain":
            await world.drain(uow)
        else:
            await StopServer(
                uow=uow,
                control_plane=world.cp,
                clock=FakeClock(_NOW),
                stop_intents=world.intents,
            )(community_id=world.community, server_id=world.server_id)


@pytest.mark.parametrize("withdrawal", ["drain", "stop-lost-response"])
async def test_an_operator_stop_after_a_silent_withdrawal_stops_the_launching_start(
    withdrawal: str,
) -> None:
    # The issue's sequence. The start is out, and a drain, or a stop whose commit
    # lands but raises, withdraws its intent after it compared. Neither sends a
    # stop. The operator then stops the (stopped, stopped, W) row. Its convergence
    # used to release the row under the launching instance. It now sends a stop,
    # which the Worker runs behind the start, as a real stop, forced if the
    # operator asked for that.
    world = _World()

    async def withdraw() -> None:
        if withdrawal == "drain":
            assert await world.drain() == [world.server_id]
        else:
            await _withdraw_raising(world, "stop", ConnectionError("response lost"))

    async def operator_stop() -> None:
        await world.stop_server()(
            community_id=world.community, server_id=world.server_id, force=True
        )

    _launches_then(world, withdraw, operator_stop)

    await world.start()

    assert world.cp.live == set()
    assert world.kinds() == ["hydrate", "start", "stop", "snapshot"]
    assert world.stored() == (DesiredState.STOPPED, None)
    assert world.cp.stop_force is True


@pytest.mark.parametrize("refused", ["withdrawing-stop", "owed-stop"])
async def test_a_refused_stop_does_not_count_as_stopping_the_start(
    refused: str,
) -> None:
    # A stop sent behind the start, either the one that withdrew the intent or
    # the one an operator stop owed after a drain, is refused by the Worker: it
    # waited behind the start past its expiry. The start is still launching, so
    # the operator's retry must send a stop again, not release.
    world = _World()
    world.cp.stop_refusals.append(
        CommandOutcome(status=CommandStatus.EXPIRED, message="expired in the lane")
    )

    async def stop_refused() -> None:
        if refused == "owed-stop":
            assert await world.drain() == [world.server_id]
        with pytest.raises(CommandDispatchError):
            await world.stop()

    async def operator_retries() -> None:
        await world.stop()

    _launches_then(world, stop_refused, operator_retries)

    await world.start()

    assert world.cp.live == set()
    assert world.kinds() == ["hydrate", "start", "stop", "stop", "snapshot"]
    assert world.stored() == (DesiredState.STOPPED, None)


async def test_a_release_leaves_a_row_the_worker_reported_on_since_its_read() -> None:
    # What no in-process record sees: a start sent before an API restart, say,
    # whose instance reports starting while the convergence takes its final
    # snapshot. The release compares the row's observed_at with the one it read,
    # so it leaves the row assigned, and the reconciler acts on the report.
    world = _World(assigned=True)
    reported = _NOW + dt.timedelta(seconds=1)

    async def report_starting() -> None:
        assert await world.repo.record_observed_state(
            world.server_id,
            ObservedState.STARTING,
            reported,
            expected_worker=world.worker,
        )

    world.cp.snapshot_hooks.append(report_starting)

    await world.stop()

    assert world.kinds() == ["snapshot"]
    assert world.stored() == (DesiredState.STOPPED, world.worker)


async def _withdrawal_not_yet_visible(world: _World, withdrawal: str) -> Hook:
    """Cancel a write's commit before its flip lands; return what lands it.

    The commit raises, cancelled, while the flip is still unreadable to other
    sessions, so the write's bump comes first (comment 1 on issue #3766).
    """

    world.repo.stage_flips = True
    await _withdraw_raising(world, withdrawal, asyncio.CancelledError())

    async def lands() -> None:
        await world.repo.land_staged()

    return lands


@pytest.mark.parametrize("withdrawal", ["drain", "stop"])
async def test_a_re_sent_start_out_before_its_withdrawal_was_visible_is_stopped(
    withdrawal: str,
) -> None:
    # Comment 1 on #3766: the cancelled commit's bump came before its flip was
    # readable. A re-send that read the count in that gap, and the row before the
    # flip landed, is not held back, and goes out under the stopped row. The
    # operator stop owed for that row sends a stop behind it.
    world = _World(
        desired=DesiredState.RUNNING, assigned=True, observed=ObservedState.STOPPED
    )
    lands = await _withdrawal_not_yet_visible(world, withdrawal)
    world.after_hydrate_and_next_read(lands)

    await world.start_server().redispatch_start(
        community_id=world.community, server_id=world.server_id
    )
    assert world.kinds() == ["hydrate", "start"]
    assert world.stored() == (DesiredState.STOPPED, world.worker)

    await world.stop()

    assert world.cp.live == set()
    assert world.kinds() == ["hydrate", "start", "stop", "snapshot"]
    assert world.stored() == (DesiredState.STOPPED, None)


@pytest.mark.parametrize(
    "observed",
    [ObservedState.STOPPED, ObservedState.UNKNOWN],
    ids=["clear", "redispatch-stop"],
)
async def test_a_re_sent_start_that_compares_during_the_owed_stop_is_not_sent(
    observed: ObservedState,
) -> None:
    # The same re-send, comparing only once the operator stop's convergence is
    # under way: paused in its final snapshot, before it releases (a row read
    # stopped), or with its stop on the Worker (a row read unknown). The
    # convergence withdraws the intent itself before either, so the re-send's
    # count has moved and it sends nothing. Its stop, if any, ran first and found
    # nothing, and the row is released with nothing under it.
    world = _World(desired=DesiredState.RUNNING, assigned=True, observed=observed)
    uow = _ExitRunsHook(servers=world.repo)
    lands = await _withdrawal_not_yet_visible(world, "drain")
    paused = asyncio.Event()
    resume = asyncio.Event()
    stops: list[asyncio.Task[Server]] = []

    async def pause() -> None:
        paused.set()
        await resume.wait()

    if observed is ObservedState.STOPPED:
        world.cp.snapshot_hooks.append(pause)
    else:
        world.cp.hold_stop_answer = resume
        world.cp.stop_sent = paused

    async def operator_stop_under_way() -> None:
        stops.append(asyncio.create_task(world.stop()))
        await _either(stops[0], paused)

    async def arm() -> None:
        world.repo.after_next_read = lands
        uow.on_next_exit = operator_stop_under_way

    world.after_hydrate(arm)

    with pytest.raises(LifecycleTransitionConflictError):
        await world.start_server(uow).redispatch_start(
            community_id=world.community, server_id=world.server_id
        )
    resume.set()
    await stops[0]

    assert "start" not in world.kinds()
    assert world.stored() == (DesiredState.STOPPED, None)
    assert world.cp.live == set()


@pytest.mark.parametrize(
    "observed",
    [ObservedState.STOPPED, ObservedState.UNKNOWN],
    ids=["clear", "redispatch-stop"],
)
async def test_a_drain_still_committing_cannot_let_a_failed_start_undo_a_newer_one(
    observed: ObservedState,
) -> None:
    # Comment 2 on #3766, #3791's interaction. Start A hydrates. A drain's flip has
    # landed, but its commit has not returned, so it has not bumped. An operator
    # stop of the drained row runs its convergence and releases the row. A newer
    # start B then read the count, which nothing had moved since A read it, and
    # placed the server on the same Worker. A's hydrate then failed, and A's revert
    # found the count and the row as A had left them, and undid B's intent under
    # B's instance. The convergence now withdraws the intent itself before it
    # releases, so B reads a newer count and A's revert leaves B alone.
    world = _seeded("start")
    world.repo.by_id[world.server_id].observed_state = observed
    drain_uow = _CommitLandsThenWaits(servers=world.repo, at=1)
    drains: list[asyncio.Task[list[ServerId]]] = []

    async def drain_lands_then_stop_and_restart() -> CommandOutcome:
        drains.append(asyncio.create_task(world.drain(drain_uow)))
        await _either(drains[0], drain_uow.landed)
        await world.stop()
        await world.start()
        return _HYDRATE_FAILED

    world.cp.hydrate_hooks.append(drain_lands_then_stop_and_restart)

    await _fails(world, "start")
    drain_uow.returns.set()

    assert await drains[0] == [world.server_id]
    assert world.stored() == (DesiredState.RUNNING, world.worker)
    assert world.cp.live == {world.server_id}
    assert world.cp.decremented == []


@pytest.mark.parametrize("ending", ["refused", "crashed"])
async def test_an_owed_stop_after_a_start_that_left_nothing_running_releases(
    ending: str,
) -> None:
    # The stop sent behind a start finds nothing when the Worker refused the start,
    # or its instance has crashed since. The Worker answers SERVER_NOT_FOUND, and
    # the row is released as before, a crash still reading crashed.
    world = _World()

    async def drain() -> None:
        assert await world.drain() == [world.server_id]

    if ending == "refused":

        async def drain_then_refuse() -> CommandOutcome:
            await drain()
            return CommandOutcome(status=CommandStatus.PORT_CONFLICT, message="taken")

        world.cp.start_hooks.append(drain_then_refuse)
        with pytest.raises(CommandDispatchError):
            await world.start()
    else:
        _launches_then(world, drain)
        await world.start()
        world.cp.live.discard(world.server_id)
        assert await world.repo.record_observed_state(
            world.server_id,
            ObservedState.CRASHED,
            _NOW + dt.timedelta(seconds=1),
            expected_worker=world.worker,
        )

    await world.stop()

    assert world.kinds() == ["hydrate", "start", "stop", "snapshot"]
    assert world.stored() == (DesiredState.STOPPED, None)
    assert world.repo.by_id[world.server_id].observed_state is (
        ObservedState.STOPPED if ending == "refused" else ObservedState.CRASHED
    )


async def test_an_owed_stop_on_a_disconnected_worker_releases_without_a_stop() -> None:
    # A stop cannot reach a Worker whose session has ended, and the session's end
    # discarded every command still queued on it. The row is released as before,
    # with the exposure the release of an unknown row on a disconnected Worker
    # already accepts: a start that had begun may still be running there.
    world = _World()

    async def disconnect() -> None:
        world.cp._connected[world.worker] = False
        await world.repo.mark_worker_servers_unknown(
            world.worker, _NOW + dt.timedelta(seconds=1)
        )

    async def drain() -> None:
        assert await world.drain() == [world.server_id]

    _launches_then(world, disconnect, drain)
    await world.start()

    await world.stop()

    assert world.kinds() == ["hydrate", "start"]
    assert world.stored() == (DesiredState.STOPPED, None)


@pytest.mark.parametrize("answer", ["ok", "not-found"])
@pytest.mark.parametrize("stop", ["withdrawing", "owed"])
async def test_a_stop_run_behind_the_start_leaves_a_later_release_plain(
    stop: str, answer: str
) -> None:
    # A stop the Worker ran behind the start has stopped it, or found it gone:
    # the stop that withdrew the intent, or the one an operator stop owed after a
    # drain. A release still owed after that stop (its final snapshot timed out
    # here) is the plain one again: the final snapshot and the release, with no
    # second stop.
    world = _World()
    await world.start()
    if answer == "not-found":
        world.cp.live.discard(world.server_id)
    if stop == "owed":
        assert await world.drain() == [world.server_id]

    async def timed_out() -> None:
        raise WorkerUnavailableError(str(world.worker.value), upload_may_be_live=True)

    world.cp.snapshot_hooks.append(timed_out)
    await world.stop()
    assert world.stored() == (DesiredState.STOPPED, world.worker)

    await world.stop()

    assert world.cp.live == set()
    assert world.kinds() == ["hydrate", "start", "stop", "snapshot", "snapshot"]
    assert world.stored() == (DesiredState.STOPPED, None)


@pytest.mark.parametrize(
    ("observed", "convergence"),
    [
        (ObservedState.STOPPED, "clear_stale_assignment"),
        (ObservedState.UNKNOWN, "redispatch_stop"),
    ],
    ids=["clear", "redispatch-stop"],
)
async def test_a_convergence_that_finds_the_row_started_again_withdraws_nothing(
    observed: ObservedState, convergence: str
) -> None:
    # A reconciler tick listed the stopped, still-assigned row as owed this
    # convergence. Before it acts, the user's stop releases the row and the user
    # starts the server again. The tick reads the row desired running and gives
    # up. Its bump comes only after that check, so the user's start, hydrating
    # meanwhile, still finds the count it read and is sent, not answered 409.
    world = _World(assigned=True, observed=observed)
    await world.stop()
    assert world.stored() == (DesiredState.STOPPED, None)
    released = world.kinds()
    count = world.intents.current(world.server_id)

    async def tick() -> None:
        converge = getattr(world.stop_server(), convergence)
        routed = (
            {"worker_connected": True}
            if convergence == "clear_stale_assignment"
            else {}
        )
        with pytest.raises(InvalidLifecycleTransitionError):
            await converge(
                community_id=world.community, server_id=world.server_id, **routed
            )

    world.after_hydrate(tick)

    await world.start()

    assert world.kinds() == [*released, "hydrate", "start"]
    assert world.cp.live == {world.server_id}
    assert world.stored() == (DesiredState.RUNNING, world.worker)
    assert world.intents.current(world.server_id) == count


# Issue #3815: a start intent no Worker holds yet, (running, unassigned), while
# the reconciler retries its placement. A stop of it commits desired=stopped
# with the compare-and-set and the bump of any stop, and sends nothing: there is
# no Worker to tell and no placement load to drop. A placement racing the stop
# wins or loses on the row as the other write left it. Each test names the step
# of the placement the stop lands at.


async def test_a_stop_before_the_placement_leaves_it_no_intent_to_place() -> None:
    # The reconciler listed the row, then the operator's stop commits before the
    # placement reads it. The placement finds no running intent and sends nothing.
    world = _World(desired=DesiredState.RUNNING)

    stopped = await world.stop()

    assert stopped.desired_state is DesiredState.STOPPED
    assert world.stored() == (DesiredState.STOPPED, None)
    assert world.intents.current(world.server_id) == 1

    with pytest.raises(InvalidLifecycleTransitionError):
        await world.place()

    assert world.kinds() == []
    assert world.cp.reserved == []
    assert world.cp.load == []
    assert world.stored() == (DesiredState.STOPPED, None)


@pytest.mark.parametrize(
    "error",
    [ConnectionError("commit landed, response lost"), asyncio.CancelledError()],
    ids=["lost-response", "cancelled"],
)
async def test_a_stop_of_an_unplaced_intent_whose_commit_raised_still_bumps(
    error: BaseException,
) -> None:
    # As for an assigned row: the commit may have landed, so the stop bumps.
    world = _World(desired=DesiredState.RUNNING)

    await _withdraw_raising(world, "stop", error)

    assert world.stored() == (DesiredState.STOPPED, None)
    assert world.intents.current(world.server_id) == 1
    assert world.kinds() == []


async def test_a_stop_after_the_placement_read_the_row_wins_the_compare() -> None:
    # The stop commits once the placement has read (running, unassigned), while it
    # provisions the JAR and picks a Worker. The placement's compare-and-set
    # expects the row it read and matches nothing: it releases its reservation,
    # answers the conflict and sends nothing.
    world = _World(desired=DesiredState.RUNNING)

    async def stop() -> None:
        await world.stop()

    world.repo.after_next_read = stop

    with pytest.raises(LifecycleTransitionConflictError):
        await world.place()

    assert world.kinds() == []
    assert world.stored() == (DesiredState.STOPPED, None)
    assert world.cp.released == [(world.worker, world.server_id)]
    assert world.cp.load == []
    assert world.intents.current(world.server_id) == 1


async def test_a_stop_that_read_the_unplaced_row_loses_to_the_placement() -> None:
    # The stop read (running, unassigned) and waits on its write, while the
    # placement's compare-and-set lands and its hydrate runs. The stop's own
    # compare-and-set expects the row unassigned and matches nothing. It answers
    # the conflict and neither bumps nor drops the load, so the placement still
    # sends its start. A retried stop finds (running, W) and stops it as usual.
    world = _World(desired=DesiredState.RUNNING)
    write = asyncio.Event()
    world.repo.pause_next_flip = write
    stop = asyncio.create_task(world.stop())
    await _either(stop, world.repo.flip_paused)
    assert world.repo.flip_paused.is_set()

    async def stop_writes() -> None:
        write.set()
        with pytest.raises(LifecycleTransitionConflictError):
            await stop

    world.after_hydrate(stop_writes)

    await world.place()

    assert world.kinds() == ["hydrate", "start"]
    assert world.stored() == (DesiredState.RUNNING, world.worker)
    assert world.cp.live == {world.server_id}
    assert world.cp.load == ["confirm"]
    assert world.intents.current(world.server_id) == 0


async def test_a_stop_during_the_placement_hydrate_stops_the_placed_row() -> None:
    # The placement has committed its Worker, so the stop finds (running, W) and
    # takes the normal path: it drops the load and sends the stop, which the
    # Worker answers SERVER_NOT_FOUND as nothing runs yet, then releases the row.
    # The placement's re-read finds no intent and sends nothing.
    world = _World(desired=DesiredState.RUNNING)

    async def stop() -> None:
        await world.stop()

    world.after_hydrate(stop)

    with pytest.raises(LifecycleTransitionConflictError):
        await world.place()

    assert world.kinds() == ["hydrate", "stop"]
    assert world.stored() == (DesiredState.STOPPED, None)
    assert world.cp.live == set()
    assert world.cp.load == ["confirm", "drop"]
    assert world.intents.current(world.server_id) == 1


async def test_a_stop_after_the_placement_compare_is_sent_behind_its_start() -> None:
    # The stop becomes runnable as the placement's re-read closes, after which
    # nothing awaits before the start is enqueued. The start goes out first, and
    # the stop behind it is a real stop.
    world = _World(desired=DesiredState.RUNNING)
    uow = _ExitRunsHook(servers=world.repo)
    stops: list[asyncio.Task[Server]] = []

    async def schedule_stop() -> None:
        stops.append(asyncio.create_task(world.stop()))

    async def arm() -> None:
        uow.on_next_exit = schedule_stop

    world.after_hydrate(arm)

    await world.start_server(uow).place_and_start(
        community_id=world.community, server_id=world.server_id
    )
    await stops[0]

    assert world.kinds() == ["hydrate", "start", "stop", "snapshot"]
    assert world.stored() == (DesiredState.STOPPED, None)
    assert world.cp.live == set()
    assert world.cp.load == ["confirm", "drop"]


async def test_a_user_start_cannot_place_an_unplaced_intent_until_it_is_stopped() -> (
    None
):
    # A user start creates an intent, so it needs (stopped, unassigned) and
    # refuses one that already stands. Once a stop withdraws it, the start places
    # the server and sends its start.
    world = _World(desired=DesiredState.RUNNING)

    with pytest.raises(InvalidLifecycleTransitionError):
        await world.start()
    assert world.cp.reserved == []

    await world.stop()
    await world.start()

    assert world.kinds() == ["hydrate", "start"]
    assert world.stored() == (DesiredState.RUNNING, world.worker)
    assert world.cp.live == {world.server_id}


async def test_a_stale_placement_loses_to_a_stop_and_the_user_start_after_it() -> None:
    # The placement read the unplaced intent and holds the lifecycle lock while
    # it picks a Worker. A stop, which takes no lifecycle lock, withdraws the
    # intent, and the operator starts the server again: the start waits for the
    # lock. The placement's compare-and-set matches nothing, so it sends nothing,
    # and the start then places the server and sends the one start.
    world = _World(desired=DesiredState.RUNNING)
    world.lock = lock = _ExclusiveLock()
    starts: list[asyncio.Task[Server]] = []

    async def stop_then_start() -> None:
        await world.stop()
        starts.append(asyncio.create_task(world.start()))
        await _either(starts[0], lock.waited)

    world.repo.after_next_read = stop_then_start

    with pytest.raises(LifecycleTransitionConflictError):
        await world.place()
    await starts[0]

    assert world.kinds() == ["hydrate", "start"]
    assert world.stored() == (DesiredState.RUNNING, world.worker)
    assert world.cp.live == {world.server_id}
    assert world.cp.load == ["confirm"]
    assert world.cp.released == [(world.worker, world.server_id)]


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
    # The start waits for uploads in the count the data plane keeps (issue #3950).
    assert isinstance(start.store_generation, StorageGenerationReader)
    assert start.store_generation.uploads is get_snapshot_uploads(request)
    assert stop.stop_intents is app.state.stop_intents
    assert isinstance(drain.stopper, ServersAssignedServerStopper)
    assert drain.stopper.stop_servers.stop_intents is app.state.stop_intents


# --- an older start and a newer placement's mark (issue #3890, r2 finding 1) --


_KEEP_CODES = [
    "command_in_flight",
    "orphan_pending",
    "untracked_container",
    "command_expired",
]


def _refused(precondition: str) -> CommandOutcome:
    return CommandOutcome(
        status=worker_status("HydrateTrigger", precondition), message="refused"
    )


async def _due_tick_sends_nothing_new(world: _World) -> None:
    """Run the periodic snapshot past its jitter; it must dispatch nothing."""

    before = world.kinds()
    clock = FakeClock(_NOW)
    scheduler = RunSnapshotCadenceTick(
        uow=FakeUnitOfWork(servers=world.repo),
        control_plane=world.cp,
        clock=clock,
        default_interval_seconds=300,
        min_interval_seconds=60,
    )
    await scheduler.tick()
    clock.set(_NOW + dt.timedelta(hours=1))
    await scheduler.tick()
    assert world.kinds() == before


@pytest.mark.parametrize("precondition", _KEEP_CODES)
async def test_an_older_start_leaves_a_newer_placements_mark(precondition: str) -> None:
    # Start T1's hydrate is in flight when a stop releases the row, without a
    # snapshot since it is marked, and start T2 places it on the same Worker. T2's
    # hydrate is refused, so T2 keeps the Worker, marked. T1's hydrate then
    # answers. T1 no longer owns the row, so it clears nothing: the Worker holds
    # T1's copy, not the store's.
    world = _World()
    kept_mark: list[bool] = []

    async def stop_then_a_kept_start() -> None:
        await world.stop()

        async def refuse() -> CommandOutcome:
            return _refused(precondition)

        world.cp.hydrate_hooks.append(refuse)
        with pytest.raises(CommandDispatchError):
            await world.start()
        kept_mark.append(world.repo.by_id[world.server_id].awaiting_hydrate)

    world.after_hydrate(stop_then_a_kept_start)
    with pytest.raises(LifecycleTransitionConflictError):
        await world.start()

    assert kept_mark == [True]
    assert world.kinds() == ["hydrate", "stop", "hydrate"]
    row = world.repo.by_id[world.server_id]
    assert (row.desired_state, row.assigned_worker_id, row.awaiting_hydrate) == (
        DesiredState.RUNNING,
        world.worker,
        True,
    )
    await _due_tick_sends_nothing_new(world)


async def test_an_older_start_after_a_stale_clear_leaves_a_newer_placements_mark() -> (
    None
):
    # The r2 review's chain, with no task stalled by the test:
    # 1. Start T1 places the server on the Worker; its hydrate is in flight.
    # 2. The operator's stop waits behind that hydrate, and its request is
    #    cancelled. The row reads (stopped, stopped, assigned, marked).
    # 3. A second stop takes the stale clear: no start was sent, so it skips the
    #    final snapshot for the mark and releases the row.
    # 4. An at-rest edit or a restore moves the store.
    # 5. Start T2 places the server on the same Worker, marked, and its hydrate
    #    is refused UNAVAILABLE: kept.
    # 6. T1's hydrate answers. T2's mark survives: the Worker still holds the copy
    #    from before step 4.
    world = _World()
    guard = StopConvergenceGuard()
    kept_mark: list[bool] = []

    def stop_at(at: dt.datetime) -> StopServer:
        return StopServer(
            uow=FakeUnitOfWork(servers=world.repo),
            control_plane=world.cp,
            clock=FakeClock(at),
            stop_intents=world.intents,
            stop_convergence=guard,
        )

    async def during_t1_hydrate() -> None:
        world.cp.hold_stop_answer = asyncio.Event()
        stop = asyncio.create_task(
            stop_at(_NOW)(community_id=world.community, server_id=world.server_id)
        )
        await world.cp.stop_sent.wait()
        stop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stop
        world.cp.hold_stop_answer = None
        row = world.repo.by_id[world.server_id]
        assert (row.desired_state, row.assigned_worker_id, row.awaiting_hydrate) == (
            DesiredState.STOPPED,
            world.worker,
            True,
        )
        await stop_at(_NOW + dt.timedelta(seconds=120)).clear_stale_assignment(
            community_id=world.community,
            server_id=world.server_id,
            worker_connected=True,
        )
        assert world.stored() == (DesiredState.STOPPED, None)
        world.store_generation = 1

        async def refuse() -> CommandOutcome:
            return _refused("untracked_container")

        world.cp.hydrate_hooks.append(refuse)
        with pytest.raises(CommandDispatchError):
            await world.start()
        kept_mark.append(world.repo.by_id[world.server_id].awaiting_hydrate)

    world.after_hydrate(during_t1_hydrate)
    with pytest.raises(LifecycleTransitionConflictError):
        await world.start()

    assert kept_mark == [True]
    assert world.kinds() == ["hydrate", "stop", "hydrate"]
    row = world.repo.by_id[world.server_id]
    assert (row.desired_state, row.assigned_worker_id, row.awaiting_hydrate) == (
        DesiredState.RUNNING,
        world.worker,
        True,
    )
    await _due_tick_sends_nothing_new(world)


async def test_a_placement_between_the_compare_and_the_clear_keeps_its_mark() -> None:
    # The same release and newer placement, landing after start T1's compare has
    # passed and before its clear writes. The compare cannot see them, so the
    # clear's own guard does: it asserts the row as T1 read it, and the newer
    # placement's write moved ``updated_at``.
    world = _World()
    later = _NOW + dt.timedelta(minutes=1)

    async def stop_then_a_kept_start() -> None:
        await world.stop()
        # The store moves, so the newer start hydrates rather than reuse the set
        # T1's hydrate left.
        world.store_generation = 1

        async def refuse() -> CommandOutcome:
            return _refused("untracked_container")

        world.cp.hydrate_hooks.append(refuse)
        newer = replace(world.start_server(), clock=FakeClock(later))
        with pytest.raises(CommandDispatchError):
            await newer(community_id=world.community, server_id=world.server_id)

    async def arm() -> None:
        world.repo.before_next_clear = stop_then_a_kept_start

    world.after_hydrate(arm)
    with pytest.raises(LifecycleTransitionConflictError):
        await world.start()

    row = world.repo.by_id[world.server_id]
    assert (row.updated_at, row.assigned_worker_id, row.awaiting_hydrate) == (
        later,
        world.worker,
        True,
    )
    await _due_tick_sends_nothing_new(world)
