"""Every ``observed_state`` write reaches live subscribers, after its commit (#3212).

The events relay replays nothing, so a connected dashboard only learns of a
state change from a live ``status`` frame. Before issue #3212 only a Worker's
``StatusChange`` produced one: the worker-disconnect write, the API-startup
reset and the lifecycle use cases' own convergence writes committed silently,
and a client kept the pre-write state until its next snapshot.

Every ``desired_state`` change does too (#3428): a dashboard that never
learned of a start or stop made elsewhere offered the actions of the old
intent. So does every change of the assignment (#3818): a dashboard holding a
released server's old Worker offered a Start the API refuses.

Each test drives one write path over a real PostgreSQL row through the real
adapters -- the repository, the unit of work, the control-plane state sink --
with a subscriber attached to the real in-process bus, and asserts the frame
it receives. A journal of the row writes, the commits and the publishes pins
the ordering the relay's snapshot argument rests on (WEBUI_SPEC.md Section
2.6): no frame is published while the write it announces is uncommitted.

DB-gated (TESTING.md Section 5): run only when ``MCD_TEST_DATABASE_URL`` is set
(the CI Postgres service), skipped otherwise.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.fleet.adapters.real_time_events import (
    InProcessRealTimeEvents,
)
from mc_server_dashboard_api.fleet.domain.control_plane import CommandResultCode
from mc_server_dashboard_api.fleet.domain.real_time_events import (
    EventStream,
    EventSubscription,
    RealTimeEvent,
)
from mc_server_dashboard_api.servers.adapters.late_snapshot_result_sink import (
    ServersLateSnapshotResultSink,
)
from mc_server_dashboard_api.servers.adapters.repositories import (
    StatusEventsNotWiredError,
)
from mc_server_dashboard_api.servers.adapters.server_state_sink import (
    ServersServerStateSink,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.lifecycle import (
    RestartServer,
    StartServer,
    StopServer,
    StopServersAssignedToWorker,
)
from mc_server_dashboard_api.servers.application.manage_server import CreateServer
from mc_server_dashboard_api.servers.application.startup_reset import (
    ResetUnverifiableObservedStates,
)
from mc_server_dashboard_api.servers.application.stop_convergence import (
    StopConvergenceGuard,
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
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import (
    MAX_STATUS_DETAIL_BYTES,
    CommunityId,
    CrashReason,
    DesiredState,
    FinalSnapshotRefusal,
    ObservedState,
    WorkerId,
    bound_status_detail,
)
from tests.fleet.fakes import FakeClock as FleetFakeClock
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.integration.test_lifecycle_scenarios import (
    _AdvancingClock,
    _load,
    _seed_community,
)
from tests.servers.fakes import (
    FakeControlPlane,
    FakeFileStore,
    FakeJarProvisioner,
    FakeStoreGenerationReader,
    FakeVersionValidator,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_STATUS = frozenset({EventStream.STATUS})


class _JournalingBus(InProcessRealTimeEvents):
    """The real bus, journaling each status publish beside the DB activity."""

    def __init__(self, journal: list[str]) -> None:
        super().__init__()
        self._journal = journal

    def publish(self, *, server_id: str, event: RealTimeEvent) -> None:
        if event.stream is EventStream.STATUS:
            self._journal.append("publish")
        super().publish(server_id=server_id, event=event)


@dataclass
class _World:
    """One community's servers over a real database, with a journaled bus."""

    engine: AsyncEngine
    community: CommunityId
    journal: list[str]
    # The row-locking reads the observed-state writes issued, as emitted.
    locking_selects: list[str]
    bus: _JournalingBus
    clock: _AdvancingClock = field(default_factory=lambda: _AdvancingClock(_NOW))

    def uow(self) -> ServersUnitOfWork:
        """A unit of work wired to the bus, as the composition root builds it."""

        return ServersUnitOfWork(create_session_factory(self.engine), self.bus)

    def sink(self) -> ServersServerStateSink:
        return ServersServerStateSink(
            create_session_factory(self.engine),
            clock=self.clock,
            real_time_events=self.bus,
        )

    def start_server(self, control_plane: FakeControlPlane) -> StartServer:
        return StartServer(
            uow=self.uow(),
            control_plane=control_plane,
            clock=self.clock,
            jar_provisioner=FakeJarProvisioner(),
            store_generation=FakeStoreGenerationReader(),
            file_store=FakeFileStore(seed_eula=True),
        )

    def stop_server(self, control_plane: FakeControlPlane) -> StopServer:
        return StopServer(uow=self.uow(), control_plane=control_plane, clock=self.clock)

    async def server(
        self,
        name: str,
        *,
        worker: uuid.UUID | None = None,
        reported: str | None = None,
    ) -> Server:
        """Create a server; start it on ``worker``; have the worker report a state."""

        server = await CreateServer(
            uow=self.uow(),
            clock=self.clock,
            version_validator=FakeVersionValidator(),
            file_store=FakeFileStore(),
            port_range=PortRange(start=25565, end=25664),
        )(
            community_id=self.community,
            name=name,
            mc_edition="java",
            mc_version="1.21.1",
            server_type="vanilla",
            config={},
        )
        if worker is not None:
            await self.start_server(FakeControlPlane(place_to=WorkerId(worker)))(
                community_id=self.community, server_id=server.id
            )
        if reported is not None:
            assert worker is not None
            await self.report(server, worker, reported)
        return server

    async def report(self, server: Server, worker: uuid.UUID, state: str) -> bool:
        return await self.report_from(server, str(worker), state)

    async def report_from(self, server: Server, worker_id: str, state: str) -> bool:
        return await self.sink().record_observed_state(
            server_id=str(server.id.value),
            worker_id=worker_id,
            state=state,
            observed_at=self.clock.now(),
        )

    async def observed(self, server: Server) -> ObservedState:
        row = await _load(self.engine, server.id)
        assert row is not None
        return row.observed_state

    def watch(self, server: Server) -> EventSubscription:
        """Subscribe to ``server``'s status and forget the setup's journal."""

        self.journal.clear()
        return self.bus.subscribe(server_id=str(server.id.value), streams=_STATUS)

    def watch_all(self) -> EventSubscription:
        self.journal.clear()
        return self.bus.subscribe_all(streams=_STATUS)

    def fleet_clock(self) -> FleetFakeClock:
        """The world's clock, for the control plane's servicer and registry.

        The servicer stamps each Worker report as it reads it (issue #4118),
        the registry a session's end (issue #4130), and the guard compares both
        with the sink's own stamps, so they read one clock, as they read the
        wall clock in production.
        """

        return _WorldFleetClock(self)

    def assert_published_after_commit(self) -> None:
        """No status event was published over an uncommitted state write.

        The journal reads ``write`` (an UPDATE of ``observed_state``,
        ``desired_state``, ``assigned_worker_id`` or ``final_snapshot_refusal``),
        ``commit`` and ``publish`` in the order they happened; a ``publish``
        whose nearest earlier DB activity is a ``write`` announced a state no
        reader could see.
        """

        last_db_activity = None
        for entry in self.journal:
            if entry == "publish":
                assert last_db_activity == "commit", self.journal
            else:
                last_db_activity = entry


