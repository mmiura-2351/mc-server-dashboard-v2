"""A report whose COMMIT answers late, or not at all, still reaches subscribers.

Review r1 of #4049 (issue #4042): the deadline used to cut the first attempt. A
COMMIT the server had already applied but not yet answered then published no
frame, and the retry, finding the row already at the reported state, staged none
either. The servicer now starts the hold at the deadline and awaits the same
attempt.

A COMMIT whose answer is lost with its connection fails the attempt as an
outage although it applied (issue #4062). The held retry then finds the row
already at the reported state, or already released, and still publishes the
frame the lost attempt would have published.

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
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId
from mc_server_dashboard_api.servers.adapters.late_snapshot_result_sink import (
    ServersLateSnapshotResultSink,
)
from mc_server_dashboard_api.servers.adapters.server_state_sink import (
    ServersServerStateSink,
)
from mc_server_dashboard_api.servers.application.stop_convergence import (
    StopConvergenceGuard,
)
from mc_server_dashboard_api.servers.application.unrecorded_stops import (
    UnrecordedStops,
)
from mc_server_dashboard_api.servers.domain.value_objects import ObservedState
from mcsd.controlplane.v1 import control_plane_pb2 as pb
from tests.fleet.fakes import FakeClock, FakeDatabasePing
from tests.fleet.test_grpc_session import (  # noqa: F401
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
    fast_hold,
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
        FakeClock(_T0),
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
            FakeClock(_T0),
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
        FakeClock(_T0),
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
    released = [
        r
        for r in caplog.records
        if r.name == _LIFECYCLE_LOGGER
        and r.getMessage().startswith(f"released worker {_WORKER_ID} for server")
    ]
    assert len(released) == 1
