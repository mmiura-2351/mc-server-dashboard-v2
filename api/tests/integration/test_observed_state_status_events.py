"""Every ``observed_state`` write reaches live subscribers, after its commit (#3212).

The events relay replays nothing, so a connected dashboard only learns of a
state change from a live ``status`` frame. Before issue #3212 only a Worker's
``StatusChange`` produced one: the worker-disconnect write, the API-startup
reset and the lifecycle use cases' own convergence writes committed silently,
and a client kept the pre-write state until its next snapshot.

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
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.fleet.adapters.real_time_events import (
    InProcessRealTimeEvents,
)
from mc_server_dashboard_api.fleet.domain.real_time_events import (
    EventStream,
    EventSubscription,
    RealTimeEvent,
)
from mc_server_dashboard_api.servers.adapters.server_state_sink import (
    ServersServerStateSink,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.lifecycle import (
    StartServer,
    StopServer,
)
from mc_server_dashboard_api.servers.application.manage_server import CreateServer
from mc_server_dashboard_api.servers.application.startup_reset import (
    ResetUnverifiableObservedStates,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ObservedState,
    WorkerId,
)
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.integration.test_lifecycle_scenarios import (
    _AdvancingClock,
    _load,
    _seed_community,
)
from tests.servers.fakes import (
    FakeClock,
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
        return await self.sink().record_observed_state(
            server_id=str(server.id.value), worker_id=str(worker), state=state
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

    def assert_published_after_commit(self) -> None:
        """No status event was published over an uncommitted observed-state write.

        The journal reads ``write`` (an UPDATE of ``observed_state``), ``commit``
        and ``publish`` in the order they happened; a ``publish`` whose nearest
        earlier DB activity is a ``write`` announced a state no reader could see.
        """

        last_db_activity = None
        for entry in self.journal:
            if entry == "publish":
                assert last_db_activity == "commit", self.journal
            else:
                last_db_activity = entry


@pytest.fixture
async def world() -> AsyncIterator[_World]:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    engine = create_async_engine(_DB_URL)
    journal: list[str] = []

    def _on_execute(_conn: object, _cursor: object, statement: str, *_: object) -> None:
        if statement.startswith("UPDATE") and "observed_state=" in statement:
            journal.append("write")

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
    # A frozen clock stamps the next report no later than the row's observed_at.
    stale = ServersServerStateSink(
        create_session_factory(world.engine),
        clock=FakeClock(_NOW),
        real_time_events=world.bus,
    )
    watching = world.watch(server)

    applied = await stale.record_observed_state(
        server_id=str(server.id.value), worker_id=str(worker), state="stopped"
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
    )

    assert [event.payload for event in await _received(watching)] == [
        _status("unknown", detail="cannot confirm")
    ]


# --- The Worker disconnect: the bulk unknown write ---------------------------


async def test_worker_disconnect_publishes_unknown_for_each_changed_server(
    world: _World,
) -> None:
    worker, other_worker = uuid.uuid4(), uuid.uuid4()
    running = await world.server("running", worker=worker, reported="running")
    crashed = await world.server("crashed", worker=worker, reported="crashed")
    await world.server("unknown", worker=worker, reported="unknown")
    await world.server("elsewhere", worker=other_worker, reported="running")
    watching = world.watch_all()

    await world.sink().mark_worker_servers_unknown(worker_id=str(worker))

    # One frame per server the disconnect changed: none for the row already
    # unknown, none for the other worker's server.
    assert sorted(await _states(watching), key=str) == sorted(
        [
            (str(running.id.value), "unknown"),
            (str(crashed.id.value), "unknown"),
        ],
        key=str,
    )
    # All of them after the one commit: no transaction is open across a publish.
    assert world.journal == ["write", "commit", "publish", "publish"]


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
    watching = world.watch(server)

    await world.start_server(
        FakeControlPlane(place_to=WorkerId(uuid.uuid4()), outcomes=_ALREADY_RUNNING)
    )(community_id=world.community, server_id=server.id)

    assert [event.payload for event in await _received(watching)] == [
        _status("running")
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
    """StopServer's confirmed-stop write, when the Worker's own report is lost."""

    worker = uuid.uuid4()
    server = await world.server("survival", worker=worker, reported="running")
    watching = world.watch(server)

    await world.stop_server(FakeControlPlane())(
        community_id=world.community, server_id=server.id
    )

    assert [event.payload for event in await _received(watching)] == [
        _status("stopped")
    ]
    world.assert_published_after_commit()


async def test_confirmed_stop_after_the_workers_stopped_report_publishes_nothing(
    world: _World,
) -> None:
    """The ordinary stop: the Worker's ``stopped`` frame already announced it.

    The confirmed-stop write then changes no state, so it repeats no frame.
    """

    worker = uuid.uuid4()
    server = await world.server("survival", worker=worker, reported="stopped")
    watching = world.watch(server)

    await world.stop_server(FakeControlPlane())(
        community_id=world.community, server_id=server.id
    )

    assert await _received(watching) == []


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
        _status("stopped")
    ]
    world.assert_published_after_commit()


async def test_stop_of_a_crashed_instance_leaves_the_crash_and_publishes_nothing(
    world: _World,
) -> None:
    """The same release keeps ``crashed`` standing: nothing changed, no frame."""

    worker = uuid.uuid4()
    server = await world.server("survival", worker=worker, reported="crashed")
    watching = world.watch(server)

    await world.stop_server(
        FakeControlPlane(
            outcomes={"stop": CommandOutcome(status=CommandStatus.SERVER_NOT_FOUND)}
        )
    )(community_id=world.community, server_id=server.id)

    assert await _received(watching) == []
    assert await world.observed(server) is ObservedState.CRASHED


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
    ).clear_stale_assignment(community_id=world.community, server_id=server.id)

    assert [event.payload for event in await _received(watching)] == [
        _status("unknown")
    ]
    assert await world.observed(server) is ObservedState.UNKNOWN
    world.assert_published_after_commit()


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