class _WorldFleetClock(FleetFakeClock):
    """Reads the world's clock; ``set`` has no effect."""

    def __init__(self, world: _World) -> None:
        super().__init__(_NOW)
        self._world = world

    def now(self) -> dt.datetime:
        return self._world.clock.now()


@pytest.fixture
async def world() -> AsyncIterator[_World]:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    engine = create_async_engine(_DB_URL)
    journal: list[str] = []
    locking_selects: list[str] = []

    def _on_execute(_conn: object, _cursor: object, statement: str, *_: object) -> None:
        if statement.startswith("UPDATE") and (
            "observed_state=" in statement
            or "desired_state=" in statement
            or "assigned_worker_id=" in statement
            or "final_snapshot_refusal=" in statement
        ):
            journal.append("write")
        if statement.startswith("SELECT") and "FOR UPDATE" in statement:
            locking_selects.append(statement)

    def _on_commit(_conn: object) -> None:
        journal.append("commit")

    sa_event.listen(engine.sync_engine, "before_cursor_execute", _on_execute)
    sa_event.listen(engine.sync_engine, "commit", _on_commit)
    try:
        community = CommunityId(await _seed_community(engine))
        yield _World(
            engine=engine,
            community=community,
            journal=journal,
            locking_selects=locking_selects,
            bus=_JournalingBus(journal),
        )
    finally:
        await engine.dispose()
        await downgrade_base(_DB_URL)


async def _received(subscription: EventSubscription) -> list[RealTimeEvent]:
    """Drain what ``subscription`` has buffered, then release it."""

    events: list[RealTimeEvent] = []
    try:
        while True:
            events.append(await asyncio.wait_for(anext(subscription), timeout=0.05))
    except TimeoutError:
        await subscription.aclose()
        return events


def _status(state: str, *, detail: str = "", reason: str = "") -> dict[str, object]:
    return {"state": state, "detail": detail, "reason": reason}


def _intent(desired: str) -> dict[str, object]:
    """The payload of the status frame a ``desired_state`` change publishes."""

    return {"desired_state": desired}


def _assigned(worker: uuid.UUID | None) -> dict[str, object]:
    """The field a status frame carries when its write changed the assignment."""

    return {"assigned_worker_id": None if worker is None else str(worker)}


# A release clears the final-snapshot refusal with the assignment (#4054).
_RELEASED = _assigned(None) | {"final_snapshot_refusal": None}


async def _states(subscription: EventSubscription) -> list[tuple[str | None, object]]:
    """The (server id, state) of each frame a firehose subscriber received."""

    return [
        (event.server_id, event.payload["state"])
        for event in await _received(subscription)
    ]


# --- The Worker path: a StatusChange through the state sink -----------------


async def test_worker_report_publishes_its_frame_after_commit(world: _World) -> None:
    worker = uuid.uuid4()
    server = await world.server("survival", worker=worker)
    emitted = dt.datetime(2026, 6, 4, 11, 59, tzinfo=dt.timezone.utc)
    watching = world.watch(server)

    applied = await world.sink().record_observed_state(
        server_id=str(server.id.value),
        worker_id=str(worker),
        state="crashed",
        detail="boom",
        reason="forge_install_failed",
        emitted_at=emitted,
        observed_at=world.clock.now(),
    )

    assert applied
    assert await _received(watching) == [
        RealTimeEvent(
            stream=EventStream.STATUS,
            payload=_status("crashed", detail="boom", reason="forge_install_failed"),
            emitted_at=emitted,
        )
    ]
    assert world.journal == ["write", "commit", "publish"]


async def test_worker_report_dropped_by_the_ownership_guard_publishes_nothing(
    world: _World,
) -> None:
    """A report the sink does not apply is not relayed either (issue #1957)."""

    server = await world.server("survival", worker=uuid.uuid4())
    watching = world.watch(server)

    applied = await world.report(server, uuid.uuid4(), "running")

    assert not applied
    assert await _received(watching) == []
    assert await world.observed(server) is ObservedState.STOPPED


