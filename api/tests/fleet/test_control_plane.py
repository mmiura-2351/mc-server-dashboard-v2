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
from grpc import aio

from mc_server_dashboard_api.fleet.adapters.control_plane import (
    ControlPlaneState,
    GrpcControlPlane,
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
    StartServerCommand,
    WorkerNotConnectedError,
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
_CREDENTIAL = "shared-worker-secret"
# Registers through the real gRPC servicer, which requires a UUID worker id
# (issue #99); the fleet WorkerId value object itself stays free-form.
_WORKER = "22222222-2222-2222-2222-222222222222"


def _register_message() -> pb.WorkerMessage:
    caps = pb.WorkerCapabilities(drivers=[pb.EXECUTION_DRIVER_KIND_HOST_PROCESS])
    return pb.WorkerMessage(
        correlation_id="reg-1",
        register=pb.Register(
            worker_id=_WORKER, worker_version="1.0.0", capabilities=caps
        ),
    )


class _Harness:
    def __init__(self, *, command_timeout: float = 5.0) -> None:
        self.state = ControlPlaneState()
        self.control_plane = GrpcControlPlane(
            self.state, timeout_seconds=command_timeout
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
            driver=DriverKind.HOST_PROCESS,
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
            driver=DriverKind.HOST_PROCESS,
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
            driver=DriverKind.HOST_PROCESS,
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
            driver=DriverKind.HOST_PROCESS,
            jar_relpath="server.jar",
            minecraft_version="1.21.1",
            memory_limit_bytes=2048 * 1024 * 1024,
        ),
    )
    await echo

    assert received[0].memory_limit_bytes == 2048 * 1024 * 1024
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
            driver=DriverKind.HOST_PROCESS,
            jar_relpath="server.jar",
            minecraft_version="1.21.1",
        ),
    )
    await echo

    assert received[0].memory_limit_bytes == 0
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
