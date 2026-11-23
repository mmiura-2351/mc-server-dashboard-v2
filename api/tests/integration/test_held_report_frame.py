"""A report whose COMMIT answers late, or not at all, still reaches subscribers.

Review r1 of #4049 (issue #4042): the deadline used to cut the first attempt. A
COMMIT the server had already applied but not yet answered then published no
frame, and the retry, finding the row already at the reported state, staged none
either. The servicer now starts the hold at the deadline and awaits the same
attempt.

A COMMIT whose answer is lost with its connection fails the attempt as an
outage although it applied (issue #4062). The held retry then finds the row
already at the reported state, or already released, or holding the refusal it
records (issue #4104), and still publishes the frame the lost attempt would have
published.

Runs only when ``MCD_TEST_DATABASE_URL`` is set; skipped otherwise (TESTING.md
Section 5). The real sinks write through a proxy that holds back the COMMIT's
answer past the deadline, or drops it with the connection; the report comes over
the real control-plane stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from mc_server_dashboard_api.core.adapters.database import (
    create_engine,
    create_session_factory,
)
from mc_server_dashboard_api.fleet.adapters import grpc_server
from mc_server_dashboard_api.fleet.adapters.control_plane import ControlPlaneState
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.domain.control_plane import CommandResultCode
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId
from mc_server_dashboard_api.servers.adapters.late_snapshot_result_sink import (
    ServersLateSnapshotResultSink,
)
from mc_server_dashboard_api.servers.adapters.server_state_sink import (
    ServersServerStateSink,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork,
)
from mc_server_dashboard_api.servers.application.lifecycle import StopServer
from mc_server_dashboard_api.servers.application.stop_convergence import (
    StopConvergenceGuard,
)
from mc_server_dashboard_api.servers.application.unannounced_refusals import (
    UnannouncedRefusals,
)
from mc_server_dashboard_api.servers.application.unrecorded_stops import (
    UnrecordedStops,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
)
from mc_server_dashboard_api.servers.domain.errors import FinalSnapshotRefusedError
from mc_server_dashboard_api.servers.domain.value_objects import (
    FinalSnapshotRefusal,
    ObservedState,
)
from mcsd.controlplane.v1 import control_plane_pb2 as pb
from tests.fleet.fakes import FakeClock, FakeDatabasePing
from tests.fleet.test_grpc_session import (
    _GRPC_SERVER_LOGGER,
    _HOLD_APPLIED,
    _HOLD_STARTED,
    _T0,
    _TIMEOUT,
    _WORKER_ID,
    _command_result_message,
    _Harness,
    _logged,
    _RealWriteSink,
    _registered,
    _status_message,
    _wait_until,
    fast_hold,  # noqa: F401
)
from tests.integration.test_lifecycle_scenarios import _load
from tests.integration.test_observed_state_status_events import (  # noqa: F401
    _RELEASED,
    _received,
    _status,
    _wedged_stop,
    _World,
    world,
)
from tests.servers.fakes import FakeControlPlane

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_DEADLINE_SECONDS = 0.2
# Past the deadline, and well inside the statement bound below.
_LATE_SECONDS = 0.8


class _LateAnswerProxy:
    """Forwards connections to the database, holding back what the server sends
    until ``answer_at`` (a ``time.monotonic`` reading) while that is ahead."""

    def __init__(self, host: str, port: int) -> None:
        self.answer_at = 0.0
        self._target = (host, port)
        self.writers: list[asyncio.StreamWriter] = []

    async def _pump(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        to_client: bool,
    ) -> None:
        try:
            while data := await reader.read(65536):
                if to_client and (wait := self.answer_at - time.monotonic()) > 0:
                    await asyncio.sleep(wait)
                writer.write(data)
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def serve(
        self, client: asyncio.StreamReader, to_client: asyncio.StreamWriter
    ) -> None:
        upstream, to_upstream = await asyncio.open_connection(*self._target)
        self.writers += [to_client, to_upstream]
        await asyncio.gather(
            self._pump(client, to_upstream, to_client=False),
            self._pump(upstream, to_client, to_client=True),
        )


async def test_a_commit_answered_after_the_deadline_still_reaches_subscribers(
    world: _World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=_GRPC_SERVER_LOGGER)
    monkeypatch.setattr(
        grpc_server, "_FIRST_ATTEMPT_TIMEOUT_SECONDS", _DEADLINE_SECONDS
    )
    assert _DB_URL is not None
    server = await world.server("survival", worker=uuid.UUID(_WORKER_ID))
    target = make_url(_DB_URL)
    assert target.host is not None and target.port is not None
    proxy = _LateAnswerProxy(target.host, target.port)
    listener = await asyncio.start_server(proxy.serve, "127.0.0.1", 0)
    url = target.set(host="127.0.0.1", port=listener.sockets[0].getsockname()[1])
    engine = create_engine(
        url.render_as_string(hide_password=False), statement_timeout=10.0
    )

    def _answer_late(_conn: object) -> None:
        # Fires just before the COMMIT is sent: the server applies it at once,
        # and its answer comes back only after the deadline.
        proxy.answer_at = time.monotonic() + _LATE_SECONDS

    sa_event.listen(engine.sync_engine, "commit", _answer_late)
    sink = ServersServerStateSink(
        create_session_factory(engine), clock=world.clock, real_time_events=world.bus
    )
    watching = world.watch(server)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        world.fleet_clock(),
        state_sink=_RealWriteSink(sink),
        database_ping=FakeDatabasePing(reachable=True),
    )
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(str(server.id.value), pb.SERVER_STATE_RUNNING))
        await _wait_until(
            lambda: _logged(caplog, _HOLD_APPLIED), "the report was not applied"
        )
        frames = [event.payload for event in await _received(watching)]
        await call.done_writing()
    finally:
        await h.stop()
        if h.servicer is not None:
            await h.servicer.drain_teardowns()
        await engine.dispose()
        listener.close()
        for writer in proxy.writers:
            writer.close()

    assert _logged(caplog, _HOLD_STARTED) != []
    assert (await world.observed(server)).value == "running"
    assert frames == [{"state": "running", "detail": "", "reason": ""}]


# --- A retry after a COMMIT whose answer was lost (issue #4062) ---------------

_LIFECYCLE_LOGGER = "mc_server_dashboard_api.servers.application.lifecycle"


class _LostAnswerProxy:
    """Forwards connections to the database. Once ``armed``, it drops the next
    answer the server sends, and closes that connection instead."""

    def __init__(self, host: str, port: int) -> None:
        self.armed = False
        self.cuts = 0
        self._target = (host, port)
        self.writers: list[asyncio.StreamWriter] = []

    async def _pump(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        to_client: bool,
    ) -> None:
        try:
            while data := await reader.read(65536):
                if to_client and self.armed:
                    self.armed = False
                    self.cuts += 1
                    return
                writer.write(data)
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def serve(
        self, client: asyncio.StreamReader, to_client: asyncio.StreamWriter
    ) -> None:
        upstream, to_upstream = await asyncio.open_connection(*self._target)
        self.writers += [to_client, to_upstream]
        await asyncio.gather(
            self._pump(client, to_upstream, to_client=False),
            self._pump(upstream, to_client, to_client=True),
        )


@contextlib.asynccontextmanager
async def _first_commit_answer_lost() -> AsyncIterator[
    tuple[_LostAnswerProxy, AsyncEngine]
]:
    """An engine whose first COMMIT applies, and whose answer is lost with its
    connection: the attempt fails as an outage although its write landed."""

    assert _DB_URL is not None
    target = make_url(_DB_URL)
    assert target.host is not None and target.port is not None
    proxy = _LostAnswerProxy(target.host, target.port)
    listener = await asyncio.start_server(proxy.serve, "127.0.0.1", 0)
    url = target.set(host="127.0.0.1", port=listener.sockets[0].getsockname()[1])
    engine = create_engine(
        url.render_as_string(hide_password=False), statement_timeout=10.0
    )

    def _lose_the_answer(_conn: object) -> None:
        # Fires just before the COMMIT is sent: the server applies it, and its
        # answer is the next thing it sends.
        if not proxy.cuts:
            proxy.armed = True

    sa_event.listen(engine.sync_engine, "commit", _lose_the_answer)
    try:
        yield proxy, engine
    finally:
        await engine.dispose()
        listener.close()
        for writer in proxy.writers:
            writer.close()


def _applied_attempts(caplog: pytest.LogCaptureFixture) -> list[object]:
    return [record.__dict__["attempts"] for record in _logged(caplog, _HOLD_APPLIED)]


def _late_releases(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _LIFECYCLE_LOGGER and r.getMessage().startswith("released worker ")
    ]


@pytest.mark.usefixtures("fast_hold")
async def test_a_report_whose_commit_answer_was_lost_reaches_subscribers(
    world: _World,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=_GRPC_SERVER_LOGGER)
    server = await world.server("survival", worker=uuid.UUID(_WORKER_ID))
    watching = world.watch(server)
    async with _first_commit_answer_lost() as (proxy, engine):
        sink = ServersServerStateSink(
            create_session_factory(engine),
            clock=world.clock,
            real_time_events=world.bus,
        )
        h = _Harness(
            InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
            world.fleet_clock(),
            state_sink=_RealWriteSink(sink),
            database_ping=FakeDatabasePing(reachable=True),
        )
        try:
            call = await _registered(await h.start())
            await call.write(
                _status_message(str(server.id.value), pb.SERVER_STATE_RUNNING)
            )
            await _wait_until(
                lambda: _logged(caplog, _HOLD_APPLIED), "the report was not applied"
            )
            frames = [event.payload for event in await _received(watching)]
            await call.done_writing()
        finally:
            await h.stop()

    assert proxy.cuts == 1
    assert _logged(caplog, _HOLD_STARTED) != []
    assert _applied_attempts(caplog) == [2]
    assert (await world.observed(server)) is ObservedState.RUNNING
    assert frames == [_status("running")]


async def test_a_repeated_report_through_the_servicer_publishes_nothing(
    world: _World,  # noqa: F811
) -> None:
    # A first attempt is no retry: a report that finds the row already at its
    # state stages no frame, and only the next, which changes it, is published.
    worker = uuid.UUID(_WORKER_ID)
    server = await world.server("survival", worker=worker, reported="running")
    watching = world.watch(server)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        world.fleet_clock(),
        state_sink=_RealWriteSink(world.sink()),
    )
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(str(server.id.value), pb.SERVER_STATE_RUNNING))
        await call.write(
            _status_message(str(server.id.value), pb.SERVER_STATE_STOPPING)
        )
        for _ in range(500):
            if await world.observed(server) is ObservedState.STOPPING:
                break
            await asyncio.sleep(0.01)
        frames = [event.payload for event in await _received(watching)]
        await call.done_writing()
    finally:
        await h.stop()

    assert frames == [_status("stopping")]


@pytest.mark.usefixtures("fast_hold")
async def test_a_late_snapshot_release_whose_commit_answer_was_lost_is_announced(
    world: _World,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=_GRPC_SERVER_LOGGER)
    caplog.set_level(logging.INFO, logger=_LIFECYCLE_LOGGER)
    server = await _wedged_stop(world, uuid.UUID(_WORKER_ID), "stopped")
    watching = world.watch(server)
    async with _first_commit_answer_lost() as (proxy, engine):
        state = ControlPlaneState(
            late_snapshot_sink=ServersLateSnapshotResultSink(
                create_session_factory(engine),
                control_plane=FakeControlPlane(),
                clock=world.clock,
                stop_convergence=StopConvergenceGuard(),
                unrecorded_stops=UnrecordedStops(),
                unannounced_refusals=UnannouncedRefusals(),
                real_time_events=world.bus,
            )
        )
        h = _Harness(
            InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
            FakeClock(_T0),
            control_plane=state,
            database_ping=FakeDatabasePing(reachable=True),
        )
        try:
            call = await _registered(await h.start())
            # An abandoned final snapshot of this session: its record is made
            # after the registration, whose open_session drops earlier ones.
            state.register_pending(
                "snapshot-1",
                WorkerId(_WORKER_ID),
                snapshot_server_id=str(server.id.value),
            )
            state.discard_pending("snapshot-1")
            await call.write(_command_result_message("snapshot-1"))
            await _wait_until(
                lambda: _logged(caplog, _HOLD_APPLIED), "the result was not applied"
            )
            frames = [event.payload for event in await _received(watching)]
            await call.done_writing()
        finally:
            await h.stop()

    row = await _load(world.engine, server.id)
    assert row is not None and row.assigned_worker_id is None
    assert proxy.cuts == 1
    assert _applied_attempts(caplog) == [2]
    assert frames == [_RELEASED]
    released = _late_releases(caplog)
    assert len(released) == 1
    # The retry found the release made, and its line says so.
    assert "held retry" in released[0]


async def test_a_retry_that_changes_the_state_publishes_one_frame(
    world: _World,  # noqa: F811
) -> None:
    # The attempt a retry repeats did not land: the retry's write changes the
    # row, and stages its one frame, not a second for being a retry.
    worker = uuid.UUID(_WORKER_ID)
    server = await world.server("survival", worker=worker, reported="starting")
    watching = world.watch(server)

    assert await world.sink().record_observed_state(
        server_id=str(server.id.value),
        worker_id=_WORKER_ID,
        state="running",
        retry=True,
        observed_at=world.clock.now(),
    )

    assert [event.payload for event in await _received(watching)] == [
        _status("running")
    ]


async def test_a_retry_that_makes_the_release_publishes_one_frame(
    world: _World,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The release a retry repeats did not land: the retry makes it, and stages
    # its one frame. It logs the release as made now.
    caplog.set_level(logging.INFO, logger=_LIFECYCLE_LOGGER)
    worker = uuid.UUID(_WORKER_ID)
    server = await _wedged_stop(world, worker, "stopped")
    sink = ServersLateSnapshotResultSink(
        create_session_factory(world.engine),
        control_plane=FakeControlPlane(),
        clock=world.clock,
        stop_convergence=StopConvergenceGuard(),
        unrecorded_stops=UnrecordedStops(),
        unannounced_refusals=UnannouncedRefusals(),
        real_time_events=world.bus,
    )
    watching = world.watch(server)

    await sink.clear_held_assignment_on_late_snapshot(
        server_id=str(server.id.value),
        worker_id=_WORKER_ID,
        code=CommandResultCode.OK,
        message=None,
        received_at=world.clock.now(),
        retry=True,
    )

    assert [event.payload for event in await _received(watching)] == [_RELEASED]
    released = _late_releases(caplog)
    assert len(released) == 1
    assert "held retry" not in released[0]


async def test_a_retry_leaves_a_row_another_worker_holds(
    world: _World,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A retry also takes an unassigned row as released, and no other. This
    # Worker's retried late result finds the server held at (stopped, stopped)
    # by another Worker: that Worker keeps the row, and nothing is published or
    # logged.
    caplog.set_level(logging.INFO, logger=_LIFECYCLE_LOGGER)
    holder = uuid.uuid4()
    server = await _wedged_stop(world, holder, "stopped")
    sink = ServersLateSnapshotResultSink(
        create_session_factory(world.engine),
        control_plane=FakeControlPlane(),
        clock=world.clock,
        stop_convergence=StopConvergenceGuard(),
        unrecorded_stops=UnrecordedStops(),
        unannounced_refusals=UnannouncedRefusals(),
        real_time_events=world.bus,
    )
    watching = world.watch(server)

    await sink.clear_held_assignment_on_late_snapshot(
        server_id=str(server.id.value),
        worker_id=_WORKER_ID,
        code=CommandResultCode.TRANSFER_FAILED,
        message="upload aborted by transfer bound",
        received_at=world.clock.now(),
        retry=True,
    )

    row = await _load(world.engine, server.id)
    assert row is not None and row.assigned_worker_id is not None
    assert row.assigned_worker_id.value == holder
    assert await _received(watching) == []
    assert _late_releases(caplog) == []


# --- A retry of a late refusal after a lost COMMIT answer (issue #4104) -------

_BUSY_REFUSAL = {"final_snapshot_refusal": "busy"}


def _late_sink(world: _World) -> ServersLateSnapshotResultSink:  # noqa: F811
    return ServersLateSnapshotResultSink(
        create_session_factory(world.engine),
        control_plane=FakeControlPlane(),
        clock=world.clock,
        stop_convergence=StopConvergenceGuard(),
        unrecorded_stops=UnrecordedStops(),
        unannounced_refusals=UnannouncedRefusals(),
        real_time_events=world.bus,
    )


async def _late_refusal(
    sink: ServersLateSnapshotResultSink, server_id: str, *, retry: bool
) -> None:
    await sink.clear_held_assignment_on_late_snapshot(
        server_id=server_id,
        worker_id=_WORKER_ID,
        code=CommandResultCode.BUSY,
        message="a leftover container may still run",
        received_at=_T0,
        retry=retry,
    )


@pytest.mark.usefixtures("fast_hold")
async def test_a_late_refusal_whose_commit_answer_was_lost_is_announced(
    world: _World,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The first attempt recorded the refusal, and its answer was lost. The held
    # retry finds the refusal recorded and still announces it, so the live page
    # shows the hold without a reload.
    caplog.set_level(logging.INFO, logger=_GRPC_SERVER_LOGGER)
    server = await _wedged_stop(world, uuid.UUID(_WORKER_ID), "stopped")
    watching = world.watch(server)
    async with _first_commit_answer_lost() as (proxy, engine):
        state = ControlPlaneState(
            late_snapshot_sink=ServersLateSnapshotResultSink(
                create_session_factory(engine),
                control_plane=FakeControlPlane(),
                clock=world.clock,
                stop_convergence=StopConvergenceGuard(),
                unrecorded_stops=UnrecordedStops(),
                unannounced_refusals=UnannouncedRefusals(),
                real_time_events=world.bus,
            )
        )
        h = _Harness(
            InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
            FakeClock(_T0),
            control_plane=state,
            database_ping=FakeDatabasePing(reachable=True),
        )
        try:
            call = await _registered(await h.start())
            # An abandoned final snapshot of this session, as above.
            state.register_pending(
                "snapshot-1",
                WorkerId(_WORKER_ID),
                snapshot_server_id=str(server.id.value),
            )
            state.discard_pending("snapshot-1")
            await call.write(
                pb.WorkerMessage(
                    correlation_id="snapshot-1",
                    command_result=pb.CommandResult(
                        success=False,
                        error=pb.CommandError(code=pb.COMMAND_ERROR_CODE_BUSY),
                    ),
                )
            )
            await _wait_until(
                lambda: _logged(caplog, _HOLD_APPLIED), "the result was not applied"
            )
            frames = [event.payload for event in await _received(watching)]
            await call.done_writing()
        finally:
            await h.stop()

    row = await _load(world.engine, server.id)
    assert row is not None and row.assigned_worker_id is not None
    assert row.assigned_worker_id.value == uuid.UUID(_WORKER_ID)
    assert row.final_snapshot_refusal is FinalSnapshotRefusal.BUSY
    assert proxy.cuts == 1
    assert _applied_attempts(caplog) == [2]
    assert frames == [_BUSY_REFUSAL]


async def test_a_first_late_refusal_the_row_already_holds_publishes_nothing(
    world: _World,  # noqa: F811
) -> None:
    # A first attempt is no retry: a late refusal that finds the same refusal
    # recorded changes nothing and stages no frame.
    server = await _wedged_stop(world, uuid.UUID(_WORKER_ID), "stopped")
    sink = _late_sink(world)
    await _late_refusal(sink, str(server.id.value), retry=False)
    watching = world.watch(server)

    await _late_refusal(sink, str(server.id.value), retry=False)

    assert await _received(watching) == []


async def test_a_retry_that_records_the_refusal_publishes_one_frame(
    world: _World,  # noqa: F811
) -> None:
    # The refusal a retry repeats did not land: the retry records it, and
    # stages its one frame, not a second for being a retry.
    server = await _wedged_stop(world, uuid.UUID(_WORKER_ID), "stopped")
    watching = world.watch(server)

    await _late_refusal(_late_sink(world), str(server.id.value), retry=True)

    assert [event.payload for event in await _received(watching)] == [_BUSY_REFUSAL]
    row = await _load(world.engine, server.id)
    assert row is not None
    assert row.final_snapshot_refusal is FinalSnapshotRefusal.BUSY


# --- An in-time refusal after a lost COMMIT answer (issue #4152) --------------


async def test_an_in_time_refusal_whose_commit_answer_was_lost_is_announced_once(
    world: _World,  # noqa: F811
) -> None:
    # The stale-stop arm's snapshot is refused, and the COMMIT of the refusal it
    # records lands with its answer lost: nothing is published. The next
    # refused attempt finds the refusal recorded and announces it all the same,
    # and the one after it publishes nothing.
    server = await _wedged_stop(world, uuid.UUID(_WORKER_ID), "stopped")
    frames: list[list[object]] = []
    async with _first_commit_answer_lost() as (proxy, engine):
        stop = StopServer(
            uow=SqlAlchemyUnitOfWork(create_session_factory(engine), world.bus),
            control_plane=FakeControlPlane(
                outcomes={"snapshot": CommandOutcome(status=CommandStatus.BUSY)}
            ),
            clock=world.clock,
        )
        for attempt in range(3):
            watching = world.watch(server)
            with pytest.raises(FinalSnapshotRefusedError):
                await stop.clear_stale_assignment(
                    community_id=world.community,
                    server_id=server.id,
                    worker_connected=True,
                )
            frames.append([event.payload for event in await _received(watching)])
            if attempt == 0:
                # The cut answer was the refusal's COMMIT, which landed.
                assert proxy.cuts == 1
                row = await _load(world.engine, server.id)
                assert row is not None
                assert row.final_snapshot_refusal is FinalSnapshotRefusal.BUSY

    assert frames == [[], [_BUSY_REFUSAL], []]
    row = await _load(world.engine, server.id)
    assert row is not None and row.assigned_worker_id is not None
    assert row.assigned_worker_id.value == uuid.UUID(_WORKER_ID)