async def test_worker_report_dropped_by_the_monotonic_guard_publishes_nothing(
    world: _World,
) -> None:
    worker = uuid.uuid4()
    server = await world.server("survival", worker=worker, reported="running")
    watching = world.watch(server)

    # Read at the world's first instant: no later than the row's observed_at.
    applied = await world.sink().record_observed_state(
        server_id=str(server.id.value),
        worker_id=str(worker),
        state="stopped",
        observed_at=_NOW,
    )

    assert not applied
    assert await _received(watching) == []
    assert await world.observed(server) is ObservedState.RUNNING


async def test_worker_report_repeating_the_state_publishes_only_what_is_new(
    world: _World,
) -> None:
    """A frame that would tell a subscriber nothing is not published.

    The bare repeat changes no state; the one carrying a detail does explain
    itself (the Worker's probe of an orphan it cannot confirm), so it is relayed.
    """

    worker = uuid.uuid4()
    server = await world.server("survival", worker=worker, reported="unknown")
    watching = world.watch(server)

    assert await world.report(server, worker, "unknown")
    assert await world.sink().record_observed_state(
        server_id=str(server.id.value),
        worker_id=str(worker),
        state="unknown",
        detail="cannot confirm",
        observed_at=world.clock.now(),
    )

    assert [event.payload for event in await _received(watching)] == [
        _status("unknown", detail="cannot confirm")
    ]


# --- The last crash: stored with the observed state (issue #3281) -------------


async def _crash(
    world: _World, server: Server
) -> tuple[CrashReason | None, str | None]:
    row = await _load(world.engine, server.id)
    assert row is not None
    return row.crash_reason, row.crash_detail


async def test_worker_crash_report_stores_the_crash_its_frame_carries(
    world: _World,
) -> None:
    """The row and the live frame explain the crash with the same bounded text,
    so a page loaded later shows what an open page was told."""

    worker = uuid.uuid4()
    server = await world.server("survival", worker=worker)
    detail = "installer ran out of memory " + "x" * MAX_STATUS_DETAIL_BYTES
    watching = world.watch(server)

    assert await world.sink().record_observed_state(
        server_id=str(server.id.value),
        worker_id=str(worker),
        state="crashed",
        detail=detail,
        reason="forge_install_out_of_memory",
        observed_at=world.clock.now(),
    )

    assert await _crash(world, server) == (
        CrashReason.FORGE_INSTALL_OUT_OF_MEMORY,
        bound_status_detail(detail),
    )
    assert [event.payload for event in await _received(watching)] == [
        _status(
            "crashed",
            detail=bound_status_detail(detail),
            reason="forge_install_out_of_memory",
        )
    ]


async def test_non_owning_worker_report_neither_stores_nor_clears_a_crash(
    world: _World,
) -> None:
    worker = uuid.uuid4()
    server = await world.server("survival", worker=worker)
    stranger = str(uuid.uuid4())

    assert not await world.sink().record_observed_state(
        server_id=str(server.id.value),
        worker_id=stranger,
        state="crashed",
        detail="not this server's crash",
        reason="forge_install_java_incompatible",
        observed_at=world.clock.now(),
    )
    assert await _crash(world, server) == (None, None)

    assert await world.sink().record_observed_state(
        server_id=str(server.id.value),
        worker_id=str(worker),
        state="crashed",
        detail="java.lang.UnsupportedClassVersionError",
        reason="forge_install_java_incompatible",
        observed_at=world.clock.now(),
    )
    assert not await world.report_from(server, stranger, "running")
    assert await _crash(world, server) == (
        CrashReason.FORGE_INSTALL_JAVA_INCOMPATIBLE,
        "java.lang.UnsupportedClassVersionError",
    )


async def test_operator_restart_of_a_crash_clears_it_once_the_worker_reports_starting(
    world: _World,
) -> None:
    """Stop then Start after a crash: the crash stays recorded while the server
    still reads crashed, and goes with the first report of the new launch."""

    worker = uuid.uuid4()
    server = await world.server("survival", worker=worker)
    assert await world.sink().record_observed_state(
        server_id=str(server.id.value),
        worker_id=str(worker),
        state="crashed",
        detail="installer ran out of memory",
        reason="forge_install_out_of_memory",
        observed_at=world.clock.now(),
    )
    await world.stop_server(
        FakeControlPlane(
            outcomes={"stop": CommandOutcome(status=CommandStatus.SERVER_NOT_FOUND)}
        )
    )(community_id=world.community, server_id=server.id)
    await world.start_server(FakeControlPlane(place_to=WorkerId(worker)))(
        community_id=world.community, server_id=server.id
    )
    assert await _crash(world, server) == (
        CrashReason.FORGE_INSTALL_OUT_OF_MEMORY,
        "installer ran out of memory",
    )

    assert await world.report(server, worker, "starting")

    assert await _crash(world, server) == (None, None)


# --- The Worker disconnect: the bulk unknown write ---------------------------


async def test_worker_disconnect_publishes_unknown_for_each_changed_server(
    world: _World,
) -> None:
    worker, other_worker = uuid.uuid4(), uuid.uuid4()
    running = await world.server("running", worker=worker, reported="running")
    crashed = await world.server("crashed", worker=worker, reported="crashed")
    # Crashed under a stop intent whose dispatch failed: the disconnect moves
    # only its stamp.
    stopping = await world.server("stopping", worker=worker, reported="running")
    with pytest.raises(WorkerUnavailableError):
        await world.stop_server(FakeControlPlane(unavailable_kinds={"stop"}))(
            community_id=world.community, server_id=stopping.id
        )
    await world.report(stopping, worker, "crashed")
    await world.server("unknown", worker=worker, reported="unknown")
    await world.server("elsewhere", worker=other_worker, reported="running")
    watching = world.watch_all()

    await world.sink().mark_worker_servers_unknown(worker_id=str(worker))

    # One frame per server the disconnect changed: none for the row already
    # unknown, none for the other worker's server, and none for either crashed
    # one, which keeps its crash (issue #3885).
    assert await _states(watching) == [(str(running.id.value), "unknown")]
    assert await world.observed(crashed) is ObservedState.CRASHED
    assert await world.observed(stopping) is ObservedState.CRASHED
    # After the one commit: no transaction is open across a publish.
    assert world.journal == ["write", "commit", "publish"]


