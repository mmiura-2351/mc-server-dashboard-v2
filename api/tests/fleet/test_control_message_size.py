"""Control-plane message-size limits, through a real grpc.aio stack (issue #3360).

One Worker's ``Session`` stream carries every server on that Worker, so a message
gRPC refuses for its size must cost one command, never the stream. These tests
run the real servicer behind a real grpc.aio server built with the production
channel options, and a real client channel standing in for the Worker:

- the limit is the value the Worker holds (the shared contract file);
- a file read and a file edit at exactly the file cap cross the stream;
- a command too large to send fails as that command's own error, unsent, and the
  next command for another server succeeds on the same stream;
- what gRPC itself does to an over-limit message, in each direction — the reason
  both sides check before sending.

These tests need no Postgres and run in the unit-runnable suite.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import grpc
import pytest
from grpc import aio

from mc_server_dashboard_api.fleet.adapters.control_plane import (
    MAX_MESSAGE_BYTES,
    ControlPlaneState,
    GrpcControlPlane,
)
from mc_server_dashboard_api.fleet.adapters.grpc_server import (
    WorkerSessionServicer,
    _server_options,
)
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.domain.control_plane import (
    Command,
    CommandResult,
    CommandResultCode,
    EditFileCommand,
    FileAccessReason,
    ListFilesCommand,
    ReadFileCommand,
    ServerCommandCommand,
    TunnelDialCommand,
)
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId
from mc_server_dashboard_api.servers.application.files import MAX_EDIT_BYTES
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
_WORKER = "22222222-2222-2222-2222-222222222222"
_AUTH = [("authorization", f"Bearer {_CREDENTIAL}")]

# Repo root: tests/fleet/<file> -> api/ -> repo root.
_LIMITS_PATH = (
    Path(__file__).resolve().parents[3]
    / "proto"
    / "contract"
    / "control_plane_limits.json"
)

# The channel options of a Worker that holds the same limit (the Go client sets
# both directions, worker/internal/adapters/controlplane).
_WORKER_OPTIONS = [
    ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
    ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
]
# A peer with no send limit of its own: what reaches the API is then bounded by
# the API's receive limit alone.
_UNLIMITED_SEND = [
    ("grpc.max_send_message_length", -1),
    ("grpc.max_receive_message_length", -1),
]


class _Harness:
    def __init__(self) -> None:
        self.state = ControlPlaneState()
        self.control_plane = GrpcControlPlane(
            self.state, clock=FakeClock(_T0), timeout_seconds=10.0
        )
        self.registry = InMemoryWorkerRegistry(
            clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT
        )
        self.registrations = 0
        self._server: aio.Server | None = None
        self._channel: aio.Channel | None = None

    async def start(
        self, client_options: list[tuple[str, int]] = _WORKER_OPTIONS
    ) -> aio.StreamStreamCall:
        """Start the server, register one Worker, and return its open call."""

        server = aio.server(options=_server_options(_TIMEOUT))
        servicer = WorkerSessionServicer(
            registry=self.registry,
            clock=FakeClock(_T0),
            worker_credential=_CREDENTIAL,
            heartbeat_timeout=_TIMEOUT,
            transfer_deadline=dt.timedelta(seconds=660),
            control_plane=self.state,
            state_sink=FakeServerStateSink(),
            real_time_events=RecordingRealTimeEvents(),
        )
        add_WorkerServiceServicer_to_server(servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        self._server = server
        self._channel = aio.insecure_channel(
            f"127.0.0.1:{port}", options=client_options
        )
        call = WorkerServiceStub(self._channel).Session(metadata=_AUTH)
        caps = pb.WorkerCapabilities(drivers=[pb.EXECUTION_DRIVER_KIND_CONTAINER])
        await call.write(
            pb.WorkerMessage(
                correlation_id="reg-1",
                register=pb.Register(
                    worker_id=_WORKER, worker_version="1.0.0", capabilities=caps
                ),
            )
        )
        await call.read()  # ack
        for _ in range(200):
            if self.state.outbound_for(WorkerId(_WORKER)) is not None:
                return call
            await asyncio.sleep(0.01)
        raise AssertionError("session was not opened in time")

    async def stop(self) -> None:
        if self._channel is not None:
            await self._channel.close()
        if self._server is not None:
            await self._server.stop(grace=None)

    async def dispatch(self, command: Command, *, server_id: str) -> CommandResult:
        return await self.control_plane.dispatch(
            worker_id=WorkerId(_WORKER), server_id=server_id, command=command
        )

    async def assert_session_survived(self, call: aio.StreamStreamCall) -> None:
        """A command for ANOTHER server still round-trips on the same stream."""

        async def answer() -> None:
            msg = await call.read()
            assert msg.api_command.WhichOneof("command") == "server_command"
            await call.write(
                pb.WorkerMessage(
                    correlation_id=msg.api_command.command_id,
                    command_result=pb.CommandResult(
                        success=True, command_output="pong"
                    ),
                )
            )

        worker = asyncio.ensure_future(answer())
        result = await self.dispatch(
            ServerCommandCommand(line="ping"), server_id=str(uuid.uuid4())
        )
        await worker
        assert result.success
        assert result.output == "pong"
        assert not call.done()


@pytest.fixture
async def harness() -> AsyncIterator[_Harness]:
    h = _Harness()
    try:
        yield h
    finally:
        await h.stop()


def test_the_limits_are_the_ones_the_worker_holds() -> None:
    limits = json.loads(_LIMITS_PATH.read_text())

    assert MAX_MESSAGE_BYTES == limits["max_message_bytes"]
    assert MAX_EDIT_BYTES == limits["max_file_bytes"]


def test_the_server_sets_the_limit_in_both_directions() -> None:
    options = dict(_server_options(_TIMEOUT))

    assert options["grpc.max_receive_message_length"] == MAX_MESSAGE_BYTES
    assert options["grpc.max_send_message_length"] == MAX_MESSAGE_BYTES


@pytest.mark.parametrize("size", [MAX_EDIT_BYTES - 1, MAX_EDIT_BYTES])
async def test_a_file_read_up_to_the_file_cap_crosses_the_stream(
    harness: _Harness, size: int
) -> None:
    call = await harness.start()
    content = b"x" * size

    async def answer() -> None:
        msg = await call.read()
        assert msg.api_command.WhichOneof("command") == "read_file"
        await call.write(
            pb.WorkerMessage(
                correlation_id=msg.api_command.command_id,
                command_result=pb.CommandResult(success=True, file_content=content),
            )
        )

    worker = asyncio.ensure_future(answer())
    result = await harness.dispatch(
        ReadFileCommand(path="world/big.dat"), server_id=str(uuid.uuid4())
    )
    await worker

    assert result.success
    assert result.file_content == content
    await harness.assert_session_survived(call)


@pytest.mark.parametrize("size", [MAX_EDIT_BYTES - 1, MAX_EDIT_BYTES])
async def test_a_file_edit_up_to_the_file_cap_crosses_the_stream(
    harness: _Harness, size: int
) -> None:
    call = await harness.start()
    content = b"x" * size
    received: list[bytes] = []

    async def answer() -> None:
        msg = await call.read()
        received.append(msg.api_command.edit_file.content)
        await call.write(
            pb.WorkerMessage(
                correlation_id=msg.api_command.command_id,
                command_result=pb.CommandResult(success=True),
            )
        )

    worker = asyncio.ensure_future(answer())
    result = await harness.dispatch(
        # A path as long as Linux allows (PATH_MAX) still fits beside the body.
        EditFileCommand(path="d/" * 2047 + "f", content=content),
        server_id=str(uuid.uuid4()),
    )
    await worker

    assert result.success
    assert received == [content]
    await harness.assert_session_survived(call)


@pytest.mark.parametrize(
    ("command", "kind", "code", "reason"),
    [
        (
            EditFileCommand(path="a.txt", content=b"x" * MAX_MESSAGE_BYTES),
            "edit_file",
            CommandResultCode.FILE_ACCESS_DENIED,
            FileAccessReason.PAYLOAD_TOO_LARGE,
        ),
        (
            ReadFileCommand(path="p" * MAX_MESSAGE_BYTES),
            "read_file",
            CommandResultCode.FILE_ACCESS_DENIED,
            FileAccessReason.PAYLOAD_TOO_LARGE,
        ),
        (
            ListFilesCommand(path="p" * MAX_MESSAGE_BYTES),
            "list_files",
            CommandResultCode.FILE_ACCESS_DENIED,
            FileAccessReason.PAYLOAD_TOO_LARGE,
        ),
        (
            ServerCommandCommand(line="say " + "x" * MAX_MESSAGE_BYTES),
            "server_command",
            CommandResultCode.INTERNAL,
            FileAccessReason.UNSPECIFIED,
        ),
    ],
)
async def test_a_command_too_large_to_send_fails_alone(
    harness: _Harness,
    caplog: pytest.LogCaptureFixture,
    command: Command,
    kind: str,
    code: CommandResultCode,
    reason: FileAccessReason,
) -> None:
    call = await harness.start()
    server_id = str(uuid.uuid4())

    with caplog.at_level(logging.WARNING):
        result = await harness.dispatch(command, server_id=server_id)

    assert not result.success
    assert result.code is code
    assert result.file_access_reason is reason
    # Nothing is left waiting on a command that was never sent.
    assert harness.state._pending == {}
    [record] = [r for r in caplog.records if "message limit" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert record.kind == kind  # type: ignore[attr-defined]
    assert record.server_id == server_id  # type: ignore[attr-defined]
    # The WARN names the command, not its payload.
    assert "xxxx" not in record.getMessage() and "pppp" not in record.getMessage()
    await harness.assert_session_survived(call)


async def test_a_command_exactly_at_the_limit_is_sent(harness: _Harness) -> None:
    call = await harness.start()
    server_id = str(uuid.uuid4())
    sizes: list[int] = []

    async def answer() -> None:
        msg = await call.read()
        sizes.append(msg.ByteSize())
        await call.write(
            pb.WorkerMessage(
                correlation_id=msg.api_command.command_id,
                command_result=pb.CommandResult(success=True, command_output="ok"),
            )
        )

    # Find the line length that makes the encoded message exactly the limit: the
    # envelope's size is fixed for a given server id, so measure it once.
    probe: list[int] = []

    async def measure() -> None:
        msg = await call.read()
        probe.append(msg.ByteSize())
        await call.write(
            pb.WorkerMessage(
                correlation_id=msg.api_command.command_id,
                command_result=pb.CommandResult(success=True),
            )
        )

    filler = 3_000_000  # long enough that every length prefix is its final width
    measuring = asyncio.ensure_future(measure())
    await harness.dispatch(ServerCommandCommand(line="x" * filler), server_id=server_id)
    await measuring
    line = "x" * (filler + MAX_MESSAGE_BYTES - probe[0])

    worker = asyncio.ensure_future(answer())
    result = await harness.dispatch(
        ServerCommandCommand(line=line), server_id=server_id
    )
    await worker

    assert sizes == [MAX_MESSAGE_BYTES]
    assert result.success
    over = await harness.dispatch(
        ServerCommandCommand(line=line + "x"), server_id=server_id
    )
    assert over.code is CommandResultCode.INTERNAL
    await harness.assert_session_survived(call)


async def test_a_fire_and_forget_command_too_large_to_send_is_dropped(
    harness: _Harness, caplog: pytest.LogCaptureFixture
) -> None:
    call = await harness.start()
    server_id = str(uuid.uuid4())

    with caplog.at_level(logging.WARNING):
        await harness.control_plane.dispatch_fire_and_forget(
            worker_id=WorkerId(_WORKER),
            server_id=server_id,
            command=TunnelDialCommand(
                endpoint="relay:1", token="t", tls_ca_pem="c" * MAX_MESSAGE_BYTES
            ),
        )

    assert harness.state._pending == {}
    [record] = [r for r in caplog.records if "message limit" in r.getMessage()]
    assert record.kind == "tunnel_dial"  # type: ignore[attr-defined]
    assert record.server_id == server_id  # type: ignore[attr-defined]
    await harness.assert_session_survived(call)


# --- What gRPC itself does to an over-limit message -------------------------
#
# Both directions end the WHOLE stream with RESOURCE_EXHAUSTED: there is no
# per-message refusal to recover from. That is why each side checks the encoded
# size before it sends.


async def test_grpc_ends_the_session_when_the_api_receives_an_over_limit_message(
    harness: _Harness,
) -> None:
    # A Worker that does not check before sending (no send limit of its own).
    call = await harness.start(client_options=_UNLIMITED_SEND)

    await call.write(
        pb.WorkerMessage(
            correlation_id="c-1",
            command_result=pb.CommandResult(
                success=True, file_content=b"x" * MAX_MESSAGE_BYTES
            ),
        )
    )

    assert await asyncio.wait_for(call.code(), timeout=10) is (
        grpc.StatusCode.RESOURCE_EXHAUSTED
    )
    # The Worker's every server lost its stream, not just the one command.
    for _ in range(200):
        if harness.state.outbound_for(WorkerId(_WORKER)) is None:
            break
        await asyncio.sleep(0.01)
    assert harness.state.outbound_for(WorkerId(_WORKER)) is None


async def test_grpc_ends_the_session_when_the_api_sends_an_over_limit_message(
    harness: _Harness,
) -> None:
    call = await harness.start(client_options=_UNLIMITED_SEND)
    queue = harness.state.outbound_for(WorkerId(_WORKER))
    assert queue is not None

    # Bypass dispatch's own check: put an over-limit message straight on the
    # Worker's outbound queue, as dispatch did before this check existed.
    command_id = str(uuid.uuid4())
    harness.state.register_pending(command_id, WorkerId(_WORKER))
    await queue.put(
        pb.ApiMessage(
            correlation_id=command_id,
            api_command=pb.ApiCommand(
                command_id=command_id,
                server_id=str(uuid.uuid4()),
                edit_file=pb.EditFile(path="a", content=b"x" * MAX_MESSAGE_BYTES),
            ),
        )
    )

    assert await asyncio.wait_for(call.code(), timeout=10) is (
        grpc.StatusCode.RESOURCE_EXHAUSTED
    )
