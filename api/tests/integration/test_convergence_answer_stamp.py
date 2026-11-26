"""A convergence write is stamped with the instant the Worker's answer arrived.

Issue #4117: a write that converges observed state from a Worker's answer, such
as ``running`` after a start refused ``INVALID_STATE``, was stamped as it was
made. A report read after the answer but written before that stamp was taken
then lost to it (issue #4118 stamps reports as they arrive), and a disconnect
during the work between the answer and the write was overwritten by it. The
write is now stamped as the answer was read, so the monotonic guard orders it
against the reports and the teardown by when the API learned each.

Runs only when ``MCD_TEST_DATABASE_URL`` is set; skipped otherwise (TESTING.md
Section 5). The start goes through the real control plane and stream to a test
Worker, and everything is written to the real database. The servicer, the
registry, the lifecycle and the sink share the world's clock.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid
from typing import Any

import pytest
from grpc import aio

from mc_server_dashboard_api.fleet.adapters.control_plane import (
    ControlPlaneState,
    GrpcControlPlane,
)
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.servers.adapters.control_plane import (
    FleetControlPlaneAdapter,
)
from mc_server_dashboard_api.servers.adapters.server_state_sink import (
    ServersServerStateSink,
)
from mc_server_dashboard_api.servers.application.lifecycle import (
    StartServer,
    StopServer,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.value_objects import (
    ObservedState,
    WorkerId,
)
from mcsd.controlplane.v1 import control_plane_pb2 as pb
from tests.fleet.fakes import FakeServerStateSink
from tests.fleet.test_grpc_session import (
    _CREDENTIAL,
    _auth,
    _Harness,
    _heartbeat_message,
    _register_message,
    _status_message,
    _wait_until,
)
from tests.integration.test_observed_state_status_events import (  # noqa: F401
    _World,
    world,
)
from tests.servers.fakes import (
    FakeFileStore,
    FakeJarProvisioner,
    FakeStoreGenerationReader,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_WORKER = uuid.UUID("22222222-2222-2222-2222-222222222222")
_OTHER_SERVER = "99999999-9999-9999-9999-999999999999"
_ANSWER_GUARD_SECONDS = 10.0
# Long enough that no session's watchdog polls during a test.
_SESSION_TIMEOUT = dt.timedelta(hours=1)


class _Sink(FakeServerStateSink):
    """Writes through the real sink. A report of ``gated`` waits for ``release``,
    which holds the processor, and so every message queued behind it."""

    def __init__(self, real: ServersServerStateSink) -> None:
        super().__init__()
        self._real = real
        self.gated: str | None = None
        self.holding = asyncio.Event()
        self.release = asyncio.Event()
        self.applied: list[tuple[str, bool]] = []
        self.marked = asyncio.Event()

    async def running_assignment_ids(self, *, worker_id: str) -> dict[str, int]:
        return await self._real.running_assignment_ids(worker_id=worker_id)

    async def record_observed_state(
        self, *, server_id: str, worker_id: str, state: str, **frame: Any
    ) -> bool:
        if server_id == self.gated:
            self.holding.set()
            await self.release.wait()
        applied = await self._real.record_observed_state(
            server_id=server_id, worker_id=worker_id, state=state, **frame
        )
        self.applied.append((server_id, applied))
        return applied

    async def mark_worker_servers_unknown(self, *, worker_id: str) -> None:
        await self._real.mark_worker_servers_unknown(worker_id=worker_id)
        self.marked.set()


class _Stack:
    """The real servicer, control plane and lifecycle over the world's database."""

    def __init__(self, world: _World) -> None:  # noqa: F811
        clock = world.fleet_clock()
        self.registry = InMemoryWorkerRegistry(
            clock=clock, heartbeat_timeout=_SESSION_TIMEOUT
        )
        state = ControlPlaneState()
        self.sink = _Sink(world.sink())
        self.harness = _Harness(
            self.registry,
            clock,
            heartbeat_timeout=_SESSION_TIMEOUT,
            state_sink=self.sink,
            control_plane=state,
        )
        control_plane = FleetControlPlaneAdapter(
            registry=self.registry,
            control_plane=GrpcControlPlane(
                state, clock=clock, timeout_seconds=_ANSWER_GUARD_SECONDS
            ),
            data_plane_base_url="http://api.invalid",
            worker_credential=_CREDENTIAL,
            hydrate_timeout_seconds=_ANSWER_GUARD_SECONDS,
            snapshot_timeout_seconds=_ANSWER_GUARD_SECONDS,
            stop_timeout_seconds=_ANSWER_GUARD_SECONDS,
        )
        self.start = StartServer(
            uow=world.uow(),
            control_plane=control_plane,
            clock=world.clock,
            jar_provisioner=FakeJarProvisioner(),
            store_generation=FakeStoreGenerationReader(),
            file_store=FakeFileStore(seed_eula=True),
        )
        self.stop_server = StopServer(
            uow=world.uow(), control_plane=control_plane, clock=world.clock
        )

    async def register(
        self, held: dict[str, int] | None = None
    ) -> aio.StreamStreamCall:
        stub = await self.harness.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message(held_servers=held))
        await call.read()  # ack
        return call

    async def stop(self) -> None:
        self.sink.release.set()
        await self.harness.stop()
        if self.harness.servicer is not None:
            await self.harness.servicer.drain_teardowns()