async def test_worker_disconnect_frame_has_the_worker_path_payload_shape(
    world: _World,
) -> None:
    worker = uuid.uuid4()
    server = await world.server("survival", worker=worker, reported="running")
    watching = world.watch(server)

    await world.sink().mark_worker_servers_unknown(worker_id=str(worker))

    assert await _received(watching) == [
        RealTimeEvent(stream=EventStream.STATUS, payload=_status("unknown"))
    ]


# --- The API-startup reset ---------------------------------------------------


async def test_startup_reset_publishes_unknown_for_each_reset_server(
    world: _World,
) -> None:
    worker = uuid.uuid4()
    running = await world.server("running", worker=worker, reported="running")
    stopping = await world.server("stopping", worker=worker, reported="stopping")
    await world.server("crashed", worker=worker, reported="crashed")
    await world.server("unassigned")
    watching = world.watch_all()

    count = await ResetUnverifiableObservedStates(uow=world.uow(), clock=world.clock)()

    assert count == 2
    assert sorted(await _states(watching), key=str) == sorted(
        [
            (str(running.id.value), "unknown"),
            (str(stopping.id.value), "unknown"),
        ],
        key=str,
    )
    assert world.journal == ["write", "commit", "publish", "publish"]


# --- The lifecycle use cases' own convergence writes -------------------------

_ALREADY_RUNNING = {"start": CommandOutcome(status=CommandStatus.INVALID_STATE)}


async def test_start_converging_on_an_already_running_instance_publishes_running(
    world: _World,
) -> None:
    """StartServer's INVALID_STATE arm: no Worker StatusChange ever follows."""

    server = await world.server("survival")
    worker = uuid.uuid4()
    watching = world.watch(server)

    await world.start_server(
        FakeControlPlane(place_to=WorkerId(worker), outcomes=_ALREADY_RUNNING)
    )(community_id=world.community, server_id=server.id)

    assert [event.payload for event in await _received(watching)] == [
        _intent("running") | _assigned(worker),
        _status("running"),
    ]
    assert await world.observed(server) is ObservedState.RUNNING
    world.assert_published_after_commit()


async def test_redispatched_start_converging_on_a_running_instance_publishes_running(
    world: _World,
) -> None:
    """The reconciler's redispatch_start INVALID_STATE arm (issue #213)."""

    worker = uuid.uuid4()
    server = await world.server("survival", worker=worker, reported="unknown")
    watching = world.watch(server)

    await world.start_server(
        FakeControlPlane(outcomes=_ALREADY_RUNNING)
    ).redispatch_start(community_id=world.community, server_id=server.id)

    assert [event.payload for event in await _received(watching)] == [
        _status("running")
    ]
    world.assert_published_after_commit()


async def test_confirmed_stop_publishes_stopped(world: _World) -> None:
    """StopServer's confirmed-stop write, when the Worker's own report is lost.

    The release after the final snapshot follows it (#3818).
    """

    worker = uuid.uuid4()
    server = await world.server("survival", worker=worker, reported="running")
    watching = world.watch(server)

    await world.stop_server(FakeControlPlane())(
        community_id=world.community, server_id=server.id
    )

    assert [event.payload for event in await _received(watching)] == [
        _intent("stopped"),
        _status("stopped"),
        _RELEASED,
    ]
    world.assert_published_after_commit()


async def test_confirmed_stop_after_the_workers_stopped_report_repeats_no_state(
    world: _World,
) -> None:
    """The ordinary stop: the Worker's ``stopped`` frame already announced it.

    The confirmed-stop write then changes no state, so it repeats no frame;
    only the stop intent and the release after the final snapshot are new.
    """

    worker = uuid.uuid4()
    server = await world.server("survival", worker=worker, reported="stopped")
    watching = world.watch(server)

    await world.stop_server(FakeControlPlane())(
        community_id=world.community, server_id=server.id
    )

    assert [event.payload for event in await _received(watching)] == [
        _intent("stopped"),
        _RELEASED,
    ]
    world.assert_published_after_commit()


async def test_stop_of_an_instance_the_worker_forgot_publishes_stopped(
    world: _World,
) -> None:
    """StopServer's SERVER_NOT_FOUND release (issue #2448): no Worker report."""

    worker = uuid.uuid4()
    server = await world.server("survival", worker=worker, reported="running")
    watching = world.watch(server)

    await world.stop_server(
        FakeControlPlane(
            outcomes={"stop": CommandOutcome(status=CommandStatus.SERVER_NOT_FOUND)}
        )
    )(community_id=world.community, server_id=server.id)

    assert [event.payload for event in await _received(watching)] == [
        _intent("stopped"),
        _status("stopped"),
        _RELEASED,
    ]
    world.assert_published_after_commit()


async def test_stop_of_a_crashed_instance_leaves_the_crash_and_publishes_intent(
    world: _World,
) -> None:
    """The same release keeps ``crashed`` standing: the intent and the
    assignment changed, the observed state did not."""

    worker = uuid.uuid4()
    server = await world.server("survival", worker=worker, reported="crashed")
    watching = world.watch(server)

    await world.stop_server(
        FakeControlPlane(
            outcomes={"stop": CommandOutcome(status=CommandStatus.SERVER_NOT_FOUND)}
        )
    )(community_id=world.community, server_id=server.id)

    assert [event.payload for event in await _received(watching)] == [
        _intent("stopped"),
        _RELEASED,
    ]
    assert await world.observed(server) is ObservedState.CRASHED
    world.assert_published_after_commit()


