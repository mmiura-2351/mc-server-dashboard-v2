"""In-process integration tests for the control-plane command dispatch path.

Starts a real grpc.aio server with the session servicer and a shared
:class:`ControlPlaneState`, dials it with a real client, registers a worker, then
exercises :class:`GrpcControlPlane.dispatch` end to end (CONTROL_PLANE.md
Sections 3, 5):

- a dispatched command rides the worker's outbound stream; the worker answers a
  CommandResult correlated by command_id and dispatch returns the typed result;
- a failure CommandResult maps to the typed failure code, with RCON output
  carried through on success;
- dispatch to a worker with no live session raises WorkerNotConnectedError;
- a command the worker never answers raises CommandTimedOutError.

No Postgres; runs in the unit-runnable suite.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncIterator

import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from grpc import aio

from mc_server_dashboard_api.fleet.adapters.control_plane import (
    ControlPlaneState,
    GrpcControlPlane,
    StaleSessionError,
    _to_result,
)
from mc_server_dashboard_api.fleet.adapters.grpc_server import WorkerSessionServicer
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.domain.control_plane import (
    CommandResultCode,
    CommandTimedOutError,
    EditFileCommand,
    FileAccessReason,
    LaunchMode,
    ListFilesCommand,
    ReadFileCommand,
    ServerCommandCommand,
    SnapshotCommand,
    StartServerCommand,
    TunnelDialCommand,
    WorkerNotConnectedError,
)
from mc_server_dashboard_api.fleet.domain.late_snapshot_sink import (
    LateSnapshotResultSink,
)
from mc_server_dashboard_api.fleet.domain.value_objects import DriverKind, WorkerId
from mcsd.controlplane.v1 import control_plane_pb2 as pb
from mcsd.controlplane.v1.control_plane_pb2_grpc import (
    WorkerServiceStub,
    add_WorkerServiceServicer_to_server,
)
from tests.fleet.fakes import (
    FakeClock,
    FakeServerStateSink,
    RecordingRealTimeEvents,
)

_T0 = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_TIMEOUT = dt.timedelta(seconds=30)
_TRANSFER_DEADLINE = dt.timedelta(seconds=660)
_CREDENTIAL = "shared-worker-secret"
# Registers through the real gRPC servicer, which requires a UUID worker id
# (issue #99); the fleet WorkerId value object itself stays free-form.
_WORKER = "22222222-2222-2222-2222-222222222222"


def _register_message() -> pb.WorkerMessage:
    caps = pb.WorkerCapabilities(drivers=[pb.EXECUTION_DRIVER_KIND_CONTAINER])
    return pb.WorkerMessage(
        correlation_id="reg-1",
        register=pb.Register(
            worker_id=_WORKER, worker_version="1.0.0", capabilities=caps
        ),
    )


class _Harness:
    def __init__(self, *, command_timeout: float = 5.0) -> None:
        self.state = ControlPlaneState()
        self.clock = FakeClock(_T0)
        self.control_plane = GrpcControlPlane(
            self.state, clock=self.clock, timeout_seconds=command_timeout
        )
        self.registry = InMemoryWorkerRegistry(
            clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT
        )
        self._server: aio.Server | None = None
        self._channel: aio.Channel | None = None

    async def start(self) -> WorkerServiceStub:
        server = aio.server()
        servicer = WorkerSessionServicer(
            registry=self.registry,
            clock=FakeClock(_T0),
            worker_credential=_CREDENTIAL,
            heartbeat_timeout=_TIMEOUT,
            transfer_deadline=_TRANSFER_DEADLINE,
            control_plane=self.state,
            state_sink=FakeServerStateSink(),
            real_time_events=RecordingRealTimeEvents(),
        )
        add_WorkerServiceServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        self._server = server
        self._channel = aio.insecure_channel(f"127.0.0.1:{port}")
        return WorkerServiceStub(self._channel)

    async def stop(self) -> None:
        if self._channel is not None:
            await self._channel.close()
        if self._server is not None:
            await self._server.stop(grace=None)


@pytest.fixture
async def harness() -> AsyncIterator[_Harness]:
    h = _Harness()
    try:
        yield h
    finally:
        await h.stop()


def _auth() -> list[tuple[str, str]]:
    return [("authorization", f"Bearer {_CREDENTIAL}")]


async def _registered_call(
    harness: _Harness, stub: WorkerServiceStub
) -> aio.StreamStreamCall:
    call = stub.Session(metadata=_auth())
    await call.write(_register_message())
    await call.read()  # ack
    # Wait until the servicer has registered this worker's outbound session.
    for _ in range(100):
        if harness.state.outbound_for(WorkerId(_WORKER)) is not None:
            return call
        await asyncio.sleep(0.01)
    raise AssertionError("session was not opened in time")


async def test_dispatch_correlates_command_result(harness: _Harness) -> None:
    stub = await harness.start()
    call = await _registered_call(harness, stub)

    async def worker_echo() -> None:
        # Read the pushed command and answer a success result for its command_id.
        msg = await call.read()
        assert msg.WhichOneof("payload") == "api_command"
        await call.write(
            pb.WorkerMessage(
                correlation_id=msg.api_command.command_id,
                command_result=pb.CommandResult(
                    success=True, command_output="players: 3"
                ),
            )
        )

    echo = asyncio.ensure_future(worker_echo())
    result = await harness.control_plane.dispatch(
        worker_id=WorkerId(_WORKER),
        server_id=str(uuid.uuid4()),
        command=ServerCommandCommand(line="list"),
    )
    await echo

    assert result.success
    assert result.output == "players: 3"
    await call.done_writing()


async def test_dispatch_read_file_carries_bytes_back(harness: _Harness) -> None:
    stub = await harness.start()
    call = await _registered_call(harness, stub)

    async def worker_echo() -> None:
        msg = await call.read()
        assert msg.api_command.WhichOneof("command") == "read_file"
        assert msg.api_command.read_file.path == "server.properties"
        await call.write(
            pb.WorkerMessage(
                correlation_id=msg.api_command.command_id,
                command_result=pb.CommandResult(
                    success=True, file_content=b"\x00\x01motd"
                ),
            )
        )

    echo = asyncio.ensure_future(worker_echo())
    result = await harness.control_plane.dispatch(
        worker_id=WorkerId(_WORKER),
        server_id=str(uuid.uuid4()),
        command=ReadFileCommand(path="server.properties"),
    )
    await echo

    assert result.success
    assert result.file_content == b"\x00\x01motd"
    await call.done_writing()


async def test_dispatch_edit_file_carries_content(harness: _Harness) -> None:
    stub = await harness.start()
    call = await _registered_call(harness, stub)

    async def worker_echo() -> None:
        msg = await call.read()
        assert msg.api_command.WhichOneof("command") == "edit_file"
        assert msg.api_command.edit_file.path == "ops.json"
        assert msg.api_command.edit_file.content == b"[]"
        await call.write(
            pb.WorkerMessage(
                correlation_id=msg.api_command.command_id,
                command_result=pb.CommandResult(success=True),
            )
        )

    echo = asyncio.ensure_future(worker_echo())
    result = await harness.control_plane.dispatch(
        worker_id=WorkerId(_WORKER),
        server_id=str(uuid.uuid4()),
        command=EditFileCommand(path="ops.json", content=b"[]"),
    )
    await echo

    assert result.success
    await call.done_writing()


async def test_dispatch_list_files_carries_listing_back(harness: _Harness) -> None:
    stub = await harness.start()
    call = await _registered_call(harness, stub)

    async def worker_echo() -> None:
        msg = await call.read()
        assert msg.api_command.WhichOneof("command") == "list_files"
        assert msg.api_command.list_files.path == "plugins"
        await call.write(
            pb.WorkerMessage(
                correlation_id=msg.api_command.command_id,
                command_result=pb.CommandResult(
                    success=True,
                    file_listing=pb.FileListing(
                        entries=[
                            pb.FileEntry(name="config.yml", is_dir=False, size=128),
                            pb.FileEntry(name="data", is_dir=True, size=0),
                        ],
                        truncated=True,
                    ),
                ),
            )
        )

    echo = asyncio.ensure_future(worker_echo())
    result = await harness.control_plane.dispatch(
        worker_id=WorkerId(_WORKER),
        server_id=str(uuid.uuid4()),
        command=ListFilesCommand(path="plugins"),
    )
    await echo

    assert result.success
    assert result.file_listing is not None
    assert result.file_listing.truncated is True
    assert [(e.name, e.is_dir, e.size) for e in result.file_listing.entries] == [
        ("config.yml", False, 128),
        ("data", True, 0),
    ]
    await call.done_writing()


async def test_dispatch_failure_maps_typed_code(harness: _Harness) -> None:
    stub = await harness.start()
    call = await _registered_call(harness, stub)

    async def worker_reject() -> None:
        msg = await call.read()
        await call.write(
            pb.WorkerMessage(
                correlation_id=msg.api_command.command_id,
                command_result=pb.CommandResult(
                    success=False,
                    error=pb.CommandError(
                        code=pb.COMMAND_ERROR_CODE_INVALID_STATE, message="running"
                    ),
                ),
            )
        )

    rejecter = asyncio.ensure_future(worker_reject())
    result = await harness.control_plane.dispatch(
        worker_id=WorkerId(_WORKER),
        server_id=str(uuid.uuid4()),
        command=StartServerCommand(
            driver=DriverKind.CONTAINER,
            jar_relpath="server.jar",
            minecraft_version="1.21.1",
        ),
    )
    await rejecter

    assert not result.success
    assert result.code is CommandResultCode.INVALID_STATE
    assert result.message == "running"
    await call.done_writing()


@pytest.mark.parametrize(
    ("wire_reason", "domain_reason"),
    [
        (pb.FILE_ACCESS_REASON_UNSPECIFIED, FileAccessReason.UNSPECIFIED),
        (pb.FILE_ACCESS_REASON_IS_A_DIRECTORY, FileAccessReason.IS_A_DIRECTORY),
        (pb.FILE_ACCESS_REASON_NOT_A_DIRECTORY, FileAccessReason.NOT_A_DIRECTORY),
        (pb.FILE_ACCESS_REASON_SYMLINK_REFUSED, FileAccessReason.SYMLINK_REFUSED),
        (
            pb.FILE_ACCESS_REASON_PAYLOAD_TOO_LARGE,
            FileAccessReason.PAYLOAD_TOO_LARGE,
        ),
    ],
)
def test_to_result_carries_file_access_reason(
    wire_reason: "pb.FileAccessReason.ValueType", domain_reason: FileAccessReason
) -> None:
    """The wire file_access_reason maps onto the domain reason (issue #548)."""

    message = pb.CommandResult(
        success=False,
        error=pb.CommandError(
            code=pb.COMMAND_ERROR_CODE_FILE_ACCESS_DENIED,
            message="denied",
            file_access_reason=wire_reason,
        ),
    )
    result = _to_result(message)
    assert result.code is CommandResultCode.FILE_ACCESS_DENIED
    assert result.file_access_reason is domain_reason


async def test_start_carries_forge_launch_mode_on_the_wire(harness: _Harness) -> None:
    stub = await harness.start()
    call = await _registered_call(harness, stub)
    received: list[pb.StartServer] = []

    async def worker_echo() -> None:
        msg = await call.read()
        received.append(msg.api_command.start)
        await call.write(
            pb.WorkerMessage(
                correlation_id=msg.api_command.command_id,
                command_result=pb.CommandResult(success=True),
            )
        )

    echo = asyncio.ensure_future(worker_echo())
    await harness.control_plane.dispatch(
        worker_id=WorkerId(_WORKER),
        server_id=str(uuid.uuid4()),
        command=StartServerCommand(
            driver=DriverKind.CONTAINER,
            jar_relpath="server.jar",
            minecraft_version="1.21.8",
            launch_mode=LaunchMode.FORGE_ARGSFILE,
        ),
    )
    await echo

    assert received[0].launch_mode == pb.LAUNCH_MODE_FORGE_ARGSFILE
    await call.done_writing()


async def test_start_carries_jar_launch_mode_on_the_wire(harness: _Harness) -> None:
    stub = await harness.start()
    call = await _registered_call(harness, stub)
    received: list[pb.StartServer] = []

    async def worker_echo() -> None:
        msg = await call.read()
        received.append(msg.api_command.start)
        await call.write(
            pb.WorkerMessage(
                correlation_id=msg.api_command.command_id,
                command_result=pb.CommandResult(success=True),
            )
        )

    echo = asyncio.ensure_future(worker_echo())
    await harness.control_plane.dispatch(
        worker_id=WorkerId(_WORKER),
        server_id=str(uuid.uuid4()),
        command=StartServerCommand(
            driver=DriverKind.CONTAINER,
            jar_relpath="server.jar",
            minecraft_version="1.21.1",
            launch_mode=LaunchMode.JAR,
        ),
    )
    await echo

    assert received[0].launch_mode == pb.LAUNCH_MODE_JAR
    await call.done_writing()


async def test_start_carries_memory_limit_bytes_on_the_wire(harness: _Harness) -> None:
    stub = await harness.start()
    call = await _registered_call(harness, stub)
    received: list[pb.StartServer] = []

    async def worker_echo() -> None:
        msg = await call.read()
        received.append(msg.api_command.start)
        await call.write(
            pb.WorkerMessage(
                correlation_id=msg.api_command.command_id,
                command_result=pb.CommandResult(success=True),
            )
        )

    echo = asyncio.ensure_future(worker_echo())
    await harness.control_plane.dispatch(
        worker_id=WorkerId(_WORKER),
        server_id=str(uuid.uuid4()),
        command=StartServerCommand(
            driver=DriverKind.CONTAINER,
            jar_relpath="server.jar",
            minecraft_version="1.21.1",
            memory_limit_bytes=2048 * 1024 * 1024,
        ),
    )
    await echo

    assert received[0].memory_limit_bytes == 2048 * 1024 * 1024
    await call.done_writing()


async def test_start_carries_cpu_millis_on_the_wire(harness: _Harness) -> None:
    stub = await harness.start()
    call = await _registered_call(harness, stub)
    received: list[pb.StartServer] = []

    async def worker_echo() -> None:
        msg = await call.read()
        received.append(msg.api_command.start)
        await call.write(
            pb.WorkerMessage(
                correlation_id=msg.api_command.command_id,
                command_result=pb.CommandResult(success=True),
            )
        )

    echo = asyncio.ensure_future(worker_echo())
    await harness.control_plane.dispatch(
        worker_id=WorkerId(_WORKER),
        server_id=str(uuid.uuid4()),
        command=StartServerCommand(
            driver=DriverKind.CONTAINER,
            jar_relpath="server.jar",
            minecraft_version="1.21.1",
            cpu_millis=2000,
        ),
    )
    await echo

    assert received[0].cpu_millis == 2000
    await call.done_writing()


async def test_start_defaults_memory_limit_bytes_to_zero(harness: _Harness) -> None:
    stub = await harness.start()
    call = await _registered_call(harness, stub)
    received: list[pb.StartServer] = []

    async def worker_echo() -> None:
        msg = await call.read()
        received.append(msg.api_command.start)
        await call.write(
            pb.WorkerMessage(
                correlation_id=msg.api_command.command_id,
                command_result=pb.CommandResult(success=True),
            )
        )

    echo = asyncio.ensure_future(worker_echo())
    await harness.control_plane.dispatch(
        worker_id=WorkerId(_WORKER),
        server_id=str(uuid.uuid4()),
        command=StartServerCommand(
            driver=DriverKind.CONTAINER,
            jar_relpath="server.jar",
            minecraft_version="1.21.1",
        ),
    )
    await echo

    assert received[0].memory_limit_bytes == 0
    assert received[0].cpu_millis == 0
    await call.done_writing()


async def test_dispatch_to_unconnected_worker_raises(harness: _Harness) -> None:
    await harness.start()
    with pytest.raises(WorkerNotConnectedError):
        await harness.control_plane.dispatch(
            worker_id=WorkerId("ghost"),
            server_id=str(uuid.uuid4()),
            command=ServerCommandCommand(line="list"),
        )


async def test_dispatch_fails_fast_on_worker_disconnect(harness: _Harness) -> None:
    # A command in flight when the worker's session ends must fail immediately
    # with WorkerNotConnectedError, not ride out the (long) command timeout.
    stub = await harness.start()
    call = await _registered_call(harness, stub)

    async def disconnect_after_command() -> None:
        # Wait for the command to ride the outbound stream, then end the session.
        await call.read()
        await call.done_writing()

    dropper = asyncio.ensure_future(disconnect_after_command())
    with pytest.raises(WorkerNotConnectedError):
        # The harness command timeout is 5s; failing fast returns well under it.
        await asyncio.wait_for(
            harness.control_plane.dispatch(
                worker_id=WorkerId(_WORKER),
                server_id=str(uuid.uuid4()),
                command=ServerCommandCommand(line="list"),
            ),
            timeout=2.0,
        )
    await dropper


async def test_dispatch_times_out_when_unanswered() -> None:
    harness = _Harness(command_timeout=0.2)
    try:
        stub = await harness.start()
        call = await _registered_call(harness, stub)
        with pytest.raises(CommandTimedOutError):
            await harness.control_plane.dispatch(
                worker_id=WorkerId(_WORKER),
                server_id=str(uuid.uuid4()),
                command=ServerCommandCommand(line="list"),
            )
        await call.done_writing()
    finally:
        await harness.stop()


async def test_timeout_override_extends_the_deadline_for_one_dispatch() -> None:
    # A slow command (the start's hydrate phase, #822) that outlasts the tiny
    # default deadline still resolves when dispatched with a longer override,
    # rather than raising CommandTimedOutError.
    harness = _Harness(command_timeout=0.1)
    try:
        stub = await harness.start()
        call = await _registered_call(harness, stub)

        async def slow_worker_echo() -> None:
            msg = await call.read()
            # Answer AFTER the default 0.1s deadline but within the override.
            await asyncio.sleep(0.3)
            await call.write(
                pb.WorkerMessage(
                    correlation_id=msg.api_command.command_id,
                    command_result=pb.CommandResult(success=True),
                )
            )

        echo = asyncio.ensure_future(slow_worker_echo())
        result = await harness.control_plane.dispatch(
            worker_id=WorkerId(_WORKER),
            server_id=str(uuid.uuid4()),
            command=ServerCommandCommand(line="list"),
            timeout_override=5.0,
        )
        await echo

        assert result.success
        await call.done_writing()
    finally:
        await harness.stop()


async def test_stale_session_teardown_does_not_fail_new_sessions_pending() -> None:
    # A worker reconnects on session B; an in-flight command is registered under
    # B. A delayed teardown of the OLD session A then fires fail_worker_pending
    # with A's token. Because A is no longer the worker's current session, B's
    # pending future must NOT be failed (it can still be answered on B's stream).
    state = ControlPlaneState()
    worker = WorkerId(_WORKER)
    session_a = 1
    session_b = 2

    state.open_session(worker, session_a)
    state.open_session(worker, session_b)  # the reconnect; B is now current
    future = state.register_pending("cmd-1", worker)

    state.fail_worker_pending(worker, session_a, WorkerNotConnectedError(worker.value))

    assert not future.done()


async def test_current_session_teardown_fails_its_pending_fast() -> None:
    # The normal disconnect: the tearing-down session is still the worker's
    # current one, so its in-flight command fails fast instead of riding out the
    # command timeout.
    state = ControlPlaneState()
    worker = WorkerId(_WORKER)
    session = 1

    state.open_session(worker, session)
    future = state.register_pending("cmd-1", worker)

    state.fail_worker_pending(worker, session, WorkerNotConnectedError(worker.value))

    assert future.done()
    with pytest.raises(WorkerNotConnectedError):
        future.result()


async def test_resolve_ignores_result_from_non_owning_worker() -> None:
    # A command is dispatched to one worker, but a CommandResult arrives bearing
    # its command_id from a DIFFERENT worker (a forged report). The future must
    # NOT be resolved; the command stays in flight to time out normally
    # (defense-in-depth, issue #789).
    state = ControlPlaneState()
    owner = WorkerId(_WORKER)
    intruder = WorkerId("33333333-3333-3333-3333-333333333333")

    future = state.register_pending("cmd-1", owner)
    await state.resolve("cmd-1", intruder, pb.CommandResult(success=True))

    assert not future.done()


async def test_resolve_accepts_result_from_owning_worker() -> None:
    # The owning worker's result for its own command resolves the future, even
    # after a forged result from another worker was dropped first (issue #789).
    state = ControlPlaneState()
    owner = WorkerId(_WORKER)
    intruder = WorkerId("33333333-3333-3333-3333-333333333333")

    future = state.register_pending("cmd-1", owner)
    await state.resolve("cmd-1", intruder, pb.CommandResult(success=False))
    assert not future.done()

    await state.resolve("cmd-1", owner, pb.CommandResult(success=True))

    assert future.done()
    assert future.result().success


class _RecordingLateSnapshotSink(LateSnapshotResultSink):
    """Records late-snapshot clear calls for assertions (issue #891)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

    async def clear_held_assignment_on_late_snapshot(
        self, *, server_id: str, worker_id: str, succeeded: bool
    ) -> None:
        self.calls.append((server_id, worker_id, succeeded))


_SERVER = "44444444-4444-4444-4444-444444444444"


def _failed_transfer_result() -> pb.CommandResult:
    return pb.CommandResult(
        success=False,
        error=pb.CommandError(code=pb.COMMAND_ERROR_CODE_TRANSFER_FAILED),
    )


async def test_late_failed_snapshot_clears_held_assignment() -> None:
    # A snapshot dispatch times out (its future is discarded), then the OWNING
    # worker reports a late TRANSFER_FAILED. The unmatched result must route to the
    # late-snapshot sink so the held assignment clears immediately (issue #891).
    sink = _RecordingLateSnapshotSink()
    state = ControlPlaneState(late_snapshot_sink=sink)
    owner = WorkerId(_WORKER)

    state.register_pending("cmd-1", owner, snapshot_server_id=_SERVER)
    state.discard_pending("cmd-1")  # dispatch timeout
    await state.resolve("cmd-1", owner, _failed_transfer_result())

    assert sink.calls == [(_SERVER, owner.value, False)]


async def test_late_snapshot_from_non_owning_worker_is_ignored() -> None:
    # The late snapshot result is forged by a DIFFERENT worker than the one the
    # snapshot was dispatched to. The #789 ownership guard drops it: the sink is
    # never called, and the record is left so the owning worker's report still wins.
    sink = _RecordingLateSnapshotSink()
    state = ControlPlaneState(late_snapshot_sink=sink)
    owner = WorkerId(_WORKER)
    intruder = WorkerId("33333333-3333-3333-3333-333333333333")

    state.register_pending("cmd-1", owner, snapshot_server_id=_SERVER)
    state.discard_pending("cmd-1")
    await state.resolve("cmd-1", intruder, _failed_transfer_result())
    assert sink.calls == []

    # The owning worker's later report still clears it.
    await state.resolve("cmd-1", owner, _failed_transfer_result())
    assert sink.calls == [(_SERVER, owner.value, False)]


async def test_late_successful_snapshot_clears_held_assignment() -> None:
    # A late SUCCESS (the publish landed but the response was slow) clears too: the
    # snapshot exists, so the held assignment is released now rather than left to
    # the stale-stop arm. ``succeeded=True`` distinguishes it from the failed case.
    sink = _RecordingLateSnapshotSink()
    state = ControlPlaneState(late_snapshot_sink=sink)
    owner = WorkerId(_WORKER)

    state.register_pending("cmd-1", owner, snapshot_server_id=_SERVER)
    state.discard_pending("cmd-1")
    await state.resolve("cmd-1", owner, pb.CommandResult(success=True))

    assert sink.calls == [(_SERVER, owner.value, True)]


async def test_unmatched_non_snapshot_result_is_dropped() -> None:
    # An unmatched result that is NOT a timed-out snapshot (a non-snapshot command,
    # or a snapshot that resolved on time) leaves the sink untouched — still just a
    # dropped late/duplicate result (issue #891 leaves the historical path intact).
    sink = _RecordingLateSnapshotSink()
    state = ControlPlaneState(late_snapshot_sink=sink)
    owner = WorkerId(_WORKER)

    # A non-snapshot command that timed out: no late-snapshot record is kept.
    state.register_pending("cmd-1", owner)
    state.discard_pending("cmd-1")
    await state.resolve("cmd-1", owner, _failed_transfer_result())
    assert sink.calls == []

    # An entirely unknown command_id (never registered) is also just dropped.
    await state.resolve("cmd-unknown", owner, pb.CommandResult(success=True))
    assert sink.calls == []


async def test_resolved_snapshot_leaves_no_late_record() -> None:
    # A snapshot that resolves ON TIME drops its in-flight record, so a later
    # duplicate result for the same command_id is not mistaken for a late snapshot.
    sink = _RecordingLateSnapshotSink()
    state = ControlPlaneState(late_snapshot_sink=sink)
    owner = WorkerId(_WORKER)

    future = state.register_pending("cmd-1", owner, snapshot_server_id=_SERVER)
    await state.resolve("cmd-1", owner, pb.CommandResult(success=True))
    assert future.done()

    # A duplicate (the record is gone) is dropped, not routed to the sink.
    await state.resolve("cmd-1", owner, _failed_transfer_result())
    assert sink.calls == []


async def test_disconnect_drops_in_flight_snapshot_record() -> None:
    # A worker disconnect fails its in-flight snapshot future; the in-flight record
    # is dropped too (the upload died with the worker's ctx, #847 DISCONNECT), so a
    # spurious late result cannot later clear via the sink.
    sink = _RecordingLateSnapshotSink()
    state = ControlPlaneState(late_snapshot_sink=sink)
    owner = WorkerId(_WORKER)
    session = 1

    state.open_session(owner, session)
    future = state.register_pending("cmd-1", owner, snapshot_server_id=_SERVER)
    state.fail_worker_pending(owner, session, WorkerNotConnectedError(owner.value))
    assert future.done()

    await state.resolve("cmd-1", owner, _failed_transfer_result())
    assert sink.calls == []


async def test_disconnect_drops_already_promoted_late_snapshot() -> None:
    # A snapshot dispatch TIMED OUT (its record was promoted to _late_snapshots),
    # then the worker disconnected before any late result arrived. The disconnect
    # drops the promoted record so it never accretes (issue #891) — and a spurious
    # later result for it routes nowhere.
    sink = _RecordingLateSnapshotSink()
    state = ControlPlaneState(late_snapshot_sink=sink)
    owner = WorkerId(_WORKER)
    session = 1

    state.open_session(owner, session)
    state.register_pending("cmd-1", owner, snapshot_server_id=_SERVER)
    state.discard_pending("cmd-1")  # timeout: promoted to _late_snapshots
    state.fail_worker_pending(owner, session, WorkerNotConnectedError(owner.value))

    await state.resolve("cmd-1", owner, _failed_transfer_result())
    assert sink.calls == []


async def test_periodic_snapshot_timeout_does_not_clear_final_snapshot_hold() -> None:
    # Regression for the cross-window clear (issue #891 review): a PERIODIC snapshot
    # and a stop-flow FINAL snapshot share the SnapshotCommand type, but only the
    # final one holds the stop at (stopped, stopped, assigned). The exact 3-step
    # interleaving must NOT let a timed-out periodic's late result clear the hold:
    #
    #   1. A periodic snapshot of running server S on worker W times out. Running-id
    #      snapshots take no worker reservation, so nothing serializes what follows.
    #   2. The user stops S; the stop flow holds the row, then dispatches the FINAL
    #      snapshot (final=True), which also times out and HOLDS the assignment.
    #   3. The periodic's late TRANSFER_FAILED arrives — it must clear NOTHING (only
    #      the final snapshot is tracked); the final's own late result clears.
    sink = _RecordingLateSnapshotSink()
    harness = _Harness(command_timeout=0.2)
    harness.state = ControlPlaneState(late_snapshot_sink=sink)
    harness.control_plane = GrpcControlPlane(
        harness.state, clock=harness.clock, timeout_seconds=0.2
    )
    try:
        stub = await harness.start()
        call = await _registered_call(harness, stub)
        worker = WorkerId(_WORKER)

        # Step 1: a PERIODIC snapshot (not final) of S on W times out. The worker
        # never answers, so dispatch raises and discards its future.
        with pytest.raises(CommandTimedOutError):
            await harness.control_plane.dispatch(
                worker_id=worker,
                server_id=_SERVER,
                command=SnapshotCommand(transfer_url="u", transfer_token="t"),
            )
        periodic_cmd = (await call.read()).api_command.command_id

        # Step 2: the stop flow dispatches the FINAL snapshot of S on W; it too
        # times out and HOLDS the assignment.
        with pytest.raises(CommandTimedOutError):
            await harness.control_plane.dispatch(
                worker_id=worker,
                server_id=_SERVER,
                command=SnapshotCommand(transfer_url="u", transfer_token="t"),
                snapshot_is_final=True,
            )
        final_cmd = (await call.read()).api_command.command_id

        # Step 3a: the PERIODIC command's late result arrives. It was never tracked
        # (only the final snapshot is), so it must clear NOTHING.
        await harness.state.resolve(periodic_cmd, worker, _failed_transfer_result())
        assert sink.calls == []

        # Step 3b: the FINAL snapshot's own late result clears the held assignment.
        await harness.state.resolve(final_cmd, worker, _failed_transfer_result())
        assert sink.calls == [(_SERVER, worker.value, False)]

        await call.done_writing()
    finally:
        await harness.stop()


async def test_cancelled_final_snapshot_dispatch_clears_on_late_result() -> None:
    # The CANCELLATION-held window (issue #901): a client disconnect cancels the
    # HTTP-request task at the stop flow's final-snapshot await. The lifecycle
    # holds the assignment exactly like the timeout case, so the cancelled
    # dispatch must discard its pending future the same way — promoting the
    # snapshot record — so the worker's late result is recognised as a held
    # final-snapshot result and clears the assignment immediately, instead of
    # being dropped on the matched path and waiting out the grace arm.
    sink = _RecordingLateSnapshotSink()
    state = ControlPlaneState(late_snapshot_sink=sink)
    control_plane = GrpcControlPlane(state, clock=FakeClock(_T0), timeout_seconds=5.0)
    worker = WorkerId(_WORKER)
    queue = state.open_session(worker, 1)

    task = asyncio.create_task(
        control_plane.dispatch(
            worker_id=worker,
            server_id=_SERVER,
            command=SnapshotCommand(transfer_url="u", transfer_token="t"),
            snapshot_is_final=True,
        )
    )
    # Once the command is on the outbound queue, dispatch is parked awaiting the
    # result future (queue.put on the unbounded queue never suspends).
    command_id = (await queue.get()).api_command.command_id
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The owning worker's late result clears the held assignment via the sink.
    await state.resolve(command_id, worker, _failed_transfer_result())
    assert sink.calls == [(_SERVER, worker.value, False)]


async def test_cancelled_periodic_snapshot_dispatch_leaves_no_late_record() -> None:
    # Round-2 scoping of #898 carried over to the cancellation window: only the
    # stop-flow FINAL snapshot promotes a record. A cancelled PERIODIC snapshot
    # dispatch is simply forgotten — its late result clears nothing.
    sink = _RecordingLateSnapshotSink()
    state = ControlPlaneState(late_snapshot_sink=sink)
    control_plane = GrpcControlPlane(state, clock=FakeClock(_T0), timeout_seconds=5.0)
    worker = WorkerId(_WORKER)
    queue = state.open_session(worker, 1)

    task = asyncio.create_task(
        control_plane.dispatch(
            worker_id=worker,
            server_id=_SERVER,
            command=SnapshotCommand(transfer_url="u", transfer_token="t"),
        )
    )
    command_id = (await queue.get()).api_command.command_id
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await state.resolve(command_id, worker, _failed_transfer_result())
    assert sink.calls == []


async def test_cancelled_fire_and_forget_logger_discards_pending_entry() -> None:
    # The fire-and-forget symmetry of the #901 cancellation arm (issue #1791):
    # the detached logger task parked on the result future is cancelled (process
    # shutdown). It must discard its correlation entry on the way out, like
    # dispatch does, so _pending stays bounded. White-box on _pending /
    # _background_tasks: a lingering non-snapshot entry has no behavioral
    # surface — map boundedness IS the observable — and the shutdown scenario
    # is precisely "the holder of the private task set cancels the task".
    state = ControlPlaneState()
    control_plane = GrpcControlPlane(state, clock=FakeClock(_T0), timeout_seconds=5.0)
    worker = WorkerId(_WORKER)
    queue = state.open_session(worker, 1)

    await control_plane.dispatch_fire_and_forget(
        worker_id=worker,
        server_id=_SERVER,
        command=TunnelDialCommand(endpoint="relay:443", token="t", tls_ca_pem="ca"),
    )
    command_id = (await queue.get()).api_command.command_id
    (task,) = control_plane._background_tasks
    # queue.put/get on a non-empty unbounded queue never suspend, so the logger
    # task has not started yet; yield once so it parks on the result future —
    # cancelling a never-started task would skip its except arm entirely.
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert command_id not in state._pending


async def test_reconnect_sweeps_promoted_late_snapshot_from_prior_session() -> None:
    # A snapshot dispatch TIMED OUT (promoted to _late_snapshots), then the worker
    # reconnected on a NEW session WITHOUT a clean teardown of the old one (so the
    # session-guarded fail_worker_pending never swept it). The superseding session's
    # open_session must drop the stale promoted record (issue #891 nit): the old
    # upload died with its ctx, so no late result will arrive to consume it.
    sink = _RecordingLateSnapshotSink()
    state = ControlPlaneState(late_snapshot_sink=sink)
    owner = WorkerId(_WORKER)
    session_a = 1
    session_b = 2

    state.open_session(owner, session_a)
    state.register_pending("cmd-1", owner, snapshot_server_id=_SERVER)
    state.discard_pending("cmd-1")  # timeout: promoted to _late_snapshots

    # The reconnect supersedes session A without a clean teardown.
    state.open_session(owner, session_b)

    # A spurious later result for the old command routes nowhere — the record is gone.
    await state.resolve("cmd-1", owner, _failed_transfer_result())
    assert sink.calls == []


async def test_register_ack_echoes_correlation_id_and_sets_sent_at(
    harness: _Harness,
) -> None:
    """RegisterAck echoes the Register's correlation_id and populates sent_at
    (issue #2002, NFR-OBS-1)."""

    stub = await harness.start()
    call = stub.Session(metadata=_auth())
    reg = pb.WorkerMessage(
        correlation_id="trace-register-42",
        register=pb.Register(
            worker_id=_WORKER,
            worker_version="1.0.0",
            capabilities=pb.WorkerCapabilities(
                drivers=[pb.EXECUTION_DRIVER_KIND_CONTAINER]
            ),
        ),
    )
    await call.write(reg)
    ack_msg = await call.read()

    assert ack_msg.correlation_id == "trace-register-42"
    assert ack_msg.HasField("sent_at")
    assert ack_msg.sent_at.seconds > 0
    await call.done_writing()


async def test_dispatched_command_carries_sent_at(harness: _Harness) -> None:
    """Every dispatched ApiMessage populates sent_at from the injected clock
    (issue #2002, issue #2166)."""

    stub = await harness.start()
    call = await _registered_call(harness, stub)
    expected = Timestamp()
    expected.FromDatetime(_T0)

    async def worker_echo() -> None:
        msg = await call.read()
        assert msg.HasField("sent_at")
        assert msg.sent_at == expected
        await call.write(
            pb.WorkerMessage(
                correlation_id=msg.api_command.command_id,
                command_result=pb.CommandResult(success=True),
            )
        )

    echo = asyncio.ensure_future(worker_echo())
    await harness.control_plane.dispatch(
        worker_id=WorkerId(_WORKER),
        server_id=str(uuid.uuid4()),
        command=ServerCommandCommand(line="list"),
    )
    await echo
    await call.done_writing()


# ---------------------------------------------------------------------------
# Reconnect-race defense-in-depth: StaleSessionError (issue #1694)
# ---------------------------------------------------------------------------


async def test_open_session_refuses_a_stale_token_after_a_newer_open() -> None:
    """open_session(W, 2) then open_session(W, 1) raises StaleSessionError;
    outbound_for(W) still returns session-2's queue (issue #1694)."""

    state = ControlPlaneState()
    worker = WorkerId(_WORKER)

    queue_2 = state.open_session(worker, 2)
    with pytest.raises(StaleSessionError):
        state.open_session(worker, 1)

    # The current outbound queue must still be the one from session 2.
    assert state.outbound_for(worker) is queue_2


async def test_open_session_refuses_a_stale_token_after_close() -> None:
    """open_session(W, 2), close(W, q2), then open_session(W, 1) still refuses.

    The high-water mark is monotonic: closing a session does not reset it, so a
    stale session arriving after the current one closed is still rejected.
    """

    state = ControlPlaneState()
    worker = WorkerId(_WORKER)

    queue_2 = state.open_session(worker, 2)
    state.close_session(worker, queue_2)

    with pytest.raises(StaleSessionError):
        state.open_session(worker, 1)


# ---------------------------------------------------------------------------
# Stale queued command skip (issue #1697)
# ---------------------------------------------------------------------------


async def test_timed_out_dispatch_message_is_stale() -> None:
    """A command whose dispatch timed out has no pending entry; discard_if_stale
    returns True for it and False for a live pending command (issue #1697)."""

    state = ControlPlaneState()
    worker = WorkerId(_WORKER)
    state.open_session(worker, 1)

    # A live pending command is NOT stale.
    future = state.register_pending("live-cmd", worker)
    assert state.discard_if_stale("live-cmd") is False
    assert not future.done()

    # A timed-out command (discarded pending) IS stale.
    state.register_pending("stale-cmd", worker)
    state.discard_pending("stale-cmd")
    assert state.discard_if_stale("stale-cmd") is True


async def test_stale_queued_commands_are_not_replayed_on_resume() -> None:
    """After timed-out commands accrue in the outbound queue, the worker's first
    received message must be the live command — stale ones are skipped at send
    time by the Session generator (issue #1697).

    The scenario: a stalled worker reader causes messages to pile up in the
    outbound asyncio.Queue. When the Session generator resumes dequeuing, it must
    skip stale messages (those whose pending entry was discarded on timeout).
    """

    harness = _Harness(command_timeout=5.0)
    try:
        stub = await harness.start()
        call = await _registered_call(harness, stub)
        worker = WorkerId(_WORKER)
        queue = harness.state.outbound_for(worker)
        assert queue is not None

        # Simulate 3 timed-out commands: manually enqueue messages and discard
        # their pending entries. All operations are synchronous (no awaits
        # between put_nowait and discard_pending), so the Session generator
        # does not get a scheduling slot to dequeue them between the two steps.
        for i in range(3):
            cmd_id = f"stale-{i}"
            harness.state.register_pending(cmd_id, worker)
            api_cmd = pb.ApiCommand(
                command_id=cmd_id,
                server_id="s",
                server_command=pb.ServerCommand(line="stale"),
            )
            msg = pb.ApiMessage(correlation_id=cmd_id, api_command=api_cmd)
            queue.put_nowait(msg)
            harness.state.discard_pending(cmd_id)

        # Now dispatch a live command whose result the worker answers.
        async def worker_read_and_answer() -> str:
            msg = await call.read()
            await call.write(
                pb.WorkerMessage(
                    correlation_id=msg.api_command.command_id,
                    command_result=pb.CommandResult(success=True),
                )
            )
            line: str = msg.api_command.server_command.line
            return line

        echo = asyncio.ensure_future(worker_read_and_answer())
        result = await harness.control_plane.dispatch(
            worker_id=worker,
            server_id=str(uuid.uuid4()),
            command=ServerCommandCommand(line="live"),
        )
        received_line = await echo

        # The worker must see the live command first, not a stale one.
        assert received_line == "live"
        assert result.success
        await call.done_writing()
    finally:
        await harness.stop()


# ---------------------------------------------------------------------------
# Cancel-to-discard window: resolve consumes final-snapshot result (issue #1996)
# ---------------------------------------------------------------------------


async def test_resolve_on_cancelled_future_fires_late_snapshot_sink() -> None:
    """Core regression (issue #1996): future.cancel() then resolve() must still
    route the result through the late-snapshot sink so the held assignment clears."""

    sink = _RecordingLateSnapshotSink()
    state = ControlPlaneState(late_snapshot_sink=sink)
    owner = WorkerId(_WORKER)

    future = state.register_pending("cmd-1", owner, snapshot_server_id=_SERVER)
    future.cancel()
    await state.resolve("cmd-1", owner, _failed_transfer_result())

    assert sink.calls == [(_SERVER, owner.value, False)]


async def test_resolve_on_cancelled_future_with_success_result() -> None:
    """Same cancel-to-discard window with a SUCCESS result: the sink sees
    succeeded=True (issue #1996)."""

    sink = _RecordingLateSnapshotSink()
    state = ControlPlaneState(late_snapshot_sink=sink)
    owner = WorkerId(_WORKER)

    future = state.register_pending("cmd-1", owner, snapshot_server_id=_SERVER)
    future.cancel()
    await state.resolve("cmd-1", owner, pb.CommandResult(success=True))

    assert sink.calls == [(_SERVER, owner.value, True)]


async def test_dispatch_cancel_resolve_before_discard_fires_sink() -> None:
    """Integration-shaped: dispatch, cancel task, resolve before the cancelled
    task's discard_pending runs (issue #1996)."""

    sink = _RecordingLateSnapshotSink()
    state = ControlPlaneState(late_snapshot_sink=sink)
    control_plane = GrpcControlPlane(state, clock=FakeClock(_T0), timeout_seconds=5.0)
    worker = WorkerId(_WORKER)
    queue = state.open_session(worker, 1)

    task = asyncio.create_task(
        control_plane.dispatch(
            worker_id=worker,
            server_id=_SERVER,
            command=SnapshotCommand(transfer_url="u", transfer_token="t"),
            snapshot_is_final=True,
        )
    )
    command_id = (await queue.get()).api_command.command_id
    task.cancel()
    # Resolve BEFORE the cancelled task is awaited (discard_pending has not run).
    await state.resolve(command_id, worker, _failed_transfer_result())
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sink.calls == [(_SERVER, worker.value, False)]


async def test_resolve_cancelled_periodic_snapshot_does_not_fire_sink() -> None:
    """Scoping guard: a cancelled PERIODIC snapshot (no snapshot_server_id) must
    NOT route through the sink (issue #1996)."""

    sink = _RecordingLateSnapshotSink()
    state = ControlPlaneState(late_snapshot_sink=sink)
    owner = WorkerId(_WORKER)

    # Periodic snapshot: no snapshot_server_id.
    future = state.register_pending("cmd-1", owner)
    future.cancel()
    await state.resolve("cmd-1", owner, _failed_transfer_result())

    assert sink.calls == []


async def test_discard_pending_after_cancelled_resolve_finds_nothing() -> None:
    """No double-fire: after resolve consumed the cancelled future's snapshot
    record, a subsequent discard_pending is a no-op (issue #1996)."""

    sink = _RecordingLateSnapshotSink()
    state = ControlPlaneState(late_snapshot_sink=sink)
    owner = WorkerId(_WORKER)

    future = state.register_pending("cmd-1", owner, snapshot_server_id=_SERVER)
    future.cancel()
    await state.resolve("cmd-1", owner, _failed_transfer_result())
    assert sink.calls == [(_SERVER, owner.value, False)]

    # discard_pending after resolve already consumed the entry — no double-fire.
    state.discard_pending("cmd-1")
    assert sink.calls == [(_SERVER, owner.value, False)]


async def test_skipped_stale_final_snapshot_drops_late_record() -> None:
    """discard_if_stale for a timed-out final snapshot also cleans up the
    promoted _late_snapshots record (issue #1697)."""

    sink = _RecordingLateSnapshotSink()
    state = ControlPlaneState(late_snapshot_sink=sink)
    worker = WorkerId(_WORKER)
    state.open_session(worker, 1)

    state.register_pending("cmd-1", worker, snapshot_server_id=_SERVER)
    state.discard_pending("cmd-1")  # timeout: promoted to _late_snapshots

    # The Session loop would call discard_if_stale when it dequeues the message.
    assert state.discard_if_stale("cmd-1") is True

    # The late-snapshot record must have been cleaned up — a late result routes
    # nowhere.
    await state.resolve("cmd-1", worker, _failed_transfer_result())
    assert sink.calls == []