def _redispatch(stack: _Stack, server: Server) -> asyncio.Task[Server]:
    return asyncio.create_task(
        stack.start.redispatch_start(
            community_id=server.community_id, server_id=server.id
        )
    )


async def _answer(
    call: aio.StreamStreamCall,
    command: str,
    error: pb.CommandErrorCode.ValueType | None = None,
) -> None:
    """Read the next command, ``command``, and answer it: ``error``, or success."""

    message: pb.ApiMessage = await asyncio.wait_for(
        call.read(), timeout=_ANSWER_GUARD_SECONDS
    )
    assert message.api_command.WhichOneof("command") == command
    result = pb.CommandResult(success=True)
    if error is not None:
        result = pb.CommandResult(success=False, error=pb.CommandError(code=error))
    await call.write(
        pb.WorkerMessage(correlation_id=message.correlation_id, command_result=result)
    )


async def _hold_the_processor(stack: _Stack, call: aio.StreamStreamCall) -> None:
    """Hold the session's processor on a slow write of another server's report."""

    stack.sink.gated = _OTHER_SERVER
    await call.write(_status_message(_OTHER_SERVER, pb.SERVER_STATE_RUNNING))
    await asyncio.wait_for(stack.sink.holding.wait(), timeout=_ANSWER_GUARD_SECONDS)


async def _report_behind_the_answer(
    stack: _Stack,
    call: aio.StreamStreamCall,
    server_id: str,
    state: pb.ServerState.ValueType,
) -> None:
    """Send ``state`` behind the answer, then free the processor once it is read."""

    await call.write(_status_message(server_id, state))
    beat = stack.registry.list_workers()[0].last_heartbeat_at
    await call.write(_heartbeat_message())
    await _wait_until(
        lambda: stack.registry.list_workers()[0].last_heartbeat_at != beat,
        "the report was not read",
    )
    stack.sink.release.set()


async def test_a_report_read_after_a_refused_start_outlives_its_convergence(
    world: _World,  # noqa: F811
) -> None:
    # The Worker refuses the start, as its instance already runs, and that
    # instance crashes at once. Both messages wait behind a slow write, so the
    # processor takes the crash report before the start's convergence is
    # written. The report was read after the answer, so the row ends crashed.
    server = await world.server("survival", worker=_WORKER)
    server_id = str(server.id.value)
    stack = _Stack(world)
    try:
        # Held at the store's generation, so the start skips the hydrate.
        call = await stack.register(held={server_id: 0})
        await _hold_the_processor(stack, call)
        start = _redispatch(stack, server)

        await _answer(call, "start", pb.COMMAND_ERROR_CODE_INVALID_STATE)
        await _report_behind_the_answer(stack, call, server_id, pb.SERVER_STATE_CRASHED)
        await asyncio.wait_for(start, timeout=_ANSWER_GUARD_SECONDS)
        await _wait_until(
            lambda: [s for s, _ in stack.sink.applied] == [_OTHER_SERVER, server_id],
            "the crash report did not land",
        )
        observed = await world.observed(server)
        await call.done_writing()
    finally:
        await stack.stop()

    assert observed is ObservedState.CRASHED