async def test_releasing_a_stop_wedged_at_stopping_publishes_unknown(
    world: _World,
) -> None:
    """clear_stale_assignment's stopping leg (issue #2452): the Worker is gone."""

    worker = uuid.uuid4()
    server = await world.server("survival", worker=worker, reported="running")
    with pytest.raises(WorkerUnavailableError):
        await world.stop_server(FakeControlPlane(unavailable_kinds={"stop"}))(
            community_id=world.community, server_id=server.id
        )
    await world.report(server, worker, "stopping")
    watching = world.watch(server)

    await world.stop_server(
        FakeControlPlane(connected={WorkerId(worker): False})
    ).clear_stale_assignment(
        community_id=world.community,
        server_id=server.id,
        worker_connected=False,
    )

    assert [event.payload for event in await _received(watching)] == [
        _status("unknown") | _RELEASED
    ]
    assert await world.observed(server) is ObservedState.UNKNOWN
    world.assert_published_after_commit()


# --- desired_state writes: the intent frame (issue #3428) --------------------


async def test_start_publishes_its_intent_on_the_server_and_community_streams(
    world: _World,
) -> None:
    """Another tab's dashboard learns the server is now meant to run.

    The per-server stream subscribes to the server's topic and the community
    stream to the firehose; both receive the frame, after the commit.
    """

    server = await world.server("survival")
    worker = uuid.uuid4()
    watching = world.watch(server)
    firehose = world.bus.subscribe_all(streams=_STATUS)

    await world.start_server(FakeControlPlane(place_to=WorkerId(worker)))(
        community_id=world.community, server_id=server.id
    )

    # The same write placed the server: the frame carries the Worker (#3818).
    started = _intent("running") | _assigned(worker)
    assert await _received(watching) == [
        RealTimeEvent(stream=EventStream.STATUS, payload=started)
    ]
    assert [
        (event.server_id, event.payload) for event in await _received(firehose)
    ] == [(str(server.id.value), started)]
    world.assert_published_after_commit()


async def test_stop_publishes_its_intent_on_the_server_and_community_streams(
    world: _World,
) -> None:
    worker = uuid.uuid4()
    server = await world.server("survival", worker=worker, reported="running")
    watching = world.watch(server)
    firehose = world.bus.subscribe_all(streams=_STATUS)

    # The Worker never answers, so the stop intent is the only write.
    with pytest.raises(WorkerUnavailableError):
        await world.stop_server(FakeControlPlane(unavailable_kinds={"stop"}))(
            community_id=world.community, server_id=server.id
        )

    assert [event.payload for event in await _received(watching)] == [
        _intent("stopped")
    ]
    assert [
        (event.server_id, event.payload) for event in await _received(firehose)
    ] == [(str(server.id.value), _intent("stopped"))]
    world.assert_published_after_commit()


async def test_drain_publishes_a_stopped_intent_for_each_server_it_flips(
    world: _World,
) -> None:
    worker = uuid.uuid4()
    a = await world.server("a", worker=worker, reported="running")
    b = await world.server("b", worker=worker, reported="running")
    await world.server("elsewhere", worker=uuid.uuid4(), reported="running")
    watching = world.watch_all()

    await StopServersAssignedToWorker(uow=world.uow(), clock=world.clock)(
        worker_id=WorkerId(worker)
    )

    received = [(event.server_id, event.payload) for event in await _received(watching)]
    assert sorted(received, key=str) == sorted(
        [
            (str(a.id.value), _intent("stopped")),
            (str(b.id.value), _intent("stopped")),
        ],
        key=str,
    )
    world.assert_published_after_commit()


async def test_compensated_start_publishes_the_reverted_intent(world: _World) -> None:
    """A dispatch failure before the start was sent reverts the intent."""

    server = await world.server("survival")
    worker = uuid.uuid4()
    watching = world.watch(server)

    with pytest.raises(WorkerUnavailableError):
        await world.start_server(
            FakeControlPlane(place_to=WorkerId(worker), unavailable_kinds={"hydrate"})
        )(community_id=world.community, server_id=server.id)

    assert [event.payload for event in await _received(watching)] == [
        _intent("running") | _assigned(worker),
        _intent("stopped") | _RELEASED,
    ]
    world.assert_published_after_commit()


async def test_only_a_write_that_changes_the_intent_publishes_one(
    world: _World,
) -> None:
    """A restart rewrites the row under the same running intent: no frame.

    The stop that follows does change it, so exactly its frame arrives.
    """

    worker = uuid.uuid4()
    server = await world.server("survival", worker=worker, reported="running")
    watching = world.watch(server)

    await RestartServer(
        uow=world.uow(), control_plane=FakeControlPlane(), clock=world.clock
    )(community_id=world.community, server_id=server.id)
    with pytest.raises(WorkerUnavailableError):
        await world.stop_server(FakeControlPlane(unavailable_kinds={"stop"}))(
            community_id=world.community, server_id=server.id
        )

    assert [event.payload for event in await _received(watching)] == [
        _intent("stopped")
    ]


async def _write_running_intent(uow: ServersUnitOfWork, server: Server) -> None:
    row = await uow.servers.get_by_id(server.id)
    assert row is not None
    row.desired_state = DesiredState.RUNNING
    assert await uow.servers.update_lifecycle(
        row, expected_from=DesiredState.STOPPED, expected_assigned_worker_id=None
    )


async def test_rolled_back_intent_write_publishes_nothing(world: _World) -> None:
    """Only the committed write of the two announces itself."""

    server = await world.server("survival")
    watching = world.watch(server)

    async with world.uow() as uow:
        await _write_running_intent(uow, server)
        # Left without a commit: the unit of work rolls back.
    async with world.uow() as uow:
        await _write_running_intent(uow, server)
        await uow.commit()

    assert [event.payload for event in await _received(watching)] == [
        _intent("running")
    ]


async def test_intent_write_whose_commit_fails_publishes_nothing(
    world: _World,
) -> None:
    """Only the write whose commit succeeded announces itself."""

    server = await world.server("survival")

    class _CommitFails(AsyncSession):
        async def commit(self) -> None:
            raise OSError("connection lost at commit")

    failing = ServersUnitOfWork(
        async_sessionmaker(world.engine, expire_on_commit=False, class_=_CommitFails),
        world.bus,
    )
    watching = world.watch(server)

    with pytest.raises(OSError, match="connection lost"):
        async with failing as uow:
            await _write_running_intent(uow, server)
            await uow.commit()
    async with world.uow() as uow:
        await _write_running_intent(uow, server)
        await uow.commit()

    assert [event.payload for event in await _received(watching)] == [
        _intent("running")
    ]


async def test_unit_of_work_without_the_bus_refuses_to_commit_an_intent_write(
    world: _World,
) -> None:
    server = await world.server("survival")

    uow = ServersUnitOfWork(create_session_factory(world.engine))
    with pytest.raises(StatusEventsNotWiredError):
        async with uow:
            row = await uow.servers.get_by_id(server.id)
            assert row is not None
            row.desired_state = DesiredState.RUNNING
            await uow.servers.update_lifecycle(
                row,
                expected_from=DesiredState.STOPPED,
                expected_assigned_worker_id=None,
            )
            await uow.commit()

    async with world.uow() as uow:
        row = await uow.servers.get_by_id(server.id)
    assert row is not None
    assert row.desired_state is DesiredState.STOPPED


# --- Assignment writes: the assignment frame (issue #3818) -----------------
#
# Every write that places or releases a server publishes the assignment it
# left, after its commit, so a client never gates on a released Worker. A
# release changes nothing else but the final-snapshot refusal it clears (#4054),
# so its frame carries those two alone.


async def test_reconciler_placement_publishes_the_assignment_alone(
    world: _World,
) -> None:
    """``place_and_start`` places a desired-running orphan; the intent stands."""

    server = await world.server("survival")
    async with world.uow() as uow:
        await _write_running_intent(uow, server)
        await uow.commit()
    worker = uuid.uuid4()
    watching = world.watch(server)

    await world.start_server(
        FakeControlPlane(place_to=WorkerId(worker))
    ).place_and_start(community_id=world.community, server_id=server.id)

    assert [event.payload for event in await _received(watching)] == [_assigned(worker)]
    world.assert_published_after_commit()


async def test_undone_placement_publishes_the_release(world: _World) -> None:
    """``place_and_start``'s pre-dispatch failure undoes only the assignment."""

    server = await world.server("survival")
    async with world.uow() as uow:
        await _write_running_intent(uow, server)
        await uow.commit()
    worker = uuid.uuid4()
    watching = world.watch(server)

    with pytest.raises(WorkerUnavailableError):
        await world.start_server(
            FakeControlPlane(place_to=WorkerId(worker), unavailable_kinds={"hydrate"})
        ).place_and_start(community_id=world.community, server_id=server.id)

    assert [event.payload for event in await _received(watching)] == [
        _assigned(worker),
        _RELEASED,
    ]
    world.assert_published_after_commit()


async def _wedged_stop(world: _World, worker: uuid.UUID, reported: str) -> Server:
    """A server left at (stopped, ``reported``, assigned to ``worker``).

    Its stop never reached the Worker, which then reported ``reported``.
    """

    server = await world.server("survival", worker=worker, reported="running")
    with pytest.raises(WorkerUnavailableError):
        await world.stop_server(FakeControlPlane(unavailable_kinds={"stop"}))(
            community_id=world.community, server_id=server.id
        )
    assert await world.report(server, worker, reported)
    return server


@pytest.mark.parametrize("wedged_at", ["stopped", "crashed", "unknown"])
async def test_stale_assignment_release_publishes_the_release(
    world: _World, wedged_at: str
) -> None:
    """``clear_stale_assignment`` with the Worker gone: the row is released only."""

    worker = uuid.uuid4()
    server = await _wedged_stop(world, worker, wedged_at)
    watching = world.watch(server)

    await world.stop_server(
        FakeControlPlane(connected={WorkerId(worker): False})
    ).clear_stale_assignment(
        community_id=world.community,
        server_id=server.id,
        worker_connected=False,
    )

    assert [event.payload for event in await _received(watching)] == [_RELEASED]
    assert await world.observed(server) is ObservedState(wedged_at)
    world.assert_published_after_commit()


async def test_late_final_snapshot_release_publishes_the_release(
    world: _World,
) -> None:
    """The late-snapshot sink, as the composition root wires it to the bus."""

    worker = uuid.uuid4()
    server = await _wedged_stop(world, worker, "stopped")
    sink = ServersLateSnapshotResultSink(
        create_session_factory(world.engine),
        control_plane=FakeControlPlane(),
        clock=world.clock,
        stop_convergence=StopConvergenceGuard(),
        unrecorded_stops=UnrecordedStops(),
        real_time_events=world.bus,
    )
    watching = world.watch(server)

    await sink.clear_held_assignment_on_late_snapshot(
        server_id=str(server.id.value),
        worker_id=str(worker),
        code=CommandResultCode.OK,
        message=None,
        received_at=world.clock.now(),
    )

    assert [event.payload for event in await _received(watching)] == [_RELEASED]
    world.assert_published_after_commit()