async def test_a_refused_hydrates_convergence_does_not_outlive_the_disconnect(
    world: _World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Issue #4117: the Worker refuses the hydrate, as its instance already
    # runs, and the session ends while the start puts its hydrate mark back.
    # The convergence to running then lands after the teardown's unknown. The
    # answer was read before the session ended, so the row ends unknown.
    server = await world.server("survival", worker=_WORKER)
    unmarking, unmark = asyncio.Event(), asyncio.Event()
    real_unmark = StartServer._unmark

    async def _held_unmark(
        start: StartServer, marked: Server, worker_id: WorkerId, count: int
    ) -> None:
        unmarking.set()
        await unmark.wait()
        await real_unmark(start, marked, worker_id, count)

    monkeypatch.setattr(StartServer, "_unmark", _held_unmark)
    stack = _Stack(world)
    try:
        # Nothing held, so the start hydrates first.
        call = await stack.register()
        start = _redispatch(stack, server)

        await _answer(call, "hydrate", pb.COMMAND_ERROR_CODE_INVALID_STATE)
        await asyncio.wait_for(unmarking.wait(), timeout=_ANSWER_GUARD_SECONDS)
        await call.done_writing()
        await asyncio.wait_for(stack.sink.marked.wait(), timeout=_ANSWER_GUARD_SECONDS)
        unmark.set()
        await asyncio.wait_for(start, timeout=_ANSWER_GUARD_SECONDS)
        observed = await world.observed(server)
    finally:
        unmark.set()
        await stack.stop()

    assert observed is ObservedState.UNKNOWN


@pytest.mark.parametrize("answer", ["stopped", "not_found"])
async def test_a_report_read_after_a_stops_answer_outlives_its_convergence(
    world: _World,  # noqa: F811
    answer: str,
) -> None:
    # The same interleave on a stop: the Worker confirms it, or finds no
    # instance, and then reports the next state of the server, a start queued
    # behind the stop, before the stop's convergence to stopped is written.
    # The report was read after the answer, so the row ends starting.
    server = await world.server("survival", worker=_WORKER)
    server_id = str(server.id.value)
    stack = _Stack(world)
    try:
        call = await stack.register()
        await _hold_the_processor(stack, call)
        stop = asyncio.create_task(
            stack.stop_server(community_id=server.community_id, server_id=server.id)
        )

        error = None
        if answer == "not_found":
            error = pb.COMMAND_ERROR_CODE_SERVER_NOT_FOUND
        await _answer(call, "stop", error)
        await _report_behind_the_answer(
            stack, call, server_id, pb.SERVER_STATE_STARTING
        )
        # The final snapshot is sent once the convergence has committed.
        snapshot: pb.ApiMessage = await asyncio.wait_for(
            call.read(), timeout=_ANSWER_GUARD_SECONDS
        )
        assert snapshot.api_command.WhichOneof("command") == "snapshot"
        await _wait_until(
            lambda: [s for s, _ in stack.sink.applied] == [_OTHER_SERVER, server_id],
            "the report did not land",
        )
        observed = await world.observed(server)
        await call.write(_command_result(snapshot.correlation_id))
        await asyncio.wait_for(stop, timeout=_ANSWER_GUARD_SECONDS)
        await call.done_writing()
    finally:
        await stack.stop()

    assert observed is ObservedState.STARTING


def _command_result(command_id: str) -> pb.WorkerMessage:
    return pb.WorkerMessage(
        correlation_id=command_id, command_result=pb.CommandResult(success=True)
    )