async def test_release_under_an_unchanged_state_publishes_the_release_alone(
    world: _World,
) -> None:
    """An unassigning write that repeats the state still announces the release."""

    worker = uuid.uuid4()
    server = await _wedged_stop(world, worker, "unknown")
    watching = world.watch(server)

    async with world.uow() as uow:
        assert await uow.servers.record_observed_state(
            server.id,
            ObservedState.UNKNOWN,
            world.clock.now(),
            unassign=True,
            expected_worker=WorkerId(worker),
        )
        await uow.commit()

    assert [event.payload for event in await _received(watching)] == [_RELEASED]


async def test_release_guarded_off_another_workers_row_publishes_nothing(
    world: _World,
) -> None:
    """A clear that matched no row released nothing, so it announces nothing.

    Its caller commits whatever the write returned; a frame here would tell
    clients a Worker still holding the server had let it go.
    """

    worker = uuid.uuid4()
    server = await _wedged_stop(world, worker, "stopped")
    watching = world.watch(server)

    async with world.uow() as uow:
        assert not await uow.servers.clear_assignment_after_final_snapshot(
            server.id, WorkerId(uuid.uuid4())
        )
        await uow.commit()

    assert await _received(watching) == []


async def test_stale_release_guarded_off_a_newer_report_publishes_nothing(
    world: _World,
) -> None:
    """The stale-stop clear matched no row: a report landed since its read.

    ``observed_at`` moved (issue #3766), so the clear released nothing and
    announces nothing.
    """

    worker = uuid.uuid4()
    server = await _wedged_stop(world, worker, "stopped")
    async with world.uow() as uow:
        row = await uow.servers.get_by_id(server.id)
    assert row is not None and row.observed_at is not None
    read_at = row.observed_at - dt.timedelta(seconds=1)
    watching = world.watch(server)

    async with world.uow() as uow:
        assert not await uow.servers.clear_stale_assignment(
            server.id, WorkerId(worker), observed_at=read_at
        )
        await uow.commit()

    assert await _received(watching) == []


# --- The final-snapshot refusal: its own frame (issue #4054) -----------------


async def test_final_snapshot_refusal_publishes_its_frame_after_commit(
    world: _World,
) -> None:
    """A refusal set on a stop holding its Worker."""

    worker = uuid.uuid4()
    server = await _wedged_stop(world, worker, "stopped")
    watching = world.watch(server)

    async with world.uow() as uow:
        assert await uow.servers.set_final_snapshot_refusal(
            server.id, WorkerId(worker), FinalSnapshotRefusal.UNAVAILABLE
        )
        await uow.commit()

    assert [event.payload for event in await _received(watching)] == [
        {"final_snapshot_refusal": "unavailable"},
    ]
    world.assert_published_after_commit()


async def test_final_snapshot_refusal_that_changes_nothing_publishes_nothing(
    world: _World,
) -> None:
    """The same refusal again, or one from a Worker the stop does not hold."""

    worker = uuid.uuid4()
    server = await _wedged_stop(world, worker, "stopped")
    async with world.uow() as uow:
        assert await uow.servers.set_final_snapshot_refusal(
            server.id, WorkerId(worker), FinalSnapshotRefusal.BUSY
        )
        await uow.commit()
    watching = world.watch(server)

    async with world.uow() as uow:
        assert not await uow.servers.set_final_snapshot_refusal(
            server.id, WorkerId(worker), FinalSnapshotRefusal.BUSY
        )
        assert not await uow.servers.set_final_snapshot_refusal(
            server.id, WorkerId(uuid.uuid4()), FinalSnapshotRefusal.UNAVAILABLE
        )
        await uow.commit()

    assert await _received(watching) == []


async def test_lost_lifecycle_compare_and_set_publishes_nothing(
    world: _World,
) -> None:
    """A compare-and-set that matched no row changed neither intent nor Worker.

    Here it expected another Worker than the one the row holds, as a failed
    start's revert does once a newer placement has moved the row on.
    """

    worker = uuid.uuid4()
    server = await world.server("survival", worker=worker, reported="running")
    watching = world.watch(server)

    async with world.uow() as uow:
        row = await uow.servers.get_by_id(server.id)
        assert row is not None
        row.desired_state = DesiredState.STOPPED
        row.assigned_worker_id = None
        assert not await uow.servers.update_lifecycle(
            row,
            expected_from=DesiredState.RUNNING,
            expected_assigned_worker_id=WorkerId(uuid.uuid4()),
        )
        await uow.commit()

    assert await _received(watching) == []


# --- The unit of work: nothing is published for a write that did not commit --


async def test_rolled_back_write_publishes_nothing(world: _World) -> None:
    worker = uuid.uuid4()
    server = await world.server("survival", worker=worker)
    watching = world.watch(server)

    uow = world.uow()
    async with uow:
        assert await uow.servers.record_observed_state(
            server.id, ObservedState.RUNNING, world.clock.now()
        )
        # Left without a commit: the unit of work rolls back.

    assert await _received(watching) == []
    assert await world.observed(server) is ObservedState.STOPPED


async def test_write_rolled_back_before_a_later_commit_publishes_nothing(
    world: _World,
) -> None:
    """A rollback discards the staged frame; a later commit cannot resurrect it."""

    worker = uuid.uuid4()
    server = await world.server("survival", worker=worker)
    watching = world.watch(server)

    uow = world.uow()
    async with uow:
        await uow.servers.record_observed_state(
            server.id, ObservedState.RUNNING, world.clock.now()
        )
        await uow.rollback()
        await uow.commit()

    assert await _received(watching) == []
    assert await world.observed(server) is ObservedState.STOPPED


async def test_savepoint_rollback_discards_only_the_frames_staged_inside_it(
    world: _World,
) -> None:
    """A rolled-back savepoint takes its own frame with it, and no other."""

    worker = uuid.uuid4()
    kept = await world.server("kept", worker=worker)
    undone = await world.server("undone", worker=worker)
    watching = world.watch_all()

    uow = world.uow()
    async with uow:
        await uow.servers.record_observed_state(
            kept.id, ObservedState.STARTING, world.clock.now()
        )
        with pytest.raises(RuntimeError, match="refused"):
            async with uow.savepoint():
                await uow.servers.record_observed_state(
                    undone.id, ObservedState.RUNNING, world.clock.now()
                )
                raise RuntimeError("refused")
        await uow.commit()

    assert await _states(watching) == [(str(kept.id.value), "starting")]
    assert await world.observed(undone) is ObservedState.STOPPED


# --- A commit owner built without the bus fails loudly ----------------------


async def test_unit_of_work_without_the_bus_refuses_to_commit_an_observed_write(
    world: _World,
) -> None:
    """Missing wiring is a programming error, not a silently dropped frame."""

    server = await world.server("survival", worker=uuid.uuid4())

    uow = ServersUnitOfWork(create_session_factory(world.engine))
    with pytest.raises(StatusEventsNotWiredError):
        async with uow:
            await uow.servers.record_observed_state(
                server.id, ObservedState.RUNNING, world.clock.now()
            )
            await uow.commit()

    # Refused before the commit: the write did not land silently either.
    assert await world.observed(server) is ObservedState.STOPPED


async def test_state_sink_without_the_bus_refuses_to_commit_an_observed_write(
    world: _World,
) -> None:
    worker = uuid.uuid4()
    server = await world.server("survival", worker=worker)
    unwired = ServersServerStateSink(
        create_session_factory(world.engine), clock=world.clock
    )

    with pytest.raises(StatusEventsNotWiredError):
        await unwired.record_observed_state(
            server_id=str(server.id.value),
            worker_id=str(worker),
            state="running",
            observed_at=world.clock.now(),
        )
    with pytest.raises(StatusEventsNotWiredError):
        await unwired.mark_worker_servers_unknown(worker_id=str(worker))

    assert await world.observed(server) is ObservedState.STOPPED


# --- Lock order ---------------------------------------------------------------


async def test_observed_state_writes_lock_server_rows_in_id_order(
    world: _World,
) -> None:
    """One lock order for every writer, so overlapping bulk writes cannot deadlock."""

    worker = uuid.uuid4()
    await world.server("a", worker=worker, reported="running")
    await world.server("b", worker=worker, reported="running")
    world.locking_selects.clear()

    await world.sink().mark_worker_servers_unknown(worker_id=str(worker))
    disconnect = list(world.locking_selects)
    await ResetUnverifiableObservedStates(uow=world.uow(), clock=world.clock)()

    # The disconnect takes every lock it needs in its first statement, the
    # crashed rows under a stopped intent whose stamp it moves included (issue
    # #3885). Its write's own locking read then re-reads only those rows, so it
    # never asks for a lower id after a higher one.
    assert len(disconnect) == 2
    assert "server.desired_state" in disconnect[0]
    assert "WHERE server.id IN" in disconnect[1]
    assert len(world.locking_selects) == 3
    for statement in world.locking_selects:
        assert "ORDER BY server.id" in statement
        assert statement.rstrip().endswith("FOR UPDATE")


# --- Known residual: publish order across connections (issue #3307) ---------


async def test_publish_order_follows_commit_completion_not_commit_order(
    world: _World,
) -> None:
    """DOCUMENTS CURRENT BEHAVIOUR, not the desired one (issue #3307).

    Each write publishes right after its own commit returns, so two writers on
    different connections publish in the order their commit acknowledgements
    reach the event loop -- which can differ from the order the database
    committed them in. Here the worker's ``running`` report commits first but
    its commit is slow to return; the disconnect's ``unknown`` commits and
    publishes in the meantime; ``running`` is then published last. The row
    reads ``unknown`` and a subscriber is left on ``running`` until the next
    frame, gap or snapshot (WEBUI_SPEC.md Section 2.6).

    Closing this needs per-server sequencing of status frames (issue #3307).
    When that lands, the final assertion flips: the last state a subscriber
    holds must be ``unknown``.
    """

    worker = uuid.uuid4()
    server = await world.server("survival", worker=worker)
    committed, release = asyncio.Event(), asyncio.Event()

    class _SlowToAcknowledgeSession(AsyncSession):
        async def commit(self) -> None:
            await super().commit()
            committed.set()
            await release.wait()

    slow_report = ServersServerStateSink(
        async_sessionmaker(
            world.engine, expire_on_commit=False, class_=_SlowToAcknowledgeSession
        ),
        clock=world.clock,
        real_time_events=world.bus,
    )
    watching = world.watch(server)

    report = asyncio.create_task(
        slow_report.record_observed_state(
            server_id=str(server.id.value),
            worker_id=str(worker),
            state="running",
            observed_at=world.clock.now(),
        )
    )
    await committed.wait()
    await world.sink().mark_worker_servers_unknown(worker_id=str(worker))
    release.set()
    assert await report

    assert await world.observed(server) is ObservedState.UNKNOWN
    states = [event.payload["state"] for event in await _received(watching)]
    # Stale: the database committed running, then unknown.
    assert states == ["unknown", "running"]
