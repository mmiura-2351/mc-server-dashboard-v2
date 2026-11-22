"""In-process integration tests for the control-plane gRPC server.

Starts a real grpc.aio server on an ephemeral localhost port and dials it with
a real client channel, so the full stream lifecycle is exercised end to end
(CONTROL_PLANE.md Section 4) without any database (NFR-TEST-1):

- register -> RegisterAck{heartbeat_interval};
- a wrong/missing credential is rejected with UNAUTHENTICATED, as is a
  non-ASCII presented one or a configured one with no UTF-8 encoding (#3695);
- a non-Register first message is rejected with FAILED_PRECONDITION;
- Event{Heartbeat} refreshes liveness in the shared registry;
- closing the stream marks the Worker offline (FR-WRK-4).

These tests need no Postgres and run in the unit-runnable suite.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import grpc
import pytest
from grpc import aio

from mc_server_dashboard_api.core.adapters.database import (
    DatabaseConnectError,
    DatabaseTimeoutError,
    create_engine,
    create_session_factory,
)
from mc_server_dashboard_api.fleet.adapters import grpc_server
from mc_server_dashboard_api.fleet.adapters.control_plane import (
    ControlPlaneState,
    GrpcControlPlane,
)
from mc_server_dashboard_api.fleet.adapters.grpc_server import (
    _MAX_CONSECUTIVE_HANDLER_FAILURES,
    _STATE_BY_PROTO,
    WorkerSessionServicer,
    _keepalive_options,
)
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.domain.control_plane import (
    CommandResultCode,
    ReclaimDeletedServerCommand,
)
from mc_server_dashboard_api.fleet.domain.entities import WorkerStatus
from mc_server_dashboard_api.fleet.domain.late_snapshot_sink import (
    LateSnapshotResultSink,
)
from mc_server_dashboard_api.fleet.domain.real_time_events import EventStream
from mc_server_dashboard_api.fleet.domain.registry import SessionToken
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId
from mc_server_dashboard_api.servers.adapters.clock import SystemClock
from mc_server_dashboard_api.servers.adapters.server_state_sink import (
    ServersServerStateSink,
)
from mcsd.controlplane.v1 import control_plane_pb2 as pb
from mcsd.controlplane.v1.control_plane_pb2_grpc import (
    WorkerServiceStub,
    add_WorkerServiceServicer_to_server,
)
from tests.core.fakes import black_hole_database
from tests.fleet.fakes import (
    FakeClock,
    FakeDatabasePing,
    FakeServerStateSink,
    RecordingRealTimeEvents,
    make_worker,
)

_T0 = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_TIMEOUT = dt.timedelta(seconds=30)
_TRANSFER_DEADLINE = dt.timedelta(seconds=660)
_CREDENTIAL = "shared-worker-secret"
# The API persists assigned_worker_id as a UUID column, so a Worker must
# register with a UUID-format id (CONFIGURATION.md Section 6.1, issue #99).
_WORKER_ID = "22222222-2222-2222-2222-222222222222"


def _register_message(
    worker_id: str = _WORKER_ID,
    held_servers: dict[str, int] | None = None,
    unheld_scratch_server_ids: list[str] | None = None,
) -> pb.WorkerMessage:
    caps = pb.WorkerCapabilities(
        drivers=[pb.EXECUTION_DRIVER_KIND_CONTAINER],
        max_servers=4,
        resources=pb.HostResources(cpu_cores=8, memory_bytes=16_000_000_000),
    )
    return pb.WorkerMessage(
        correlation_id="reg-1",
        register=pb.Register(
            worker_id=worker_id,
            worker_version="1.0.0",
            capabilities=caps,
            held_servers=[
                pb.HeldServer(server_id=sid, generation=gen)
                for sid, gen in (held_servers or {}).items()
            ],
            unheld_scratch_server_ids=unheld_scratch_server_ids or [],
        ),
    )


def _heartbeat_message() -> pb.WorkerMessage:
    return pb.WorkerMessage(event=pb.Event(heartbeat=pb.Heartbeat()))


class _Harness:
    def __init__(
        self,
        registry: InMemoryWorkerRegistry,
        clock: FakeClock,
        *,
        heartbeat_timeout: dt.timedelta = _TIMEOUT,
        state_sink: FakeServerStateSink | None = None,
        control_plane: ControlPlaneState | None = None,
        real_time_events: RecordingRealTimeEvents | None = None,
        worker_credential: str = _CREDENTIAL,
        database_ping: FakeDatabasePing | None = None,
    ) -> None:
        self.worker_credential = worker_credential
        self.registry = registry
        self.clock = clock
        # The servicer derives its watchdog poll interval from this (timeout/3),
        # so it must match the registry's timeout for the poll cadence to track
        # the liveness window it polls (issue #1732). Defaults to the production
        # 30 s used by most tests; the shared-clock tests pass _SHORT_TIMEOUT so
        # the watchdog polls at ~0.1 s instead of 10 s.
        self.heartbeat_timeout = heartbeat_timeout
        self.state_sink = state_sink or FakeServerStateSink()
        self.control_plane = control_plane or ControlPlaneState()
        self.real_time_events = real_time_events or RecordingRealTimeEvents()
        self.database_ping = database_ping or FakeDatabasePing()
        self.servicer: WorkerSessionServicer | None = None
        self._server: aio.Server | None = None
        self._port: int | None = None
        self._channels: list[aio.Channel] = []

    async def start(self) -> WorkerServiceStub:
        server = aio.server()
        servicer = WorkerSessionServicer(
            registry=self.registry,
            clock=self.clock,
            worker_credential=self.worker_credential,
            heartbeat_timeout=self.heartbeat_timeout,
            transfer_deadline=_TRANSFER_DEADLINE,
            control_plane=self.control_plane,
            state_sink=self.state_sink,
            real_time_events=self.real_time_events,
            database_ping=self.database_ping,
        )
        add_WorkerServiceServicer_to_server(servicer, server)
        self._port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        self.servicer = servicer
        self._server = server
        return await self.new_stub()

    async def new_stub(self) -> WorkerServiceStub:
        """Open a fresh channel to the running server and return a stub on it.

        A fresh connection per call keeps a previous channel's teardown (or a
        connection-level GOAWAY) from racing a new call (issue #181). Every
        channel is tracked and closed in ``stop``.

        The channel is awaited to READY before the stub is handed out: under
        full-suite load the server's listening socket can still be coming up
        when the client dials, so issuing the first RPC eagerly raced the bind
        and surfaced a transient UNAVAILABLE / "Connection refused" (issue
        #667). Waiting for readiness removes that race without masking any
        per-call status.
        """

        assert self._port is not None, "start() must run before new_stub()"
        channel = aio.insecure_channel(f"127.0.0.1:{self._port}")
        self._channels.append(channel)
        await asyncio.wait_for(channel.channel_ready(), timeout=5)
        return WorkerServiceStub(channel)

    async def stop(self) -> None:
        for channel in self._channels:
            await channel.close()
        if self._server is not None:
            await self._server.stop(grace=None)


@pytest.fixture
async def harness() -> AsyncIterator[_Harness]:
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
    )
    try:
        yield h
    finally:
        await h.stop()


def _auth(credential: str | None) -> list[tuple[str, str]]:
    if credential is None:
        return []
    return [("authorization", f"Bearer {credential}")]


# Connection-level statuses that mean "the transport went away", not "the
# server made a per-call decision". Under full-suite load a channel can emit a
# GOAWAY (surfacing as INTERNAL/UNAVAILABLE) before the per-call abort's
# trailing status is read, masking the real rejection code (issue #181).
_GOAWAY_CODES = frozenset({grpc.StatusCode.INTERNAL, grpc.StatusCode.UNAVAILABLE})


async def _drive_terminal_code(
    call: aio.StreamStreamCall, message: pb.WorkerMessage | None = None
) -> grpc.StatusCode:
    """Drive a rejected ``Session`` to its terminal status, race-free.

    The server aborts the stream (e.g. UNAUTHENTICATED) immediately on connect,
    so the abort may surface on the client's ``write`` or on its ``read``
    depending on timing — asserting on either one alone is flaky. The
    authoritative trailing status is always available via ``call.code()`` once
    the call terminates, so we let the abort land wherever it does and read the
    final code from the call object. ``code()`` is bounded by a timeout so an
    auth-bypass regression that wrongly accepts the session fails fast instead
    of hanging the suite.
    """

    with contextlib.suppress(aio.AioRpcError):
        await call.write(message if message is not None else _register_message())
    with contextlib.suppress(aio.AioRpcError):
        await call.read()
    return await asyncio.wait_for(call.code(), timeout=5)


async def _terminal_code(
    harness: _Harness,
    metadata: list[tuple[str, str]],
    message: pb.WorkerMessage | None = None,
) -> grpc.StatusCode:
    """Return the terminal status of a rejected ``Session``, GOAWAY-tolerant.

    The server's per-call abort is authoritative, but a connection-level GOAWAY
    can race it and surface as INTERNAL/UNAVAILABLE instead (issue #181). That
    is a transport artifact, not the rejection: retry once on a *fresh* channel,
    which cannot inherit the prior connection's teardown. One retry is enough —
    two independent GOAWAYs back-to-back would itself be a real bug worth a
    failure.
    """

    stub = await harness.new_stub()
    code = await _drive_terminal_code(stub.Session(metadata=metadata), message)
    if code in _GOAWAY_CODES:
        stub = await harness.new_stub()
        code = await _drive_terminal_code(stub.Session(metadata=metadata), message)
    return code


async def test_register_returns_ack(harness: _Harness) -> None:
    stub = await harness.start()
    call = stub.Session(metadata=_auth(_CREDENTIAL))
    await call.write(_register_message())

    response = await call.read()

    assert response.WhichOneof("payload") == "register_ack"
    assert response.register_ack.heartbeat_interval.ToTimedelta() == _TIMEOUT / 3
    # The ack advertises the Worker-side per-transfer deadline (issue #874).
    assert response.register_ack.transfer_deadline.ToTimedelta() == _TRANSFER_DEADLINE
    assert response.correlation_id == "reg-1"
    snapshots = harness.registry.list_workers()
    assert len(snapshots) == 1
    assert snapshots[0].status is WorkerStatus.ONLINE
    await call.done_writing()


async def test_register_records_held_servers(harness: _Harness) -> None:
    # The register intake records the working sets the Worker reports it already
    # holds, with the generation each is at (issue #763), so the lifecycle layer can
    # skip the destructive hydrate on a same-worker restart only when fresh enough.
    stub = await harness.start()
    call = stub.Session(metadata=_auth(_CREDENTIAL))
    await call.write(_register_message(held_servers={"server-a": 5, "server-b": 0}))

    await call.read()

    worker = WorkerId(_WORKER_ID)
    assert harness.registry.held_generation(worker, "server-a") == 5
    assert harness.registry.held_generation(worker, "server-b") == 0
    assert harness.registry.held_generation(worker, "server-c") is None
    await call.done_writing()


async def test_missing_credential_is_rejected(harness: _Harness) -> None:
    await harness.start()

    code = await _terminal_code(harness, _auth(None))

    assert code == grpc.StatusCode.UNAUTHENTICATED
    assert harness.registry.list_workers() == []


async def test_wrong_credential_is_rejected(harness: _Harness) -> None:
    await harness.start()

    code = await _terminal_code(harness, _auth("wrong"))

    assert code == grpc.StatusCode.UNAUTHENTICATED


async def test_unencodable_configured_credential_rejects_every_session() -> None:
    # A configured credential with no UTF-8 encoding (a lone surrogate, as an
    # undecodable environment byte becomes) matches nothing: the session is
    # rejected as unauthenticated rather than raised (issue #3695).
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        worker_credential="\udcff",
    )
    try:
        await h.start()

        code = await _terminal_code(h, _auth(_CREDENTIAL))

        assert code == grpc.StatusCode.UNAUTHENTICATED
        assert h.registry.list_workers() == []
    finally:
        await h.stop()


class _Aborted(Exception):
    """Raised by :class:`_FakeContext.abort`, as the real ``abort`` raises."""


class _FakeContext:
    """A servicer context presenting ``metadata``; records the abort it gets.

    grpcio's client refuses a non-ASCII value under a non-``-bin`` key, but the
    server hands one through to ``invocation_metadata()`` from any other client
    (UTF-8, falling back to Latin-1), so the servicer is driven directly here.
    """

    def __init__(self, metadata: list[tuple[str, str]]) -> None:
        self._metadata = metadata
        self.aborted: tuple[grpc.StatusCode, str] | None = None

    def invocation_metadata(self) -> list[tuple[str, str]]:
        return self._metadata

    async def abort(self, code: grpc.StatusCode, details: str = "") -> None:
        self.aborted = (code, details)
        raise _Aborted


async def test_non_ascii_presented_credential_is_rejected() -> None:
    async def _requests() -> AsyncIterator[pb.WorkerMessage]:
        yield _register_message()

    registry = InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT)
    servicer = WorkerSessionServicer(
        registry=registry,
        clock=FakeClock(_T0),
        worker_credential=_CREDENTIAL,
        heartbeat_timeout=_TIMEOUT,
        transfer_deadline=_TRANSFER_DEADLINE,
        control_plane=ControlPlaneState(),
        state_sink=FakeServerStateSink(),
        real_time_events=RecordingRealTimeEvents(),
        database_ping=FakeDatabasePing(),
    )
    context = _FakeContext(_auth("\u00e9"))

    with pytest.raises(_Aborted):
        await anext(servicer.Session(_requests(), context))

    assert context.aborted == (
        grpc.StatusCode.UNAUTHENTICATED,
        "worker credential rejected",
    )
    assert registry.list_workers() == []


async def test_non_register_first_message_is_rejected(harness: _Harness) -> None:
    await harness.start()

    code = await _terminal_code(
        harness, _auth(_CREDENTIAL), message=_heartbeat_message()
    )

    assert code == grpc.StatusCode.FAILED_PRECONDITION


async def test_no_register_within_deadline_is_rejected(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A credentialed client that opens the stream and never sends Register must
    # not hold the RPC forever (issue #1700): the pre-registration wait is
    # bounded, and expiry aborts the stream with DEADLINE_EXCEEDED before any
    # registration state exists.
    import mc_server_dashboard_api.fleet.adapters.grpc_server as grpc_server_module

    monkeypatch.setattr(grpc_server_module, "_REGISTRATION_TIMEOUT_SECONDS", 0.2)
    stub = await harness.start()

    call = stub.Session(metadata=_auth(_CREDENTIAL))
    # Write nothing: the server's registration deadline must end the call.
    code = await asyncio.wait_for(call.code(), timeout=5)

    assert code == grpc.StatusCode.DEADLINE_EXCEEDED
    assert harness.registry.list_workers() == []


async def test_half_close_before_register_is_rejected(harness: _Harness) -> None:
    # A client that half-closes without ever sending Register is rejected with
    # FAILED_PRECONDITION ("stream closed before Register"). Pinned alongside
    # issue #1700: the bounded pre-registration wait wraps the first __anext__
    # in a task, and StopAsyncIteration must still propagate through it.
    stub = await harness.start()

    call = stub.Session(metadata=_auth(_CREDENTIAL))
    await call.done_writing()
    code = await asyncio.wait_for(call.code(), timeout=5)

    assert code == grpc.StatusCode.FAILED_PRECONDITION
    assert harness.registry.list_workers() == []


async def test_non_uuid_worker_id_is_rejected(harness: _Harness) -> None:
    # assigned_worker_id is a UUID column, so a non-UUID worker id is rejected
    # at registration (issue #99) instead of silently breaking downstream.
    stub = await harness.start()
    call = stub.Session(metadata=_auth(_CREDENTIAL))
    await call.write(_register_message(worker_id="worker-1"))

    with pytest.raises(aio.AioRpcError) as exc:
        await call.read()
    assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert "uuid" in exc.value.details().lower()
    assert harness.registry.list_workers() == []


# A hand-configured worker id may spell its UUID non-canonically (issue #3542); the
# API keys the Worker by the canonical lowercase-hyphenated form, the form every
# stored reference (assigned_worker_id, drain) carries.
_CANONICAL_ID = "abcdef01-2345-6789-abcd-ef0123456789"
_UPPER_ID = _CANONICAL_ID.upper()
_HYPHENLESS_ID = _CANONICAL_ID.replace("-", "")


@pytest.mark.parametrize("spelling", [_UPPER_ID, _HYPHENLESS_ID])
async def test_non_canonical_worker_id_is_keyed_canonically(
    harness: _Harness, spelling: str
) -> None:
    stub = await harness.start()
    call = stub.Session(metadata=_auth(_CREDENTIAL))
    await call.write(_register_message(worker_id=spelling))
    await call.read()  # ack

    assert [w.id for w in harness.registry.list_workers()] == [WorkerId(_CANONICAL_ID)]
    await call.done_writing()


async def test_non_canonical_worker_id_matches_canonical_assignments() -> None:
    # The tally reads assigned_worker_id (a uuid column), so it is keyed canonically.
    sink = FakeServerStateSink(
        running_ids={_CANONICAL_ID: {"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}}
    )
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
    )
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message(worker_id=_UPPER_ID))
        await call.read()  # ack

        for _ in range(100):
            snapshots = h.registry.list_workers()
            if snapshots and snapshots[0].assigned_count == 1:
                break
            await asyncio.sleep(0.01)

        assert sink.counted_for == [_CANONICAL_ID]
        assert h.registry.list_workers()[0].assigned_count == 1
        await call.done_writing()
    finally:
        await h.stop()


async def test_reconnect_with_other_spelling_is_the_same_drained_worker(
    harness: _Harness,
) -> None:
    stub = await harness.start()
    call_a = stub.Session(metadata=_auth(_CREDENTIAL))
    await call_a.write(_register_message(worker_id=_UPPER_ID))
    await call_a.read()  # ack
    # The drain is recorded under the canonical id, as the REST surface keys it.
    assert harness.registry.set_draining(WorkerId(_CANONICAL_ID), True)
    await call_a.done_writing()
    while await call_a.read() is not aio.EOF:
        pass

    call_b = stub.Session(metadata=_auth(_CREDENTIAL))
    await call_b.write(_register_message(worker_id=_HYPHENLESS_ID))
    await call_b.read()  # ack

    snapshots = harness.registry.list_workers()
    assert [w.id for w in snapshots] == [WorkerId(_CANONICAL_ID)]
    assert snapshots[0].status is WorkerStatus.DRAINING
    await call_b.done_writing()


async def test_heartbeat_refreshes_liveness(harness: _Harness) -> None:
    stub = await harness.start()
    call = stub.Session(metadata=_auth(_CREDENTIAL))
    await call.write(_register_message())
    await call.read()  # ack

    # Advance the server clock past the window, then heartbeat to refresh it.
    harness.clock.set(_T0 + dt.timedelta(seconds=25))
    await call.write(_heartbeat_message())
    # Let the server process the heartbeat before asserting.
    await _drain_until_heartbeat_recorded(harness)

    snapshot = harness.registry.list_workers()[0]
    assert snapshot.last_heartbeat_at == _T0 + dt.timedelta(seconds=25)
    assert snapshot.status is WorkerStatus.ONLINE
    await call.done_writing()


async def test_disconnect_marks_offline(harness: _Harness) -> None:
    stub = await harness.start()
    call = stub.Session(metadata=_auth(_CREDENTIAL))
    await call.write(_register_message())
    await call.read()  # ack

    await call.done_writing()
    # Drain the server response stream to completion so the handler's finally
    # block (mark_disconnected) has run.
    while await call.read() is not aio.EOF:
        pass

    snapshot = harness.registry.list_workers()[0]
    assert snapshot.status is WorkerStatus.OFFLINE


async def test_stale_session_teardown_keeps_reconnected_worker_online(
    harness: _Harness,
) -> None:
    # Session A registers the worker; a real client reconnect (PR #84 backoff)
    # re-registers the same id on Session B while A's teardown is still pending.
    stub = await harness.start()
    call_a = stub.Session(metadata=_auth(_CREDENTIAL))
    await call_a.write(_register_message())
    await call_a.read()  # ack

    call_b = stub.Session(metadata=_auth(_CREDENTIAL))
    await call_b.write(_register_message())
    await call_b.read()  # ack

    # Session A tears down after B is the current Session.
    await call_a.done_writing()
    while await call_a.read() is not aio.EOF:
        pass

    # The freshly re-registered Worker (Session B) must stay ONLINE.
    snapshot = harness.registry.list_workers()[0]
    assert snapshot.status is WorkerStatus.ONLINE
    await call_b.done_writing()


async def test_superseded_session_heartbeat_leaves_the_dropped_worker_offline() -> None:
    # Session A is superseded by Session B, and B's stream ends. A's reader runs
    # until A's watchdog next polls (timeout/3), so a heartbeat still arrives on
    # A. It must not revive the record B's teardown marked disconnected, nor move
    # the drop instant the reconciler's grace runs from (issue #3983).
    clock = FakeClock(_T0)
    h = _Harness(InMemoryWorkerRegistry(clock=clock, heartbeat_timeout=_TIMEOUT), clock)
    try:
        stub = await h.start()
        call_a = stub.Session(metadata=_auth(_CREDENTIAL))
        await call_a.write(_register_message())
        await call_a.read()  # ack

        registered_b = _T0 + dt.timedelta(seconds=1)
        clock.set(registered_b)
        call_b = stub.Session(metadata=_auth(_CREDENTIAL))
        await call_b.write(_register_message())
        await call_b.read()  # ack

        dropped = _T0 + dt.timedelta(seconds=2)
        clock.set(dropped)
        await call_b.done_writing()
        await _drain_to_eof(call_b)

        clock.set(_T0 + dt.timedelta(seconds=3))
        await call_a.write(_heartbeat_message())
        # A's reader handles its messages in order, so once A's stream is drained
        # the heartbeat has been read.
        await call_a.done_writing()
        await _drain_to_eof(call_a)

        snapshot = h.registry.get(WorkerId(_WORKER_ID))
        assert snapshot is not None
        assert snapshot.status is WorkerStatus.OFFLINE
        assert snapshot.offline_since == dropped
        assert snapshot.last_heartbeat_at == registered_b
    finally:
        await h.stop()


def _status_message(
    server_id: str, state: "pb.ServerState.ValueType"
) -> pb.WorkerMessage:
    return pb.WorkerMessage(
        event=pb.Event(server_id=server_id, status_change=pb.StatusChange(state=state))
    )


async def test_status_change_reconciles_observed_state(harness: _Harness) -> None:
    stub = await harness.start()
    call = stub.Session(metadata=_auth(_CREDENTIAL))
    await call.write(_register_message())
    await call.read()  # ack

    server_id = "11111111-1111-1111-1111-111111111111"
    await call.write(_status_message(server_id, pb.SERVER_STATE_RUNNING))
    for _ in range(100):
        if harness.state_sink.observed:
            break
        await __import__("asyncio").sleep(0.01)

    assert harness.state_sink.observed == [(server_id, _WORKER_ID, "running")]
    await call.done_writing()


async def test_status_change_unknown_is_ingested_not_dropped(
    harness: _Harness,
) -> None:
    # Issue #2474: a Worker that cannot confirm an instance's fate reports
    # SERVER_STATE_UNKNOWN rather than guessing running or stopped. The ingest must
    # carry it to the sink as "unknown" -- the value the sink parses into
    # ObservedState.UNKNOWN -- instead of dropping it as an unmapped state.
    stub = await harness.start()
    call = stub.Session(metadata=_auth(_CREDENTIAL))
    await call.write(_register_message())
    await call.read()  # ack

    server_id = "11111111-1111-1111-1111-111111111111"
    await call.write(_status_message(server_id, pb.SERVER_STATE_UNKNOWN))
    for _ in range(100):
        if harness.state_sink.observed:
            break
        await asyncio.sleep(0.01)

    assert harness.state_sink.observed == [(server_id, _WORKER_ID, "unknown")]
    await call.done_writing()


def test_every_wire_server_state_has_an_ingest_mapping() -> None:
    # The ingest silently DROPS a StatusChange whose state it cannot map, so a wire
    # value added without a _STATE_BY_PROTO entry discards a Worker's report --
    # exactly how SERVER_STATE_UNKNOWN was lost before issue #2474. Only UNSPECIFIED
    # stays unmapped: it is the proto zero value, not a state a Worker asserts.
    unmapped = set(pb.ServerState.values()) - set(_STATE_BY_PROTO)
    assert unmapped == {pb.SERVER_STATE_UNSPECIFIED}


async def test_disconnect_marks_worker_servers_unknown(harness: _Harness) -> None:
    stub = await harness.start()
    call = stub.Session(metadata=_auth(_CREDENTIAL))
    await call.write(_register_message())
    await call.read()  # ack

    await call.done_writing()
    while await call.read() is not aio.EOF:
        pass

    assert harness.state_sink.unknown_for == [_WORKER_ID]


async def test_stale_session_teardown_does_not_mark_servers_unknown(
    harness: _Harness,
) -> None:
    # Session A registers the worker; the worker reconnects on Session B (PR #84
    # backoff) before A's teardown runs. A's delayed teardown must NOT stamp the
    # live worker's servers observed=unknown, or it clobbers Session B's state
    # (issue #775, CONTROL_PLANE.md Section 4.4).
    stub = await harness.start()
    call_a = stub.Session(metadata=_auth(_CREDENTIAL))
    await call_a.write(_register_message())
    await call_a.read()  # ack

    call_b = stub.Session(metadata=_auth(_CREDENTIAL))
    await call_b.write(_register_message())
    await call_b.read()  # ack

    # Session A tears down after B is the current Session.
    await call_a.done_writing()
    while await call_a.read() is not aio.EOF:
        pass

    # The stale teardown must not have marked the live worker's servers unknown.
    assert harness.state_sink.unknown_for == []
    await call_b.done_writing()


async def test_poison_status_change_does_not_end_session(harness: _Harness) -> None:
    # A handler failure on ONE StatusChange must not tear down the whole worker
    # session; the bad event is logged-and-skipped and a later StatusChange is
    # still reconciled (issue #776).
    bad_server = "33333333-3333-3333-3333-333333333333"
    good_server = "11111111-1111-1111-1111-111111111111"
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=FakeServerStateSink(fail_observed_for={bad_server}),
    )
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message())
        await call.read()  # ack

        # The first StatusChange raises inside the handler; the stream survives.
        await call.write(_status_message(bad_server, pb.SERVER_STATE_RUNNING))
        await call.write(_status_message(good_server, pb.SERVER_STATE_RUNNING))
        for _ in range(100):
            if h.state_sink.observed:
                break
            await asyncio.sleep(0.01)

        # The good event was reconciled, and the worker is still ONLINE: the
        # poison event did not drop the stream.
        assert h.state_sink.observed == [(good_server, _WORKER_ID, "running")]
        assert h.registry.list_workers()[0].status is WorkerStatus.ONLINE
        await call.done_writing()
    finally:
        await h.stop()


async def _drain_to_eof(call: aio.StreamStreamCall) -> None:
    """Drive the response stream to completion so the handler's finally runs."""

    while await call.read() is not aio.EOF:
        pass


async def test_consecutive_handler_failures_end_session(harness: _Harness) -> None:
    # A permanently poisoned sink makes every StatusChange handler raise. The
    # per-message log-and-continue (issue #776) is bounded (issue #807): after the
    # cap of consecutive failures, the next failure ends the session, so teardown
    # marks the worker disconnected and writes its servers unknown.
    server = "11111111-1111-1111-1111-111111111111"
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=FakeServerStateSink(always_fail_observed=True),
    )
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message())
        await call.read()  # ack

        # Send one more than the cap: the first _MAX failures are skipped, the
        # next one is terminal and ends the session.
        for _ in range(_MAX_CONSECUTIVE_HANDLER_FAILURES + 1):
            await call.write(_status_message(server, pb.SERVER_STATE_RUNNING))
        await call.done_writing()
        await _drain_to_eof(call)

        snapshot = h.registry.list_workers()[0]
        assert snapshot.status is WorkerStatus.OFFLINE
        assert h.state_sink.unknown_for == [_WORKER_ID]
    finally:
        await h.stop()


async def test_success_resets_consecutive_failure_counter(harness: _Harness) -> None:
    # A success mid-streak resets the consecutive-failure counter, so transient
    # blips never accumulate to the cap (issue #807). Drive the sink to the brink
    # (cap-1 failures), let one succeed, then fail again: the session must stay
    # alive because the streak was reset rather than continuing toward the cap.
    good_server = "11111111-1111-1111-1111-111111111111"
    bad_server = "33333333-3333-3333-3333-333333333333"

    class _CountingSink(FakeServerStateSink):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def record_observed_state(
            self, *, server_id: str, worker_id: str, state: str, **frame: Any
        ) -> bool:
            self.calls += 1
            if server_id == bad_server:
                raise RuntimeError("observed-state sink unavailable")
            return await super().record_observed_state(
                server_id=server_id, worker_id=worker_id, state=state, **frame
            )

    sink = _CountingSink()
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
    )
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message())
        await call.read()  # ack

        # cap-1 failures (one short of terminal), one success (resets the
        # counter), then cap-1 more failures. Without the reset this would total
        # well past the cap and end the session.
        for _ in range(_MAX_CONSECUTIVE_HANDLER_FAILURES - 1):
            await call.write(_status_message(bad_server, pb.SERVER_STATE_RUNNING))
        await call.write(_status_message(good_server, pb.SERVER_STATE_RUNNING))
        for _ in range(_MAX_CONSECUTIVE_HANDLER_FAILURES - 1):
            await call.write(_status_message(bad_server, pb.SERVER_STATE_RUNNING))

        # Wait until every sent message has been handled (the good one recorded,
        # the bad ones raised) before asserting the session is still alive.
        expected_calls = 2 * (_MAX_CONSECUTIVE_HANDLER_FAILURES - 1) + 1
        for _ in range(200):
            if sink.calls >= expected_calls:
                break
            await asyncio.sleep(0.01)

        assert sink.observed == [(good_server, _WORKER_ID, "running")]
        assert h.registry.list_workers()[0].status is WorkerStatus.ONLINE
        assert h.state_sink.unknown_for == []
        await call.done_writing()
    finally:
        await h.stop()


async def test_cancelled_session_cancels_pending_outbound_get() -> None:
    # When the Session generator is cancelled mid-await (an abrupt RPC cancel),
    # the finally must cancel the pending outbound.get() task so it does not
    # await an orphaned queue forever (issue #788). The generator is iterated by
    # a background task so it reaches and parks on outbound.get(); cancelling
    # that task drives the cancel into the await, and an empty waiter deque on
    # the queue after teardown proves the pending get() was cancelled.
    async def _requests() -> AsyncIterator[pb.WorkerMessage]:
        yield _register_message()
        # Keep the inbound stream open so the generator parks on outbound.get().
        await asyncio.Event().wait()

    class _Ctx:
        def invocation_metadata(self) -> list[tuple[str, str]]:
            return _auth(_CREDENTIAL)

        async def abort(self, *_args: object) -> None:  # pragma: no cover
            raise AssertionError("unexpected abort")

    registry = InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT)
    control_plane = ControlPlaneState()
    servicer = WorkerSessionServicer(
        registry=registry,
        clock=FakeClock(_T0),
        worker_credential=_CREDENTIAL,
        heartbeat_timeout=_TIMEOUT,
        transfer_deadline=_TRANSFER_DEADLINE,
        control_plane=control_plane,
        state_sink=FakeServerStateSink(),
        real_time_events=RecordingRealTimeEvents(),
        database_ping=FakeDatabasePing(),
    )

    gen = servicer.Session(_requests(), _Ctx())

    async def _drain() -> None:
        async for _ in gen:
            pass

    tasks_before = asyncio.all_tasks()
    driver = asyncio.ensure_future(_drain())
    # Wait until the generator has opened its outbound queue and parked on the
    # body's outbound.get(). The queue's presence in the control plane plus a
    # settle sleep is enough to know the body has reached the await — we do not
    # inspect CPython-private queue internals (issue #807).
    for _ in range(200):
        if control_plane.outbound_for(WorkerId(_WORKER_ID)) is not None:
            break
        await asyncio.sleep(0.01)
    assert control_plane.outbound_for(WorkerId(_WORKER_ID)) is not None, (
        "the session should have opened its outbound queue"
    )
    await asyncio.sleep(0.05)  # let the body reach outbound.get()

    # Abrupt RPC cancel: cancel the consumer driving the generator.
    driver.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await driver

    # The pending outbound.get() must have been cancelled by the generator's
    # finally: an uncancelled get() would leak a Task awaiting the orphaned
    # queue forever (issue #788). Observe that by the absence of leftover tasks
    # rather than the queue's private waiter deque — settle the loop, then diff
    # the task set; a leaked get() would still be pending here (issue #807).
    await asyncio.sleep(0.05)
    leaked = {
        task
        for task in asyncio.all_tasks() - tasks_before
        if task is not asyncio.current_task() and not task.done()
    }
    assert not leaked, f"the session leaked pending tasks: {leaked}"


async def test_reregister_rebuilds_assignment_count_from_tally() -> None:
    # The Worker is reported (via the authoritative tally) to be running 2 servers;
    # on (re)register the registry must rebuild its load to 2, not the reset 0.
    sink = FakeServerStateSink(
        running_ids={
            _WORKER_ID: {
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            }
        }
    )
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
    )
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message())
        await call.read()  # ack

        for _ in range(100):
            snapshots = h.registry.list_workers()
            if snapshots and snapshots[0].assigned_count == 2:
                break
            await __import__("asyncio").sleep(0.01)

        assert sink.counted_for == [_WORKER_ID]
        assert h.registry.list_workers()[0].assigned_count == 2
        await call.done_writing()
    finally:
        await h.stop()


async def test_rebuild_keeps_a_confirm_that_lands_during_the_tally_read() -> None:
    # #844: _rebuild_assignments reads the DB tally across an await; a placement that
    # commits AND confirms in that window must not be overwritten by the stale tally.
    # The servicer snapshots the confirm epoch BEFORE the await, so set_assignment
    # keeps the just-confirmed row even though it is absent from the (empty) tally.
    registry = InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT)
    session = registry.register(make_worker(worker_id=_WORKER_ID, at=_T0))
    worker = WorkerId(_WORKER_ID)
    server_id = "33333333-3333-3333-3333-333333333333"

    class _ConfirmDuringReadSink(FakeServerStateSink):
        async def running_assignment_ids(self, *, worker_id: str) -> dict[str, int]:
            # The await window: a concurrent placement commits and confirms here,
            # AFTER the servicer snapshotted the epoch but BEFORE set_assignment runs.
            registry.reserve(worker, server_id, 512)
            registry.increment_assignment(worker, server_id)
            await asyncio.sleep(0)
            # The DB tally does not yet see the just-landed commit.
            return {}

    servicer = WorkerSessionServicer(
        registry=registry,
        clock=FakeClock(_T0),
        worker_credential=_CREDENTIAL,
        heartbeat_timeout=_TIMEOUT,
        transfer_deadline=_TRANSFER_DEADLINE,
        control_plane=ControlPlaneState(),
        state_sink=_ConfirmDuringReadSink(),
        real_time_events=RecordingRealTimeEvents(),
        database_ping=FakeDatabasePing(),
    )

    await servicer._rebuild_assignments(worker, session)

    # The +1 (and its committed memory) survived the stale-tally rebuild.
    assert registry.list_workers()[0].assigned_count == 1
    assert registry.committed_memory_mb(worker) == 512


async def _drain_until_heartbeat_recorded(harness: _Harness) -> None:
    import asyncio

    for _ in range(100):
        snapshot = harness.registry.list_workers()[0]
        if snapshot.last_heartbeat_at != _T0:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("heartbeat was not recorded in time")


async def _wait_for_heartbeat_at(harness: _Harness, expected: dt.datetime) -> None:
    for _ in range(200):
        if harness.registry.list_workers()[0].last_heartbeat_at == expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("heartbeat was not recorded in time")


async def _wait_for_published(harness: _Harness, count: int) -> None:
    import asyncio

    for _ in range(100):
        if len(harness.real_time_events.published) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("event was not published in time")


async def _wait_for_status_frame(harness: _Harness) -> None:
    for _ in range(100):
        if harness.state_sink.status_frames:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("status change did not reach the sink in time")


_SERVER_ID = "11111111-1111-1111-1111-111111111111"


async def test_status_change_hands_its_live_frame_to_the_sink(
    harness: _Harness,
) -> None:
    """The sink relays a status change, after its commit (issue #3212).

    The servicer hands the frame over with the state and publishes nothing
    itself: a status event published here could not be ordered against the
    commit, nor against the observed-state writes that are not Worker reports.
    """
    stub = await harness.start()
    call = stub.Session(metadata=_auth(_CREDENTIAL))
    await call.write(_register_message())
    await call.read()  # ack

    await call.write(_status_message(_SERVER_ID, pb.SERVER_STATE_RUNNING))
    await _wait_for_status_frame(harness)

    assert harness.state_sink.status_frames == [
        (_SERVER_ID, {"state": "running", "detail": "", "reason": ""}, None)
    ]
    assert harness.real_time_events.published == []
    await call.done_writing()


@pytest.mark.parametrize(
    ("crash_reason", "expected"),
    [
        (pb.CRASH_REASON_UNSPECIFIED, ""),
        (pb.CRASH_REASON_FORGE_INSTALL_FAILED, "forge_install_failed"),
        (
            pb.CRASH_REASON_FORGE_INSTALL_OUT_OF_MEMORY,
            "forge_install_out_of_memory",
        ),
        (
            pb.CRASH_REASON_FORGE_INSTALL_JAVA_INCOMPATIBLE,
            "forge_install_java_incompatible",
        ),
    ],
)
async def test_crash_reason_rides_the_live_frame_beside_the_detail(
    harness: _Harness, crash_reason: "pb.CrashReason.ValueType", expected: str
) -> None:
    """A crash the Worker classified reaches subscribers by name (issue #1093).

    The reason rides the live status frame exactly as the detail does; an
    unclassified crash carries the empty string, never a missing key.
    """
    stub = await harness.start()
    call = stub.Session(metadata=_auth(_CREDENTIAL))
    await call.write(_register_message())
    await call.read()  # ack

    await call.write(
        pb.WorkerMessage(
            event=pb.Event(
                server_id=_SERVER_ID,
                status_change=pb.StatusChange(
                    state=pb.SERVER_STATE_CRASHED,
                    detail="boom",
                    crash_reason=crash_reason,
                ),
            )
        )
    )
    await _wait_for_status_frame(harness)

    _, payload, _ = harness.state_sink.status_frames[0]
    assert payload == {"state": "crashed", "detail": "boom", "reason": expected}
    await call.done_writing()


async def test_log_line_is_published_to_real_time_events(harness: _Harness) -> None:
    stub = await harness.start()
    call = stub.Session(metadata=_auth(_CREDENTIAL))
    await call.write(_register_message())
    await call.read()  # ack

    await call.write(
        pb.WorkerMessage(
            event=pb.Event(
                server_id=_SERVER_ID,
                log_line=pb.LogLine(line="hello", stream=pb.LOG_STREAM_STDOUT),
            )
        )
    )
    await _wait_for_published(harness, 1)

    server_id, event = harness.real_time_events.published[0]
    assert server_id == _SERVER_ID
    assert event.stream is EventStream.LOG
    assert event.payload == {"line": "hello", "stream": "stdout"}
    await call.done_writing()


async def test_emitted_at_is_propagated_to_the_status_frame(
    harness: _Harness,
) -> None:
    stub = await harness.start()
    call = stub.Session(metadata=_auth(_CREDENTIAL))
    await call.write(_register_message())
    await call.read()  # ack

    emitted = dt.datetime(2026, 6, 3, 12, 0, 0, tzinfo=dt.timezone.utc)
    message = _status_message(_SERVER_ID, pb.SERVER_STATE_RUNNING)
    message.emitted_at.FromDatetime(emitted)
    await call.write(message)
    await _wait_for_status_frame(harness)

    _server_id, _payload, emitted_at = harness.state_sink.status_frames[0]
    assert emitted_at == emitted
    await call.done_writing()


async def test_unset_emitted_at_falls_back_to_none(harness: _Harness) -> None:
    stub = await harness.start()
    call = stub.Session(metadata=_auth(_CREDENTIAL))
    await call.write(_register_message())
    await call.read()  # ack

    # No emitted_at set on the message: the relayed event carries None, so the
    # transport falls back to receive time.
    await call.write(_status_message(_SERVER_ID, pb.SERVER_STATE_RUNNING))
    await _wait_for_status_frame(harness)

    _server_id, _payload, emitted_at = harness.state_sink.status_frames[0]
    assert emitted_at is None
    await call.done_writing()


async def test_metrics_is_published_to_real_time_events(harness: _Harness) -> None:
    stub = await harness.start()
    call = stub.Session(metadata=_auth(_CREDENTIAL))
    await call.write(_register_message())
    await call.read()  # ack

    await call.write(
        pb.WorkerMessage(
            event=pb.Event(
                server_id=_SERVER_ID,
                metrics=pb.Metrics(cpu_millis=1500, memory_bytes=2048, player_count=3),
            )
        )
    )
    await _wait_for_published(harness, 1)

    server_id, event = harness.real_time_events.published[0]
    assert server_id == _SERVER_ID
    assert event.stream is EventStream.METRICS
    assert event.payload == {
        "cpu_millis": 1500,
        "memory_bytes": 2048,
        "player_count": 3,
    }
    await call.done_writing()


# ---------------------------------------------------------------------------
# Heartbeat-timeout enforcement (issue #1600)
# ---------------------------------------------------------------------------

# A short timeout keeps the watchdog poll (timeout/3) under ~0.1 s so the
# session-end detection is fast enough for a test. The shared clock between
# registry and servicer lets us advance liveness deterministically while the
# watchdog's asyncio.sleep still runs at real-time pace.
_SHORT_TIMEOUT = dt.timedelta(seconds=0.3)


def _shared_clock_harness() -> _Harness:
    """Harness whose registry and servicer share the same FakeClock.

    The default harness fixture gives each its own clock. The watchdog
    detects a lapse by reading ``registry.get()`` which derives liveness
    from the *registry's* clock; advancing one clock must also advance the
    other for the watchdog to see OFFLINE, so a shared instance is needed.
    """

    clock = FakeClock(_T0)
    return _Harness(
        InMemoryWorkerRegistry(clock=clock, heartbeat_timeout=_SHORT_TIMEOUT),
        clock,
        heartbeat_timeout=_SHORT_TIMEOUT,
    )


async def test_heartbeat_lapse_ends_session_and_marks_servers_unknown() -> None:
    h = _shared_clock_harness()
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message())
        await call.read()  # ack

        # Advance the shared clock past the liveness window; the watchdog's
        # next poll (at most ~0.1 s real time) will see the worker OFFLINE.
        h.clock.set(_T0 + _SHORT_TIMEOUT + dt.timedelta(seconds=1))
        await _drain_to_eof(call)

        snapshot = h.registry.list_workers()[0]
        assert snapshot.status is WorkerStatus.OFFLINE
        assert h.state_sink.unknown_for == [_WORKER_ID]
    finally:
        await h.stop()


async def test_refreshed_heartbeats_keep_session_alive() -> None:
    h = _shared_clock_harness()
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message())
        await call.read()  # ack

        # Advance the clock to just under the timeout multiple times,
        # heartbeating each time. The session must stay alive. The watchdog now
        # polls at ~0.1 s (timeout/3), so wait for each heartbeat to be recorded
        # before advancing again: that keeps now - last_heartbeat at one 0.2 s
        # step (< the 0.3 s timeout) at every poll, so the test is deterministic
        # regardless of scheduling under -n auto load (issue #1732).
        for i in range(1, 4):
            at = _T0 + dt.timedelta(seconds=0.2 * i)
            h.clock.set(at)
            await call.write(_heartbeat_message())
            await _wait_for_heartbeat_at(h, at)

        # The worker should still be online after sustained heartbeats.
        snapshot = h.registry.list_workers()[0]
        assert snapshot.status is WorkerStatus.ONLINE
        assert h.state_sink.unknown_for == []
        await call.done_writing()
    finally:
        await h.stop()


async def test_watchdog_ends_superseded_session() -> None:
    # Register on call A, re-register on call B; verify stale session A's
    # watchdog exits (is_current_session → False) without clobbering B.
    # The clock is NOT advanced: A's watchdog exits on the session check
    # alone, and B's liveness stays within the timeout.
    h = _shared_clock_harness()
    try:
        stub = await h.start()
        call_a = stub.Session(metadata=_auth(_CREDENTIAL))
        await call_a.write(_register_message())
        await call_a.read()  # ack

        call_b = stub.Session(metadata=_auth(_CREDENTIAL))
        await call_b.write(_register_message())
        await call_b.read()  # ack

        # Session A's watchdog polls after ~0.1 s, sees its session is
        # superseded (is_current_session returns False), and exits. That
        # ends the Session generator for A through the normal teardown.
        await _drain_to_eof(call_a)

        # Session B's worker must stay ONLINE: the stale watchdog exited
        # without marking disconnected or marking servers unknown.
        snapshot = h.registry.list_workers()[0]
        assert snapshot.status is WorkerStatus.ONLINE
        assert h.state_sink.unknown_for == []
        await call_b.done_writing()
    finally:
        await h.stop()


def test_keepalive_options_derive_from_heartbeat_timeout() -> None:
    timeout = dt.timedelta(seconds=30)
    options = _keepalive_options(timeout)
    options_dict = dict(options)
    assert options_dict["grpc.keepalive_time_ms"] == 30_000
    assert options_dict["grpc.keepalive_timeout_ms"] == 20_000
    assert options_dict["grpc.http2.max_pings_without_data"] == 0


def test_keepalive_options_permit_worker_client_pings() -> None:
    """The server must permit the Worker's client-side keepalive cadence.

    The Worker dials with keepalive Time=20s (issue #1709); C-core's default
    ping-strike enforcement (min interval 5 min, 2 strikes) would answer that
    cadence with GOAWAY ENHANCE_YOUR_CALM. The permitted floor stays at half
    the Worker's cadence for timing-skew margin, and pings are permitted
    without active calls (the Worker probes between Session streams).
    """
    options_dict = dict(_keepalive_options(dt.timedelta(seconds=30)))
    assert options_dict["grpc.http2.min_ping_interval_without_data_ms"] == 10_000
    assert options_dict["grpc.keepalive_permit_without_calls"] == 1


# ---- unknown held server ids (issue #924) --------------------------------

_SERVER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_SERVER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_SERVER_C = "cccccccc-cccc-cccc-cccc-cccccccccccc"


async def test_register_ack_carries_unknown_held_server_ids() -> None:
    """RegisterAck returns the subset of held servers that no longer exist."""
    # Only _SERVER_A is known; _SERVER_B is deleted.
    sink = FakeServerStateSink(known_server_ids={_SERVER_A})
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
    )
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message(held_servers={_SERVER_A: 1, _SERVER_B: 2}))
        response = await call.read()

        unknown = list(response.register_ack.unknown_held_server_ids)
        assert unknown == [_SERVER_B]
        await call.done_writing()
    finally:
        await h.stop()


async def test_register_ack_carries_an_unknown_unheld_scratch_server_id() -> None:
    """A deleted id the Worker lists as an unheld scratch is in the ack (#3794).

    Its scratch dir holds no working set, so the Worker lists it apart from the
    held ones. The API checks it for a delete like a held id, but never records it
    as held: a start must not skip its hydrate for a world the Worker does not hold.
    """
    # _SERVER_B is deleted; _SERVER_A (held) and _SERVER_C (unheld) are known.
    sink = FakeServerStateSink(known_server_ids={_SERVER_A, _SERVER_C})
    registry = InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT)
    h = _Harness(registry, FakeClock(_T0), state_sink=sink)
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(
            _register_message(
                held_servers={_SERVER_A: 1},
                unheld_scratch_server_ids=[_SERVER_B, _SERVER_C],
            )
        )
        response = await call.read()

        assert list(response.register_ack.unknown_held_server_ids) == [_SERVER_B]
        worker = WorkerId(_WORKER_ID)
        assert registry.held_generation(worker, _SERVER_A) == 1
        assert registry.held_generation(worker, _SERVER_B) is None
        assert registry.held_generation(worker, _SERVER_C) is None
        await call.done_writing()
    finally:
        await h.stop()


async def test_register_ack_empty_unknown_when_all_known() -> None:
    """RegisterAck returns an empty list when all held servers still exist."""
    sink = FakeServerStateSink(known_server_ids={_SERVER_A, _SERVER_B})
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
    )
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message(held_servers={_SERVER_A: 1, _SERVER_B: 2}))
        response = await call.read()

        assert list(response.register_ack.unknown_held_server_ids) == []
        await call.done_writing()
    finally:
        await h.stop()


async def test_register_ack_sink_error_yields_empty_unknown_list() -> None:
    """A sink exception yields an empty list; registration still succeeds."""

    class _FailingSink(FakeServerStateSink):
        async def existing_server_ids(self, *, server_ids: list[str]) -> set[str]:
            raise RuntimeError("DB down")

    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=_FailingSink(),
    )
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message(held_servers={_SERVER_A: 1}))
        response = await call.read()

        # Fail-safe: empty list rather than misclassifying.
        assert list(response.register_ack.unknown_held_server_ids) == []
        await call.done_writing()
    finally:
        await h.stop()


async def test_register_ack_no_held_servers_yields_empty_unknown() -> None:
    """When the Worker advertises no held servers, unknown is empty."""
    sink = FakeServerStateSink(known_server_ids=set())
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
    )
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message())  # no held_servers
        response = await call.read()

        assert list(response.register_ack.unknown_held_server_ids) == []
        await call.done_writing()
    finally:
        await h.stop()


async def test_register_ack_unparseable_held_id_treated_as_existing() -> None:
    """An unparseable (non-UUID) held ID must NOT be classified as deleted.

    ScanHeldServers advertises any content-bearing dir in the scratch root, so
    a safety copy like '<id>.bak' could appear. Misclassifying it as deleted
    would cause the worker to RemoveAll it.
    """
    # "not-a-uuid" is unparseable; _SERVER_A is a valid UUID-shaped id that
    # is known. Neither should appear in the unknown list.
    sink = FakeServerStateSink(known_server_ids={_SERVER_A})
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
    )
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(
            _register_message(held_servers={_SERVER_A: 1, "not-a-uuid": 3})
        )
        response = await call.read()

        # "not-a-uuid" is unparseable so it must be treated as existing (safe),
        # not classified as deleted.
        assert list(response.register_ack.unknown_held_server_ids) == []
        await call.done_writing()
    finally:
        await h.stop()


async def test_a_delete_reported_during_the_held_lookup_reaches_the_worker() -> None:
    """A delete that commits while a registration looks up the held servers is
    still reported to that Worker (issue #3679).

    The lookup read the server before the delete committed, so the ack does not
    list it as deleted. The delete's report to the connected Workers is then the
    only thing that reaches this one before its next registration, and it can
    only if the session already takes commands while the lookup runs.
    """

    lookup_read = asyncio.Event()
    delete_reported = asyncio.Event()

    class _DeleteDuringLookupSink(FakeServerStateSink):
        async def existing_server_ids(self, *, server_ids: list[str]) -> set[str]:
            existing = await super().existing_server_ids(server_ids=server_ids)
            lookup_read.set()
            await delete_reported.wait()
            return existing

    state = ControlPlaneState()
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=_DeleteDuringLookupSink(known_server_ids={_SERVER_A}),
        control_plane=state,
    )
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message(held_servers={_SERVER_A: 1}))
        await asyncio.wait_for(lookup_read.wait(), timeout=5)

        await GrpcControlPlane(
            state, clock=FakeClock(_T0), timeout_seconds=5.0
        ).dispatch_fire_and_forget(
            worker_id=WorkerId(_WORKER_ID),
            server_id=_SERVER_A,
            command=ReclaimDeletedServerCommand(),
        )
        delete_reported.set()

        ack = await call.read()
        assert list(ack.register_ack.unknown_held_server_ids) == []
        msg = await asyncio.wait_for(call.read(), timeout=5)
        assert msg.api_command.WhichOneof("command") == "reclaim_deleted_server"
        assert msg.api_command.server_id == _SERVER_A
        await call.write(
            pb.WorkerMessage(
                correlation_id=msg.api_command.command_id,
                command_result=pb.CommandResult(success=True),
            )
        )
        await call.done_writing()
    finally:
        await h.stop()


async def test_a_session_superseded_during_the_held_lookup_ends_like_a_later_one() -> (
    None
):
    """A session a reconnect supersedes during its held lookup is not aborted
    (issue #3679).

    The lookup runs after ``open_session``, so by then the session owns the
    Worker's queue the way any open session does, and a supersede there is handled
    as it is later on: the newer session's ``open_session`` retires what the older
    one had queued, and the older one's teardown is session-guarded. It yields its
    ack on its own stream, sends nothing it had queued, and ends OK; the newer
    session stays current and carries what follows.
    """

    lookup_started = asyncio.Event()
    release_lookup = asyncio.Event()

    class _GatedLookupSink(FakeServerStateSink):
        """Stalls the first held-server lookup until released."""

        def __init__(self) -> None:
            super().__init__(known_server_ids={_SERVER_A})
            self._first_call = True

        async def existing_server_ids(self, *, server_ids: list[str]) -> set[str]:
            if self._first_call:
                self._first_call = False
                lookup_started.set()
                await release_lookup.wait()
            return await super().existing_server_ids(server_ids=server_ids)

    sink = _GatedLookupSink()
    state = ControlPlaneState()
    control_plane = GrpcControlPlane(state, clock=FakeClock(_T0), timeout_seconds=5.0)
    worker = WorkerId(_WORKER_ID)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
        control_plane=state,
    )
    try:
        stub = await h.start()

        # Session A is open and stalled in its held lookup; a report lands on its
        # queue.
        call_a = stub.Session(metadata=_auth(_CREDENTIAL))
        await call_a.write(_register_message(held_servers={_SERVER_A: 1}))
        await asyncio.wait_for(lookup_started.wait(), timeout=5)
        queue_a = state.outbound_for(worker)
        assert queue_a is not None
        await control_plane.dispatch_fire_and_forget(
            worker_id=worker, server_id=_SERVER_A, command=ReclaimDeletedServerCommand()
        )

        # Session C supersedes A while A's lookup is still stalled.
        call_c = stub.Session(metadata=_auth(_CREDENTIAL))
        await call_c.write(_register_message())
        ack_c = await call_c.read()
        assert ack_c.WhichOneof("payload") == "register_ack"
        queue_c = state.outbound_for(worker)
        assert queue_c is not None and queue_c is not queue_a

        # A resumes: it yields its ack, sends nothing it had queued, and ends OK.
        release_lookup.set()
        ack_a = await asyncio.wait_for(call_a.read(), timeout=5)
        assert ack_a.WhichOneof("payload") == "register_ack"
        await call_a.done_writing()
        assert await asyncio.wait_for(call_a.read(), timeout=5) is aio.EOF
        assert await asyncio.wait_for(call_a.code(), timeout=5) == grpc.StatusCode.OK

        # C stays current, the Worker online and none of its servers unknown, and a
        # later report reaches the Worker on C.
        assert state.outbound_for(worker) is queue_c
        assert h.registry.list_workers()[0].status is WorkerStatus.ONLINE
        assert sink.unknown_for == []
        await control_plane.dispatch_fire_and_forget(
            worker_id=worker, server_id=_SERVER_B, command=ReclaimDeletedServerCommand()
        )
        msg = await asyncio.wait_for(call_c.read(), timeout=5)
        assert msg.api_command.WhichOneof("command") == "reclaim_deleted_server"
        assert msg.api_command.server_id == _SERVER_B
        await call_c.write(
            pb.WorkerMessage(
                correlation_id=msg.api_command.command_id,
                command_result=pb.CommandResult(success=True),
            )
        )
        await call_c.done_writing()
    finally:
        await h.stop()


# ---------------------------------------------------------------------------
# Reconnect-race regression (issue #1694)
# ---------------------------------------------------------------------------


async def test_superseded_registration_cannot_divorce_control_plane_from_registry() -> (
    None
):
    """A stale session that resumes its DB awaits after a newer session's
    open_session must be aborted, leaving the current session's outbound queue
    intact (issue #1694).

    The race: session B registers (gets a registry token), then stalls in its
    DB awaits. Session C registers (gets a higher token), finishes its awaits
    first, and calls open_session. When B resumes, its is_current_session check
    must fail and the session must be aborted with ABORTED, so it never calls
    open_session to overwrite C's queue.
    """

    # Gate the first session's DB await so we can control interleaving.
    gate = asyncio.Event()

    class _GatedSink(FakeServerStateSink):
        """Stalls running_assignment_ids until the gate is set."""

        def __init__(self) -> None:
            super().__init__()
            self._first_call = True

        async def running_assignment_ids(self, *, worker_id: str) -> dict[str, int]:
            if self._first_call:
                self._first_call = False
                await gate.wait()
            return await super().running_assignment_ids(worker_id=worker_id)

    sink = _GatedSink()
    control_plane = ControlPlaneState()
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
        control_plane=control_plane,
    )
    try:
        stub = await h.start()

        # Session B registers; its _rebuild_assignments stalls on the gate.
        call_b = stub.Session(metadata=_auth(_CREDENTIAL))
        await call_b.write(_register_message())
        # Do NOT read the ack yet; B is stalled in its awaits.

        # Wait until B's registration has landed in the registry (the register
        # call itself is synchronous and happens before the stalled await).
        for _ in range(200):
            if h.registry.list_workers():
                break
            await asyncio.sleep(0.01)
        assert h.registry.list_workers(), "B should have registered"

        # Session C registers the same worker; its awaits complete immediately
        # because the gate only blocks the FIRST call.
        call_c = stub.Session(metadata=_auth(_CREDENTIAL))
        await call_c.write(_register_message())
        ack_c = await call_c.read()
        assert ack_c.WhichOneof("payload") == "register_ack"

        # Wait until C's open_session has populated the outbound queue.
        worker = WorkerId(_WORKER_ID)
        for _ in range(200):
            if control_plane.outbound_for(worker) is not None:
                break
            await asyncio.sleep(0.01)
        queue_c = control_plane.outbound_for(worker)
        assert queue_c is not None, "C's outbound queue should exist"

        # Release the gate: B resumes its awaits, hits the is_current_session
        # check, and should be aborted.
        gate.set()

        # B's stream must end with ABORTED (superseded by C).
        code_b = await asyncio.wait_for(call_b.code(), timeout=5)
        assert code_b == grpc.StatusCode.ABORTED

        # C's outbound queue must still be the current one — not overwritten.
        assert control_plane.outbound_for(worker) is queue_c

        # C's worker is still ONLINE.
        snapshot = h.registry.list_workers()[0]
        assert snapshot.status is WorkerStatus.ONLINE
        await call_c.done_writing()
    finally:
        await h.stop()


async def test_superseded_rebuild_does_not_overwrite_the_newer_sessions_tally() -> None:
    # Session B's tally read stalls; Session C registers and rebuilds from a
    # fresher tally. When B's stale tally returns, it must not replace C's
    # assignments: C's register reset the confirms and decrements the rebuild
    # guards rely on, so a stale tally written then can count a stopped server
    # or lose a confirmed one until the next reconnect (issue #3983).
    gate = asyncio.Event()
    stopped_since = "33333333-3333-3333-3333-333333333333"

    class _StaleFirstTallySink(FakeServerStateSink):
        """The first tally read stalls and returns a server stopped since."""

        def __init__(self) -> None:
            super().__init__()
            self._first_call = True

        async def running_assignment_ids(self, *, worker_id: str) -> dict[str, int]:
            if self._first_call:
                self._first_call = False
                await gate.wait()
                return {stopped_since: 512}
            return await super().running_assignment_ids(worker_id=worker_id)

    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=_StaleFirstTallySink(),
    )
    worker = WorkerId(_WORKER_ID)
    try:
        stub = await h.start()
        call_b = stub.Session(metadata=_auth(_CREDENTIAL))
        await call_b.write(_register_message())
        for _ in range(200):
            if h.registry.list_workers():
                break
            await asyncio.sleep(0.01)
        assert h.registry.list_workers(), "B should have registered"

        call_c = stub.Session(metadata=_auth(_CREDENTIAL))
        await call_c.write(_register_message())
        await call_c.read()  # ack: C's rebuild has run

        gate.set()
        assert await asyncio.wait_for(call_b.code(), timeout=5) == (
            grpc.StatusCode.ABORTED
        )

        assert h.registry.list_workers()[0].assigned_count == 0
        assert h.registry.committed_memory_mb(worker) == 0
        await call_c.done_writing()
    finally:
        await h.stop()


# ---------------------------------------------------------------------------
# Rebuild-failure teardown regression (issue #1958)
# ---------------------------------------------------------------------------


async def test_rebuild_failure_tears_down_registration() -> None:
    """A DB error in _rebuild_assignments must not leave a phantom ONLINE worker.

    After register succeeds but the subsequent running_assignment_ids raises,
    the session must abort with UNAVAILABLE, the worker must read OFFLINE (not
    placeable), and no outbound queue should exist (issue #1958).
    """

    class _FailingRebuildSink(FakeServerStateSink):
        async def running_assignment_ids(self, *, worker_id: str) -> dict[str, int]:
            raise RuntimeError("transient DB error")

    sink = _FailingRebuildSink()
    control_plane = ControlPlaneState()
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
        control_plane=control_plane,
    )
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message())

        # The stream should end with UNAVAILABLE.
        code = await asyncio.wait_for(call.code(), timeout=5)
        assert code == grpc.StatusCode.UNAVAILABLE

        # The worker must not be ONLINE / placeable.
        worker = WorkerId(_WORKER_ID)
        candidates = h.registry.candidates_for_placement()
        assert candidates == []

        # No outbound queue should exist (open_session never ran).
        assert control_plane.outbound_for(worker) is None
    finally:
        await h.stop()


async def test_reconnect_after_rebuild_failure_succeeds() -> None:
    """After a rebuild failure, the next Session from the same worker succeeds.

    The first call raises in running_assignment_ids; the second completes
    normally: RegisterAck is returned, the worker is ONLINE, and load reflects
    the rebuilt assignments (issue #1958).
    """

    class _FailOnceSink(FakeServerStateSink):
        def __init__(self) -> None:
            super().__init__(running_ids={_WORKER_ID: {"srv-1"}})
            self._first_call = True

        async def running_assignment_ids(self, *, worker_id: str) -> dict[str, int]:
            if self._first_call:
                self._first_call = False
                raise RuntimeError("transient DB error")
            return await super().running_assignment_ids(worker_id=worker_id)

    sink = _FailOnceSink()
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
    )
    try:
        stub = await h.start()

        # First attempt fails.
        call1 = stub.Session(metadata=_auth(_CREDENTIAL))
        await call1.write(_register_message())
        code1 = await asyncio.wait_for(call1.code(), timeout=5)
        assert code1 == grpc.StatusCode.UNAVAILABLE

        # Second attempt succeeds.
        call2 = stub.Session(metadata=_auth(_CREDENTIAL))
        await call2.write(_register_message())
        ack = await call2.read()
        assert ack.WhichOneof("payload") == "register_ack"

        # Worker is ONLINE with rebuilt load.
        snapshots = h.registry.list_workers()
        assert len(snapshots) == 1
        assert snapshots[0].status is WorkerStatus.ONLINE
        candidates = h.registry.candidates_for_placement()
        assert len(candidates) == 1
        assert candidates[0].load == 1
        await call2.done_writing()
    finally:
        await h.stop()


async def test_stale_rebuild_failure_does_not_offline_newer_session() -> None:
    """A rebuild failure on a stale session must not offline a newer session.

    Session B's rebuild stalls then raises AFTER session C has registered and
    opened successfully. Because mark_disconnected is token-guarded, C stays
    ONLINE and its outbound queue is intact (issue #1958).
    """

    gate = asyncio.Event()

    class _GatedFailSink(FakeServerStateSink):
        """First call waits on gate then raises; subsequent calls succeed."""

        def __init__(self) -> None:
            super().__init__()
            self._first_call = True

        async def running_assignment_ids(self, *, worker_id: str) -> dict[str, int]:
            if self._first_call:
                self._first_call = False
                await gate.wait()
                raise RuntimeError("stale rebuild failure")
            return await super().running_assignment_ids(worker_id=worker_id)

    sink = _GatedFailSink()
    control_plane = ControlPlaneState()
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
        control_plane=control_plane,
    )
    try:
        stub = await h.start()
        worker = WorkerId(_WORKER_ID)

        # Session B registers; its _rebuild_assignments stalls on the gate.
        call_b = stub.Session(metadata=_auth(_CREDENTIAL))
        await call_b.write(_register_message())

        # Wait for B's registration to land.
        for _ in range(200):
            if h.registry.list_workers():
                break
            await asyncio.sleep(0.01)
        assert h.registry.list_workers(), "B should have registered"

        # Session C registers; its rebuild completes immediately.
        call_c = stub.Session(metadata=_auth(_CREDENTIAL))
        await call_c.write(_register_message())
        ack_c = await call_c.read()
        assert ack_c.WhichOneof("payload") == "register_ack"

        # Wait for C's outbound queue.
        for _ in range(200):
            if control_plane.outbound_for(worker) is not None:
                break
            await asyncio.sleep(0.01)
        queue_c = control_plane.outbound_for(worker)
        assert queue_c is not None, "C's outbound queue should exist"

        # Release B: its rebuild raises after C is fully open.
        gate.set()

        # B's stream must end non-OK.
        code_b = await asyncio.wait_for(call_b.code(), timeout=5)
        assert code_b != grpc.StatusCode.OK

        # C stays ONLINE.
        snapshot = h.registry.list_workers()[0]
        assert snapshot.status is WorkerStatus.ONLINE

        # C's outbound queue is still intact.
        assert control_plane.outbound_for(worker) is queue_c
        await call_c.done_writing()
    finally:
        await h.stop()


# ---------------------------------------------------------------------------
# Registration deadline (issue #4050)
# ---------------------------------------------------------------------------

_REGISTRATION_DEADLINE_SECONDS = 0.2
_LIMITS_PATH = (
    Path(__file__).resolve().parents[3]
    / "proto"
    / "contract"
    / "control_plane_limits.json"
)


class _StalledRegistrationSink(FakeServerStateSink):
    """A database that does not answer the registration's rebuild, or its
    held-server lookup."""

    def __init__(self, *, rebuild: bool, lookup: bool) -> None:
        super().__init__(running_ids={_WORKER_ID: {_SERVER_A}})
        self._rebuild = rebuild
        self._lookup = lookup

    async def running_assignment_ids(self, *, worker_id: str) -> dict[str, int]:
        if self._rebuild:
            await asyncio.Event().wait()
        return await super().running_assignment_ids(worker_id=worker_id)

    async def existing_server_ids(self, *, server_ids: list[str]) -> set[str]:
        if self._lookup:
            await asyncio.Event().wait()
        return await super().existing_server_ids(server_ids=server_ids)


def test_the_registration_deadline_is_a_third_of_the_workers_wait_for_its_ack() -> None:
    limits = json.loads(_LIMITS_PATH.read_text())

    assert (
        grpc_server._REGISTRATION_DATABASE_TIMEOUT_SECONDS * 3
        <= limits["register_ack_timeout_seconds"]
    )


class _StatusAtAbortContext(_FakeContext):
    """Also records the Worker's status in ``registry`` as the abort is sent."""

    def __init__(
        self, metadata: list[tuple[str, str]], registry: InMemoryWorkerRegistry
    ) -> None:
        super().__init__(metadata)
        self._registry = registry
        self.status_at_abort: WorkerStatus | None = None

    async def abort(self, code: grpc.StatusCode, details: str = "") -> None:
        self.status_at_abort = self._registry.list_workers()[0].status
        await super().abort(code, details)


async def test_a_rebuild_the_database_does_not_answer_refuses_the_registration(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def _requests() -> AsyncIterator[pb.WorkerMessage]:
        yield _register_message()

    monkeypatch.setattr(
        grpc_server,
        "_REGISTRATION_DATABASE_TIMEOUT_SECONDS",
        _REGISTRATION_DEADLINE_SECONDS,
    )
    registry = InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT)
    control_plane = ControlPlaneState()
    servicer = WorkerSessionServicer(
        registry=registry,
        clock=FakeClock(_T0),
        worker_credential=_CREDENTIAL,
        heartbeat_timeout=_TIMEOUT,
        transfer_deadline=_TRANSFER_DEADLINE,
        control_plane=control_plane,
        state_sink=_StalledRegistrationSink(rebuild=True, lookup=False),
        real_time_events=RecordingRealTimeEvents(),
        database_ping=FakeDatabasePing(),
    )
    context = _StatusAtAbortContext(_auth(_CREDENTIAL), registry)

    with pytest.raises(_Aborted):
        await asyncio.wait_for(anext(servicer.Session(_requests(), context)), 5)

    assert context.aborted == (
        grpc.StatusCode.UNAVAILABLE,
        "the database did not answer the registration in time",
    )
    # Offline before the Worker hears the refusal, so never placeable meanwhile.
    assert context.status_at_abort is WorkerStatus.OFFLINE
    assert control_plane.outbound_for(WorkerId(_WORKER_ID)) is None
    assert _warnings(caplog) == [
        "the database did not answer the assignment rebuild within 0.2s; "
        "refusing the registration"
    ]


async def test_a_held_lookup_the_database_does_not_answer_still_acks_in_time(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        grpc_server,
        "_REGISTRATION_DATABASE_TIMEOUT_SECONDS",
        _REGISTRATION_DEADLINE_SECONDS,
    )
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=_StalledRegistrationSink(rebuild=False, lookup=True),
    )
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message(held_servers={_SERVER_A: 1}))
        response = await asyncio.wait_for(call.read(), timeout=5)
        candidates = h.registry.candidates_for_placement()
        await call.done_writing()
    finally:
        await h.stop()

    assert list(response.register_ack.unknown_held_server_ids) == []
    assert [candidate.load for candidate in candidates] == [1]
    assert _warnings(caplog) == [
        "failed to compute unknown held servers; sending empty list"
    ]


# A rebuild that answers at half the deadline leaves the held-server lookup the
# other half, so the ack is due at the deadline. A lookup with a deadline of its
# own could not answer before the rebuild's time plus a whole deadline, 1.5 s.
_SHARED_DEADLINE_SECONDS = 1.0
_SLOW_REBUILD_SECONDS = 0.5


class _SlowRebuildSink(_StalledRegistrationSink):
    """A database that answers the rebuild only after half the deadline, and not
    the held-server lookup."""

    def __init__(self) -> None:
        super().__init__(rebuild=False, lookup=True)

    async def running_assignment_ids(self, *, worker_id: str) -> dict[str, int]:
        await asyncio.sleep(_SLOW_REBUILD_SECONDS)
        return await super().running_assignment_ids(worker_id=worker_id)


async def test_the_rebuild_and_the_held_lookup_answer_within_one_deadline(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        grpc_server,
        "_REGISTRATION_DATABASE_TIMEOUT_SECONDS",
        _SHARED_DEADLINE_SECONDS,
    )
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=_SlowRebuildSink(),
    )
    loop = asyncio.get_running_loop()
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        started = loop.time()
        await call.write(_register_message(held_servers={_SERVER_A: 1}))
        response = await asyncio.wait_for(call.read(), timeout=5)
        elapsed = loop.time() - started
        candidates = h.registry.candidates_for_placement()
        await call.done_writing()
    finally:
        await h.stop()

    assert list(response.register_ack.unknown_held_server_ids) == []
    assert [candidate.load for candidate in candidates] == [1]
    assert _warnings(caplog) == [
        "failed to compute unknown held servers; sending empty list"
    ]
    # About one deadline, short of the 1.5 s a fresh one would take.
    assert _SHARED_DEADLINE_SECONDS <= elapsed < 1.4


# ---------------------------------------------------------------------------
# The teardown write of a refused registration (issue #4091)
# ---------------------------------------------------------------------------

_SKIPPED_MARK = (
    "the database did not answer the refused registration's mark-unknown "
    "within 0.2s; skipping it"
)


class _UnansweredMarkSink(_StalledRegistrationSink):
    """A database that refuses the registration, by failing its rebuild or not
    answering it, and does not answer the teardown's mark-unknown."""

    def __init__(self, *, refusal: str) -> None:
        super().__init__(rebuild=refusal == "unanswered", lookup=False)
        self._refusal = refusal

    async def running_assignment_ids(self, *, worker_id: str) -> dict[str, int]:
        if self._refusal == "failed":
            raise RuntimeError("the rebuild failed")
        return await super().running_assignment_ids(worker_id=worker_id)

    async def mark_worker_servers_unknown(self, *, worker_id: str) -> None:
        await asyncio.Event().wait()


@pytest.mark.parametrize("refusal", ["unanswered", "failed"])
async def test_a_refused_registrations_mark_unknown_is_skipped_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, refusal: str
) -> None:
    caplog.set_level(logging.INFO, logger=_GRPC_SERVER_LOGGER)
    monkeypatch.setattr(
        grpc_server,
        "_REGISTRATION_DATABASE_TIMEOUT_SECONDS",
        _REGISTRATION_DEADLINE_SECONDS,
    )
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=_UnansweredMarkSink(refusal=refusal),
    )
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message())
        code = await asyncio.wait_for(call.code(), timeout=5)
        await _wait_until(
            lambda: _disconnect_logs(caplog), "the teardown did not finish"
        )
    finally:
        await h.stop()

    assert code is grpc.StatusCode.UNAVAILABLE
    assert [
        (r.levelno, getattr(r, "worker_id", None))
        for r in _logged(caplog, _SKIPPED_MARK)
    ] == [(logging.WARNING, _WORKER_ID)]
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert len(_disconnect_logs(caplog)) == 1


async def test_a_closed_sessions_mark_unknown_outlasting_the_deadline_still_lands(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Only a refused registration's write is bounded: the Worker of a session
    # that ended may never come back, and a skipped write would leave its
    # servers reading as they last reported (FR-WRK-4).
    caplog.set_level(logging.INFO, logger=_GRPC_SERVER_LOGGER)
    monkeypatch.setattr(
        grpc_server,
        "_REGISTRATION_DATABASE_TIMEOUT_SECONDS",
        _REGISTRATION_DEADLINE_SECONDS,
    )
    sink = _BlockingUnknownSink()
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
    )
    try:
        call = await _registered(await h.start())
        await call.done_writing()
        await asyncio.wait_for(sink.entered.wait(), timeout=5)
        await asyncio.sleep(_REGISTRATION_DEADLINE_SECONDS * 3)
        sink.release.set()
        await _wait_until(
            lambda: _disconnect_logs(caplog), "the teardown did not finish"
        )
    finally:
        sink.release.set()
        await h.stop()

    assert sink.unknown_for == [_WORKER_ID]
    assert _logged(caplog, _SKIPPED_MARK) == []


# ---------------------------------------------------------------------------
# Heartbeat fast-path (issue #1698)
# ---------------------------------------------------------------------------


async def test_heartbeat_survives_slow_status_write() -> None:
    """A slow DB write must not starve heartbeat liveness (issue #1698).

    Heartbeats bypass the processing queue entirely, so even when the
    processor is blocked on a DB-bound StatusChange the watchdog still sees
    fresh heartbeats and keeps the session alive.
    """

    gate = asyncio.Event()

    class _SlowSink(FakeServerStateSink):
        async def record_observed_state(
            self, *, server_id: str, worker_id: str, state: str, **frame: Any
        ) -> bool:
            await gate.wait()
            return await super().record_observed_state(
                server_id=server_id, worker_id=worker_id, state=state, **frame
            )

    sink = _SlowSink()
    h = _shared_clock_harness()
    h.state_sink = sink
    # Rebuild with the custom sink.
    h = _Harness(
        InMemoryWorkerRegistry(clock=h.clock, heartbeat_timeout=_SHORT_TIMEOUT),
        h.clock,
        heartbeat_timeout=_SHORT_TIMEOUT,
        state_sink=sink,
    )
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message())
        await call.read()  # ack

        server_id = "11111111-1111-1111-1111-111111111111"
        # Send a StatusChange that will park in the slow sink.
        await call.write(_status_message(server_id, pb.SERVER_STATE_RUNNING))

        # Repeatedly advance the clock and heartbeat, staying under the
        # timeout each step. With the old serial design these heartbeats
        # would queue behind the blocked StatusChange and be missed.
        for i in range(1, 5):
            at = _T0 + dt.timedelta(seconds=0.2 * i)
            h.clock.set(at)
            await call.write(_heartbeat_message())
            await _wait_for_heartbeat_at(h, at)

        # The worker must still be ONLINE despite the StatusChange blocking.
        snapshot = h.registry.list_workers()[0]
        assert snapshot.status is WorkerStatus.ONLINE

        # Release the gate so the StatusChange completes.
        gate.set()
        for _ in range(100):
            if sink.observed:
                break
            await asyncio.sleep(0.01)
        assert sink.observed == [(server_id, _WORKER_ID, "running")]
        await call.done_writing()
    finally:
        await h.stop()


async def test_status_changes_processed_in_send_order() -> None:
    """Status changes queued behind a gated write are delivered in order."""

    gate = asyncio.Event()

    class _GatedOnceSink(FakeServerStateSink):
        def __init__(self) -> None:
            super().__init__()
            self._first = True

        async def record_observed_state(
            self, *, server_id: str, worker_id: str, state: str, **frame: Any
        ) -> bool:
            if self._first:
                self._first = False
                await gate.wait()
            return await super().record_observed_state(
                server_id=server_id, worker_id=worker_id, state=state, **frame
            )

    sink = _GatedOnceSink()
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
    )
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message())
        await call.read()  # ack

        server_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        server_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        await call.write(_status_message(server_a, pb.SERVER_STATE_RUNNING))
        await call.write(_status_message(server_b, pb.SERVER_STATE_STOPPED))

        # Release the gate so both writes complete.
        gate.set()
        for _ in range(200):
            if len(sink.observed) >= 2:
                break
            await asyncio.sleep(0.01)

        assert sink.observed == [
            (server_a, _WORKER_ID, "running"),
            (server_b, _WORKER_ID, "stopped"),
        ]
        await call.done_writing()
    finally:
        await h.stop()


async def test_clean_eof_drains_queued_messages() -> None:
    """A clean client half-close drains all queued messages before returning."""

    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
    )
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message())
        await call.read()  # ack

        server_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        server_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        server_c = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        await call.write(_status_message(server_a, pb.SERVER_STATE_RUNNING))
        await call.write(_status_message(server_b, pb.SERVER_STATE_STOPPED))
        await call.write(_status_message(server_c, pb.SERVER_STATE_STARTING))
        await call.done_writing()

        # Drain the server response stream to completion.
        await _drain_to_eof(call)

        # All three status changes must have been processed.
        assert len(h.state_sink.observed) == 3
        assert h.state_sink.observed == [
            (server_a, _WORKER_ID, "running"),
            (server_b, _WORKER_ID, "stopped"),
            (server_c, _WORKER_ID, "starting"),
        ]
    finally:
        await h.stop()


# ---------------------------------------------------------------------------
# Teardown under a late RPC cancel (issue #3555)
# ---------------------------------------------------------------------------

_GRPC_SERVER_LOGGER = "mc_server_dashboard_api.fleet.adapters.grpc_server"
_SERVER_ID = "11111111-1111-1111-1111-111111111111"


def _disconnect_logs(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == "worker disconnected"]


class _BlockingUnknownSink(FakeServerStateSink):
    """Holds ``mark_worker_servers_unknown`` until ``release`` is set.

    It also captures the gRPC RPC task, from the registration-time assignment
    rebuild that runs inside it, so a test can wait for that task to end.
    """

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.rpc_task: asyncio.Task[Any] | None = None

    async def running_assignment_ids(self, *, worker_id: str) -> dict[str, int]:
        self.rpc_task = asyncio.current_task()
        return await super().running_assignment_ids(worker_id=worker_id)

    async def mark_worker_servers_unknown(self, *, worker_id: str) -> None:
        self.entered.set()
        await self.release.wait()
        await super().mark_worker_servers_unknown(worker_id=worker_id)


async def _cancel_rpc_mid_teardown(h: _Harness, sink: _BlockingUnknownSink) -> None:
    """Drive a Session into its teardown, then let gRPC cancel the RPC task.

    When a Worker's transport dies, gRPC core completes two operations on their
    own: the pending read fails, which the inbound iterator reports as a clean
    EOF, and the close-on-server op reports a cancel, which cancels the RPC task.
    When the EOF is handled first, the cancel lands inside the teardown. The
    half-close is that EOF; the client cancel is that late cancel, delivered
    while the teardown is held inside ``mark_worker_servers_unknown``.
    """

    stub = await h.start()
    call = stub.Session(metadata=_auth(_CREDENTIAL))
    await call.write(_register_message())
    await call.read()  # ack
    await call.done_writing()
    await asyncio.wait_for(sink.entered.wait(), timeout=5)
    call.cancel()
    assert sink.rpc_task is not None
    done, _ = await asyncio.wait({sink.rpc_task}, timeout=5)
    assert done, "gRPC should have cancelled and finished the RPC task"


async def test_rpc_cancel_mid_teardown_still_marks_servers_unknown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Issue #3555: the cancel used to interrupt the mark-unknown write and skip
    # the log line, and gRPC logged the CancelledError only at DEBUG. The
    # Worker's servers then kept their last observed state while it was down.
    caplog.set_level(logging.INFO, logger=_GRPC_SERVER_LOGGER)
    sink = _BlockingUnknownSink()
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
    )
    try:
        await _cancel_rpc_mid_teardown(h, sink)

        sink.release.set()
        for _ in range(200):
            if sink.unknown_for and _disconnect_logs(caplog):
                break
            await asyncio.sleep(0.01)

        assert sink.unknown_for == [_WORKER_ID]
        assert len(_disconnect_logs(caplog)) == 1
        assert h.registry.list_workers()[0].status is WorkerStatus.OFFLINE
    finally:
        sink.release.set()
        await h.stop()


async def test_drain_teardowns_waits_for_a_teardown_its_rpc_left_behind() -> None:
    # The teardown outlives its cancelled RPC task, so the app awaits it at
    # shutdown, before disposing the engine it writes through.
    sink = _BlockingUnknownSink()
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
    )
    try:
        await _cancel_rpc_mid_teardown(h, sink)
        assert h.servicer is not None
        drain = asyncio.ensure_future(h.servicer.drain_teardowns())
        await asyncio.sleep(0.05)
        assert not drain.done(), "the teardown is still held in the sink"

        sink.release.set()
        await asyncio.wait_for(drain, timeout=5)

        assert sink.unknown_for == [_WORKER_ID]
    finally:
        sink.release.set()
        await h.stop()


async def test_cancelled_stale_teardown_does_not_mark_servers_unknown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The teardown that survives the RPC cancel must still apply the issue #775
    # guard, and at the point it writes rather than when the RPC ended: Session
    # A is still current when its RPC is cancelled, and Worker B replaces it
    # while A's teardown is still settling its reader.
    caplog.set_level(logging.INFO, logger=_GRPC_SERVER_LOGGER)
    entered = asyncio.Event()
    unwinding = asyncio.Event()
    release = asyncio.Event()

    class _SlowUnwindSink(FakeServerStateSink):
        """A status write whose cancellation takes until ``release`` to unwind."""

        async def record_observed_state(
            self, *, server_id: str, worker_id: str, state: str, **frame: Any
        ) -> bool:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                unwinding.set()
                await release.wait()
                raise
            return await super().record_observed_state(
                server_id=server_id, worker_id=worker_id, state=state, **frame
            )

    sink = _SlowUnwindSink()
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
    )
    try:
        stub = await h.start()
        call_a = stub.Session(metadata=_auth(_CREDENTIAL))
        await call_a.write(_register_message())
        await call_a.read()  # ack
        await call_a.write(_status_message(_SERVER_ID, pb.SERVER_STATE_RUNNING))
        await asyncio.wait_for(entered.wait(), timeout=5)

        # The RPC is cancelled while A is current; its teardown cancels the
        # reader and waits for the held status write to unwind.
        call_a.cancel()
        await asyncio.wait_for(unwinding.wait(), timeout=5)

        call_b = stub.Session(metadata=_auth(_CREDENTIAL))
        await call_b.write(_register_message())
        await call_b.read()  # ack: B is now the current Session

        release.set()
        for _ in range(200):
            if _disconnect_logs(caplog):
                break
            await asyncio.sleep(0.01)

        assert len(_disconnect_logs(caplog)) == 1, "A's teardown should finish"
        assert sink.unknown_for == []
        assert h.registry.list_workers()[0].status is WorkerStatus.ONLINE
        await call_b.done_writing()
    finally:
        release.set()
        await h.stop()


async def test_failed_mark_unknown_is_logged_and_the_teardown_finishes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The teardown catches and logs its own write failure: once the RPC's cancel
    # has broken the shield, nothing else awaits the teardown's outcome, so a
    # raised error would vanish (issue #3555).
    caplog.set_level(logging.INFO, logger=_GRPC_SERVER_LOGGER)

    class _FailingUnknownSink(FakeServerStateSink):
        async def mark_worker_servers_unknown(self, *, worker_id: str) -> None:
            raise RuntimeError("observed-state sink unavailable")

    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=_FailingUnknownSink(),
    )
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message())
        await call.read()  # ack
        await call.done_writing()
        await _drain_to_eof(call)
        assert h.servicer is not None
        await h.servicer.drain_teardowns()

        failures = [
            r
            for r in caplog.records
            if r.getMessage()
            == "failed to mark the disconnected worker's servers unknown"
        ]
        assert len(failures) == 1
        assert failures[0].levelno == logging.ERROR
        assert len(_disconnect_logs(caplog)) == 1
    finally:
        await h.stop()


class _TurnProbeRegistry(InMemoryWorkerRegistry):
    """Records whether the event loop has run anything else since a session was
    last found current."""

    def __init__(self) -> None:
        super().__init__(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT)
        self.loop_ran_since_check = True

    def is_current_session(self, worker_id: WorkerId, session: SessionToken) -> bool:
        current = super().is_current_session(worker_id, session)
        if current:
            self.loop_ran_since_check = False
            asyncio.get_running_loop().call_soon(self._loop_ran)
        return current

    def _loop_ran(self) -> None:
        self.loop_ran_since_check = True


class _TurnProbeSink(FakeServerStateSink):
    """Records, as each mark-unknown is entered, whether the loop has run
    anything else since the teardown's check. With ``refuse_first``, the first
    registration's rebuild never answers, so that registration is refused."""

    def __init__(self, registry: _TurnProbeRegistry, *, refuse_first: bool) -> None:
        super().__init__()
        self._registry = registry
        self._refuse_first = refuse_first
        self.loop_ran_before_write: list[bool] = []

    async def running_assignment_ids(self, *, worker_id: str) -> dict[str, int]:
        if self._refuse_first:
            self._refuse_first = False
            await asyncio.Event().wait()
        return await super().running_assignment_ids(worker_id=worker_id)

    async def mark_worker_servers_unknown(self, *, worker_id: str) -> None:
        self.loop_ran_before_write.append(self._registry.loop_ran_since_check)
        await super().mark_worker_servers_unknown(worker_id=worker_id)


@pytest.mark.parametrize("ending", ["closed", "refused"])
async def test_a_teardown_enters_its_write_before_anything_else_runs_after_its_check(
    monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    # Issue #4092: the sink stamps the write as it is entered, and the write
    # leaves every server observed later than that stamp. A newer session's
    # reports are stamped later only if nothing, its registration included,
    # can run between the teardown's current-session check and that entry.
    monkeypatch.setattr(
        grpc_server,
        "_REGISTRATION_DATABASE_TIMEOUT_SECONDS",
        _REGISTRATION_DEADLINE_SECONDS,
    )
    registry = _TurnProbeRegistry()
    sink = _TurnProbeSink(registry, refuse_first=ending == "refused")
    h = _Harness(registry, FakeClock(_T0), state_sink=sink)
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message())
        if ending == "closed":
            await call.read()  # ack
            await call.done_writing()
        await _wait_until(
            lambda: sink.loop_ran_before_write, "the teardown did not write"
        )
    finally:
        await h.stop()

    assert sink.loop_ran_before_write == [False]


async def test_shutdown_drain_waits_for_a_live_session_to_tear_down() -> None:
    # PR #3566 review: the gRPC stop does not wait for the Python handlers, so a
    # live Session may not have reached its teardown when the drain starts. The
    # drain must still return only after that teardown's write. Repeated because
    # the ordering is a race (it lost about half the runs before the fix).
    for _ in range(20):
        h = _Harness(
            InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
            FakeClock(_T0),
        )
        try:
            stub = await h.start()
            call = stub.Session(metadata=_auth(_CREDENTIAL))
            await call.write(_register_message())
            await call.read()  # ack
            assert h._server is not None and h.servicer is not None

            await h._server.stop(grace=None)
            await h.servicer.drain_teardowns()

            assert h.state_sink.unknown_for == [_WORKER_ID]
        finally:
            await h.stop()


def _pending_teardowns() -> list[asyncio.Task[Any]]:
    return [
        t
        for t in asyncio.all_tasks()
        if not t.done()
        and getattr(t.get_coro(), "__qualname__", "").endswith("._teardown")
    ]


async def test_drain_cancels_a_stalled_teardown_after_its_budget(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A mark-unknown write that never returns (a held row lock; the engine sets
    # no statement timeout) must not block shutdown: past the budget the drain
    # logs the worker, cancels the teardown and waits for it to settle.
    monkeypatch.setattr(grpc_server, "_TEARDOWN_DRAIN_SECONDS", 0.2)
    cancelled = asyncio.Event()

    class _StalledSink(FakeServerStateSink):
        async def mark_worker_servers_unknown(self, *, worker_id: str) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=_StalledSink(),
    )
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message())
        await call.read()  # ack
        assert h._server is not None and h.servicer is not None
        await h._server.stop(grace=None)

        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.wait_for(h.servicer.drain_teardowns(), timeout=5)
        elapsed = loop.time() - started

        assert elapsed < 0.2 + 1.0
        assert cancelled.is_set()
        assert _pending_teardowns() == []
        errors = [
            r
            for r in caplog.records
            if r.levelno == logging.ERROR
            and r.getMessage().startswith("session teardowns did not finish")
        ]
        assert len(errors) == 1
        assert _WORKER_ID in errors[0].getMessage()
    finally:
        await h.stop()


async def test_drain_gives_up_on_a_teardown_that_ignores_its_cancel(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A driver that swallows the cancel cannot hang shutdown either: the settle
    # wait is bounded too, and the drain logs that it proceeded without it.
    monkeypatch.setattr(grpc_server, "_TEARDOWN_DRAIN_SECONDS", 0.2)
    monkeypatch.setattr(grpc_server, "_TEARDOWN_SETTLE_SECONDS", 0.2)

    class _StubbornSink(FakeServerStateSink):
        async def mark_worker_servers_unknown(self, *, worker_id: str) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.Event().wait()

    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=_StubbornSink(),
    )
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message())
        await call.read()  # ack
        assert h._server is not None and h.servicer is not None
        await h._server.stop(grace=None)

        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.wait_for(h.servicer.drain_teardowns(), timeout=5)

        assert loop.time() - started < 0.4 + 1.0
        assert any(
            r.levelno == logging.ERROR
            and r.getMessage().startswith("session teardowns did not settle")
            for r in caplog.records
        )
    finally:
        for task in _pending_teardowns():
            task.cancel()
        await h.stop()


async def test_rpc_cancel_during_assignment_rebuild_still_tears_down() -> None:
    # Issue #3555, same class: a cancel that lands after the registry write but
    # before the stream is serving (here, in the assignment rebuild) used to skip
    # the teardown, leaving the Worker ONLINE with no session behind it.
    entered = asyncio.Event()

    class _BlockingRebuildSink(FakeServerStateSink):
        async def running_assignment_ids(self, *, worker_id: str) -> dict[str, int]:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")  # pragma: no cover

    sink = _BlockingRebuildSink()
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
    )
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message())
        await asyncio.wait_for(entered.wait(), timeout=5)
        call.cancel()
        assert h.servicer is not None
        await h.servicer.drain_teardowns()

        assert h.registry.list_workers()[0].status is WorkerStatus.OFFLINE
        assert sink.unknown_for == [_WORKER_ID]
    finally:
        await h.stop()


_WORKER_B = "33333333-3333-3333-3333-333333333333"


class _ShutdownRaceSink(FakeServerStateSink):
    """Stalls the writes that race a shutdown drain, with slow cancel cleanup.

    A worker in ``rebuild_unwind`` blocks in the registration-time assignment
    rebuild, and once cancelled takes that many seconds to unwind before its
    handler hands off a teardown. Every mark-unknown write stalls until it is
    cancelled, then takes ``mark_cleanup[worker]`` seconds (default 0) to
    unwind. ``events`` records each write's start and cancel, in order.
    """

    def __init__(
        self,
        *,
        rebuild_unwind: dict[str, float],
        mark_cleanup: dict[str, float] | None = None,
    ) -> None:
        super().__init__()
        self._rebuild_unwind = rebuild_unwind
        self._mark_cleanup = mark_cleanup or {}
        self.rebuild_entered = {wid: asyncio.Event() for wid in rebuild_unwind}
        self.mark_entered = asyncio.Event()
        self.events: list[str] = []

    async def running_assignment_ids(self, *, worker_id: str) -> dict[str, int]:
        if worker_id in self._rebuild_unwind:
            self.rebuild_entered[worker_id].set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(self._rebuild_unwind[worker_id])
                raise
        return await super().running_assignment_ids(worker_id=worker_id)

    async def mark_worker_servers_unknown(self, *, worker_id: str) -> None:
        self.events.append(f"mark-start {worker_id}")
        self.mark_entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(self._mark_cleanup.get(worker_id, 0.0))
            self.events.append(f"mark-cancelled {worker_id}")
            raise


async def test_drain_settles_a_teardown_handed_off_after_its_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # PR #3566 round 2: the budget can expire while a handler is still
    # unwinding the stop's cancel, with no teardown registered yet. The
    # teardown it hands off later must still be cancelled and settled before
    # the drain returns, or it writes after the engine is disposed.
    monkeypatch.setattr(grpc_server, "_TEARDOWN_DRAIN_SECONDS", 0.1)
    monkeypatch.setattr(grpc_server, "_TEARDOWN_SETTLE_SECONDS", 2.0)
    sink = _ShutdownRaceSink(rebuild_unwind={_WORKER_ID: 0.3})
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
    )
    try:
        stub = await h.start()
        call = stub.Session(metadata=_auth(_CREDENTIAL))
        await call.write(_register_message())
        await asyncio.wait_for(sink.rebuild_entered[_WORKER_ID].wait(), timeout=5)
        assert h._server is not None and h.servicer is not None

        await h._server.stop(grace=None)
        await asyncio.wait_for(h.servicer.drain_teardowns(), timeout=5)
        at_drain = list(sink.events)
        pending_at_drain = _pending_teardowns()
        await asyncio.sleep(0.5)

        assert pending_at_drain == []
        assert sink.events == at_drain, "a teardown wrote after the drain returned"
    finally:
        for task in _pending_teardowns():
            task.cancel()
        await h.stop()


async def test_drain_cancels_a_teardown_handed_off_while_settling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # PR #3566 round 2: Worker A's stalled teardown is being cancelled (its
    # cleanup takes a while) when Worker B's handler, still unwinding the stop's
    # cancel in its rebuild, hands off a teardown. The settle phase must pick B
    # up, cancel it and wait for it too.
    monkeypatch.setattr(grpc_server, "_TEARDOWN_DRAIN_SECONDS", 0.1)
    monkeypatch.setattr(grpc_server, "_TEARDOWN_SETTLE_SECONDS", 2.0)
    sink = _ShutdownRaceSink(
        rebuild_unwind={_WORKER_B: 0.2}, mark_cleanup={_WORKER_ID: 0.4}
    )
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
    )
    try:
        stub = await h.start()
        call_a = stub.Session(metadata=_auth(_CREDENTIAL))
        await call_a.write(_register_message())
        await call_a.read()  # ack
        call_b = stub.Session(metadata=_auth(_CREDENTIAL))
        await call_b.write(_register_message(worker_id=_WORKER_B))
        await asyncio.wait_for(sink.rebuild_entered[_WORKER_B].wait(), timeout=5)
        assert h._server is not None and h.servicer is not None

        await h._server.stop(grace=None)
        await asyncio.wait_for(h.servicer.drain_teardowns(), timeout=5)
        at_drain = list(sink.events)
        pending_at_drain = _pending_teardowns()
        await asyncio.sleep(0.5)

        assert pending_at_drain == []
        assert f"mark-cancelled {_WORKER_ID}" in at_drain
        assert f"mark-cancelled {_WORKER_B}" in at_drain
        assert sink.events == at_drain, "a teardown wrote after the drain returned"
    finally:
        for task in _pending_teardowns():
            task.cancel()
        await h.stop()


# ---------------------------------------------------------------------------
# Holding a report through a database outage (issue #3614)
# ---------------------------------------------------------------------------

_SERVER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_SERVER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_HOLD_STARTED = "holding a worker message while the database is unavailable"
_HOLD_APPLIED = "applied a held worker message"
_HOLD_LOST = "lost a held worker message: its session ended"
_HOLD_REFUSED = (
    "dropping a held worker message: the database answers, yet the message "
    "still fails as if it were down"
)
_SESSION_REPLACED = (
    "a newer session replaced this one; dropping its queued worker messages"
)


# An error is_transient_database_error classifies as an outage, and one it does not.
_OUTAGE = ConnectionRefusedError
_BUG = RuntimeError


class _OutageSink(FakeServerStateSink):
    """Fails some reports' writes, counting every attempt by server.

    While ``down`` is set, every write fails with an outage. A server's writes
    raise the errors ``errors[server]`` lists, in turn, and then succeed. A
    ``broken`` server's writes always fail with an error that is not an outage.
    ``gate`` holds every write until it is set.
    """

    def __init__(
        self,
        *,
        down: bool = False,
        errors: dict[str, list[type[Exception]]] | None = None,
        broken: frozenset[str] = frozenset(),
        gate: asyncio.Event | None = None,
    ) -> None:
        super().__init__()
        self.down = down
        self.attempts: dict[str, int] = {}
        self._errors = {server: list(e) for server, e in (errors or {}).items()}
        self._broken = broken
        self._gate = gate

    async def record_observed_state(
        self, *, server_id: str, worker_id: str, state: str, **frame: Any
    ) -> bool:
        if self._gate is not None:
            await self._gate.wait()
        self.attempts[server_id] = self.attempts.get(server_id, 0) + 1
        if server_id in self._broken:
            raise _BUG("the handler has a bug")
        if self.down:
            raise _OUTAGE("connect call failed")
        if self._errors.get(server_id):
            raise self._errors[server_id].pop(0)("scripted failure")
        return await super().record_observed_state(
            server_id=server_id, worker_id=worker_id, state=state, **frame
        )


@pytest.fixture
def fast_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(grpc_server, "_HOLD_RETRY_INITIAL_SECONDS", 0.01)
    monkeypatch.setattr(grpc_server, "_HOLD_RETRY_MAX_SECONDS", 0.02)


async def _wait_until(condition: Any, what: str) -> None:
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{what} in time")


def _logged(caplog: pytest.LogCaptureFixture, message: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == message]


async def _registered(stub: WorkerServiceStub) -> aio.StreamStreamCall:
    call = stub.Session(metadata=_auth(_CREDENTIAL))
    await call.write(_register_message())
    await call.read()  # ack
    return call


@pytest.mark.usefixtures("fast_hold")
async def test_a_report_held_through_an_outage_is_applied_in_order(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A report whose write fails because the database is down, its ping failing
    # too, is held at the head of the queue and retried: it lands once the
    # database is back, ahead of the report behind it. The hold logs one WARNING
    # as it starts and one INFO as it ends, not a line a retry.
    caplog.set_level(logging.INFO, logger=_GRPC_SERVER_LOGGER)
    gate = asyncio.Event()
    sink = _OutageSink(errors={_SERVER_A: [_OUTAGE] * 2}, gate=gate)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
        database_ping=FakeDatabasePing(reachable=False),
    )
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))
        await call.write(_status_message(_SERVER_B, pb.SERVER_STATE_STOPPED))
        # The reader reads in order, so once the heartbeat sent behind them is
        # recorded, both reports are queued before A's first write fails.
        beat = _T0 + dt.timedelta(seconds=1)
        h.clock.set(beat)
        await call.write(_heartbeat_message())
        await _wait_for_heartbeat_at(h, beat)
        gate.set()
        await _wait_until(lambda: len(sink.observed) == 2, "both reports not applied")

        assert sink.observed == [
            (_SERVER_A, _WORKER_ID, "running"),
            (_SERVER_B, _WORKER_ID, "stopped"),
        ]
        assert sink.attempts == {_SERVER_A: 3, _SERVER_B: 1}
        started = _logged(caplog, _HOLD_STARTED)
        assert [r.levelno for r in started] == [logging.WARNING]
        assert started[0].exc_info is not None
        assert started[0].exc_info[0] is ConnectionRefusedError
        assert [r.levelno for r in _logged(caplog, _HOLD_APPLIED)] == [logging.INFO]
        await call.done_writing()
    finally:
        await h.stop()


@pytest.mark.usefixtures("fast_hold")
@pytest.mark.parametrize("end", ["half_close", "superseded", "heartbeat_lapse"])
async def test_a_held_report_is_lost_when_its_session_ends(
    end: str, caplog: pytest.LogCaptureFixture
) -> None:
    # The hold has no time budget but ends with its session, which loses the
    # report: no retry lands it once the database is back. A session ends when
    # the Worker closes the stream, when a newer session replaces it, or when its
    # heartbeats lapse.
    caplog.set_level(logging.INFO, logger=_GRPC_SERVER_LOGGER)
    sink = _OutageSink(down=True)
    clock = FakeClock(_T0)
    timeout = _SHORT_TIMEOUT if end == "heartbeat_lapse" else _TIMEOUT
    h = _Harness(
        InMemoryWorkerRegistry(clock=clock, heartbeat_timeout=timeout),
        clock,
        heartbeat_timeout=timeout,
        state_sink=sink,
        database_ping=FakeDatabasePing(reachable=False),
    )
    try:
        stub = await h.start()
        call = await _registered(stub)
        await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))
        await _wait_until(
            lambda: sink.attempts.get(_SERVER_A, 0) >= 2, "the report was not held"
        )

        if end == "half_close":
            await call.done_writing()
            await asyncio.wait_for(_drain_to_eof(call), timeout=5)
        elif end == "superseded":
            await _registered(stub)
        else:
            clock.set(_T0 + timeout + dt.timedelta(seconds=1))
            await asyncio.wait_for(_drain_to_eof(call), timeout=5)
        await _wait_until(lambda: _logged(caplog, _HOLD_LOST), "the hold did not end")
        attempts = sink.attempts[_SERVER_A]
        sink.down = False
        await asyncio.sleep(0.2)

        assert sink.observed == []
        assert sink.attempts[_SERVER_A] == attempts
        assert [r.levelno for r in _logged(caplog, _HOLD_LOST)] == [logging.INFO]
    finally:
        await h.stop()


@pytest.mark.usefixtures("fast_hold")
async def test_only_failures_that_are_not_an_outage_count_toward_the_cap() -> None:
    # An outage's failures are held and never count toward #807's cap, however
    # many there are. Any other failure is dropped at once, without a retry, and
    # counts: one more than the cap of them ends the session. The outage outlasts
    # enough doublings that only a capped backoff retries it in time.
    outage = 12
    sink = _OutageSink(
        errors={_SERVER_A: [_OUTAGE] * outage}, broken=frozenset({_SERVER_B})
    )
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
        database_ping=FakeDatabasePing(reachable=False),
    )
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))
        for _ in range(_MAX_CONSECUTIVE_HANDLER_FAILURES + 1):
            await call.write(_status_message(_SERVER_B, pb.SERVER_STATE_RUNNING))
        await _wait_until(
            lambda: (
                sink.attempts.get(_SERVER_B) == _MAX_CONSECUTIVE_HANDLER_FAILURES + 1
            ),
            "the failing reports were not all attempted",
        )
        # The reader sees the ended processor on the next message it reads.
        await call.write(_heartbeat_message())
        await asyncio.wait_for(_drain_to_eof(call), timeout=5)

        assert sink.observed == [(_SERVER_A, _WORKER_ID, "running")]
        assert sink.attempts == {
            _SERVER_A: outage + 1,
            _SERVER_B: _MAX_CONSECUTIVE_HANDLER_FAILURES + 1,
        }
        assert h.registry.list_workers()[0].status is WorkerStatus.OFFLINE
    finally:
        await h.stop()


@pytest.mark.usefixtures("fast_hold")
async def test_an_outage_the_database_ping_contradicts_is_dropped_and_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A write that fails as if the database were down while the database answers
    # its ping fails for the message's own reason, and holding it would block the
    # queue for good. It is dropped after one confirming retry, logged at ERROR,
    # and counts toward #807's cap: one more than the cap of them ends the session.
    sink = _OutageSink(down=True)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
        database_ping=FakeDatabasePing(reachable=True),
    )
    try:
        call = await _registered(await h.start())
        for _ in range(_MAX_CONSECUTIVE_HANDLER_FAILURES + 1):
            await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))
        await _wait_until(
            lambda: (
                sink.attempts.get(_SERVER_A)
                == 2 * (_MAX_CONSECUTIVE_HANDLER_FAILURES + 1)
            ),
            "each report was not tried twice",
        )
        # The reader sees the ended processor on the next message it reads.
        await call.write(_heartbeat_message())
        await asyncio.wait_for(_drain_to_eof(call), timeout=5)

        assert sink.attempts == {_SERVER_A: 2 * (_MAX_CONSECUTIVE_HANDLER_FAILURES + 1)}
        assert h.registry.list_workers()[0].status is WorkerStatus.OFFLINE
        assert [r.levelno for r in _logged(caplog, _HOLD_REFUSED)] == [
            logging.ERROR
        ] * (_MAX_CONSECUTIVE_HANDLER_FAILURES + 1)
    finally:
        await h.stop()


@pytest.mark.usefixtures("fast_hold")
async def test_heartbeats_are_read_while_a_report_is_held() -> None:
    # A held report blocks only the queue behind it, which is far from full here,
    # so the reader still records heartbeats and the session outlives the outage.
    sink = _OutageSink(down=True)
    clock = FakeClock(_T0)
    h = _Harness(
        InMemoryWorkerRegistry(clock=clock, heartbeat_timeout=_SHORT_TIMEOUT),
        clock,
        heartbeat_timeout=_SHORT_TIMEOUT,
        state_sink=sink,
        database_ping=FakeDatabasePing(reachable=False),
    )
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))
        await _wait_until(
            lambda: sink.attempts.get(_SERVER_A, 0) >= 2, "the report was not held"
        )
        # Each step stays under the 0.3 s liveness window, so only a heartbeat
        # left unread would let the session lapse.
        for i in range(1, 5):
            beat = _T0 + dt.timedelta(seconds=0.2 * i)
            clock.set(beat)
            await call.write(_heartbeat_message())
            await _wait_for_heartbeat_at(h, beat)
        assert h.registry.list_workers()[0].status is WorkerStatus.ONLINE

        sink.down = False
        await _wait_until(lambda: sink.observed, "the held report was not applied")
        assert sink.observed == [(_SERVER_A, _WORKER_ID, "running")]
        await call.done_writing()
    finally:
        await h.stop()


@pytest.mark.usefixtures("fast_hold")
async def test_a_superseded_session_applies_nothing_more_from_its_backlog() -> None:
    # The old session holds A through the outage with B queued behind it. The
    # Worker re-registers, the database comes back and the new session reports B
    # stopped. Nothing from the old session's backlog may land after that: its B
    # running would set the row back to the older state.
    sink = _OutageSink(down=True)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
        database_ping=FakeDatabasePing(reachable=False),
    )
    try:
        stub = await h.start()
        old = await _registered(stub)
        await old.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))
        await old.write(_status_message(_SERVER_B, pb.SERVER_STATE_RUNNING))
        beat = _T0 + dt.timedelta(seconds=1)
        h.clock.set(beat)
        await old.write(_heartbeat_message())
        await _wait_for_heartbeat_at(h, beat)
        await _wait_until(
            lambda: sink.attempts.get(_SERVER_A, 0) >= 2, "the report was not held"
        )

        new = await _registered(stub)
        sink.down = False
        await new.write(_status_message(_SERVER_B, pb.SERVER_STATE_STOPPED))
        await _wait_until(lambda: sink.observed, "the new session's report not applied")
        await asyncio.sleep(0.2)

        assert sink.observed == [(_SERVER_B, _WORKER_ID, "stopped")]
        assert sink.attempts[_SERVER_B] == 1
        await new.done_writing()
    finally:
        await h.stop()


async def test_a_superseded_session_handles_no_further_queued_message() -> None:
    # A superseded session's processor stops wherever it is, hold or not: the
    # report queued behind a slow write is never handled once a newer session
    # owns the Worker.
    gate = asyncio.Event()
    sink = _OutageSink(gate=gate)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
    )
    try:
        stub = await h.start()
        old = await _registered(stub)
        await old.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))
        await old.write(_status_message(_SERVER_B, pb.SERVER_STATE_RUNNING))
        beat = _T0 + dt.timedelta(seconds=1)
        h.clock.set(beat)
        await old.write(_heartbeat_message())
        await _wait_for_heartbeat_at(h, beat)

        new = await _registered(stub)
        gate.set()
        await _wait_until(lambda: _SERVER_A in sink.attempts, "A was not written")
        await asyncio.sleep(0.2)

        assert _SERVER_B not in sink.attempts
        await new.done_writing()
    finally:
        await h.stop()


async def test_a_superseded_session_drops_a_message_it_reads_after_the_new_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The old session is idle when the Worker re-registers, so its processor is
    # waiting for a message. A report the old stream still delivers after the new
    # session applied its own is dropped, not applied over it.
    caplog.set_level(logging.INFO, logger=_GRPC_SERVER_LOGGER)
    sink = _OutageSink()
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
    )
    try:
        stub = await h.start()
        old = await _registered(stub)
        new = await _registered(stub)
        await new.write(_status_message(_SERVER_B, pb.SERVER_STATE_STOPPED))
        await _wait_until(lambda: sink.observed, "the new session's report not applied")

        await old.write(_status_message(_SERVER_B, pb.SERVER_STATE_RUNNING))
        await _wait_until(
            lambda: _logged(caplog, _SESSION_REPLACED), "the old session did not stop"
        )

        assert sink.observed == [(_SERVER_B, _WORKER_ID, "stopped")]
        assert sink.attempts == {_SERVER_B: 1}
        await new.done_writing()
    finally:
        await h.stop()


@pytest.mark.usefixtures("fast_hold")
async def test_a_held_report_whose_retry_fails_otherwise_is_dropped_and_counted() -> (
    None
):
    # A retry that fails for a reason other than an outage drops the held report
    # and counts toward #807's cap, so one bad message cannot hold the queue
    # until the session ends.
    reports = _MAX_CONSECUTIVE_HANDLER_FAILURES + 1
    sink = _OutageSink(errors={_SERVER_A: [_OUTAGE, _BUG] * reports})
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
        database_ping=FakeDatabasePing(reachable=False),
    )
    try:
        call = await _registered(await h.start())
        for _ in range(reports):
            await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))
        await _wait_until(
            lambda: sink.attempts.get(_SERVER_A) == 2 * reports,
            "each report was not tried twice",
        )
        # The reader sees the ended processor on the next message it reads.
        await call.write(_heartbeat_message())
        await asyncio.wait_for(_drain_to_eof(call), timeout=5)

        assert sink.observed == []
        assert sink.attempts == {_SERVER_A: 2 * reports}
        assert h.registry.list_workers()[0].status is WorkerStatus.OFFLINE
    finally:
        await h.stop()


@pytest.mark.usefixtures("fast_hold")
async def test_after_a_clean_close_an_outage_failure_is_dropped_and_counted() -> None:
    # Once the Worker closes the stream nothing is held: the held report and each
    # report queued behind it get one attempt, and an outage failure drops the
    # report and counts toward #807's cap, so the drain stays bounded.
    sink = _OutageSink(down=True)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
        database_ping=FakeDatabasePing(reachable=False),
    )
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))
        await _wait_until(
            lambda: sink.attempts.get(_SERVER_A, 0) >= 2, "the report was not held"
        )
        for _ in range(_MAX_CONSECUTIVE_HANDLER_FAILURES + 5):
            await call.write(_status_message(_SERVER_B, pb.SERVER_STATE_RUNNING))
        beat = _T0 + dt.timedelta(seconds=1)
        h.clock.set(beat)
        await call.write(_heartbeat_message())
        await _wait_for_heartbeat_at(h, beat)
        await call.done_writing()
        await asyncio.wait_for(_drain_to_eof(call), timeout=5)

        # A's last attempt is the first failure the cap counts, so the cap ends
        # the session on the fifth B, each tried once.
        assert sink.attempts[_SERVER_B] == _MAX_CONSECUTIVE_HANDLER_FAILURES
        assert sink.observed == []
    finally:
        await h.stop()


@pytest.mark.usefixtures("fast_hold")
async def test_a_held_report_stays_held_when_the_ping_after_its_retry_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The database answered just before the retry, but the retry failed and the
    # database stopped answering right after it: the outage is flapping, the
    # error is not the message's own, so the report stays held and lands.
    sink = _OutageSink(errors={_SERVER_A: [_OUTAGE] * 2})
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
        database_ping=FakeDatabasePing(answers=[True, False], reachable=False),
    )
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))
        await _wait_until(lambda: sink.observed, "the held report was not applied")

        assert sink.observed == [(_SERVER_A, _WORKER_ID, "running")]
        assert sink.attempts == {_SERVER_A: 3}
        assert _logged(caplog, _HOLD_REFUSED) == []
        await call.done_writing()
    finally:
        await h.stop()


@pytest.mark.usefixtures("fast_hold")
async def test_a_held_report_stays_held_when_the_ping_before_its_retry_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The ping before a retry fails and so does the retry, but the database is
    # back by the next ping: the recovery edge every outage has. Only a database
    # that answered both before and after the failed retry drops the report, so
    # it stays held and lands.
    sink = _OutageSink(errors={_SERVER_A: [_OUTAGE] * 2})
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
        database_ping=FakeDatabasePing(answers=[False], reachable=True),
    )
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))
        await _wait_until(
            lambda: sink.observed or _logged(caplog, _HOLD_REFUSED),
            "the held report was neither applied nor dropped",
        )

        assert sink.observed == [(_SERVER_A, _WORKER_ID, "running")]
        assert sink.attempts == {_SERVER_A: 3}
        assert _logged(caplog, _HOLD_REFUSED) == []
        await call.done_writing()
    finally:
        await h.stop()


async def test_a_held_report_gets_a_last_attempt_when_the_worker_closes_the_stream(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The database is back, but the hold is still waiting out its backoff when
    # the Worker closes the stream. The held report gets one last attempt, as
    # each report queued behind it does, and lands ahead of them.
    monkeypatch.setattr(grpc_server, "_HOLD_RETRY_INITIAL_SECONDS", 60.0)
    sink = _OutageSink(down=True)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
        database_ping=FakeDatabasePing(reachable=False),
    )
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_STOPPED))
        await call.write(_status_message(_SERVER_B, pb.SERVER_STATE_RUNNING))
        await _wait_until(lambda: _logged(caplog, _HOLD_STARTED), "no hold started")

        sink.down = False
        await call.done_writing()
        await asyncio.wait_for(_drain_to_eof(call), timeout=5)

        assert sink.observed == [
            (_SERVER_A, _WORKER_ID, "stopped"),
            (_SERVER_B, _WORKER_ID, "running"),
        ]
        assert sink.attempts == {_SERVER_A: 2, _SERVER_B: 1}
    finally:
        await h.stop()


class _RetryFlagSink(_OutageSink):
    """Records, per write, whether it was told it retries an earlier attempt."""

    def __init__(self, **outage: Any) -> None:
        super().__init__(**outage)
        self.retries: list[bool] = []

    async def record_observed_state(
        self, *, server_id: str, worker_id: str, state: str, **frame: Any
    ) -> bool:
        self.retries.append(frame.get("retry", False))
        return await super().record_observed_state(
            server_id=server_id, worker_id=worker_id, state=state, **frame
        )


@pytest.mark.usefixtures("fast_hold")
async def test_a_held_report_tells_the_sink_its_retries_are_retries() -> None:
    # Issue #4062: an attempt that failed as an outage may have committed, its
    # answer lost with the connection. Its retry then finds the row already at
    # the reported state, and must say it is a retry for the sink to publish the
    # frame anyway. A first attempt is none, so a repeated report stays quiet.
    sink = _RetryFlagSink(errors={_SERVER_A: [_OUTAGE] * 2})
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
        database_ping=FakeDatabasePing(reachable=False),
    )
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))
        await call.write(_status_message(_SERVER_B, pb.SERVER_STATE_STOPPED))
        await _wait_until(lambda: len(sink.observed) == 2, "both reports not applied")

        assert sink.retries == [False, True, True, False]
        await call.done_writing()
    finally:
        await h.stop()


class _OutageLateSnapshotSink(LateSnapshotResultSink):
    """Fails the first clear with an outage, then records each server it clears,
    and whether each clear was told it retries an earlier attempt."""

    def __init__(self) -> None:
        self.attempts = 0
        self.cleared: list[str] = []
        self.retries: list[bool] = []

    async def clear_held_assignment_on_late_snapshot(
        self,
        *,
        server_id: str,
        worker_id: str,
        code: CommandResultCode,
        message: str | None,
        retry: bool = False,
    ) -> None:
        self.attempts += 1
        self.retries.append(retry)
        if self.attempts == 1:
            raise _OUTAGE("connect call failed")
        self.cleared.append(server_id)


@pytest.mark.usefixtures("fast_hold")
async def test_a_held_late_final_snapshot_result_releases_on_its_retry() -> None:
    # Issue #4023: the release a late final-snapshot result drives fails while
    # the database is down, so the result is held like any report. Its retry
    # must apply the release, rather than find nothing to release.
    sink = _OutageLateSnapshotSink()
    state = ControlPlaneState(late_snapshot_sink=sink)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        control_plane=state,
        database_ping=FakeDatabasePing(reachable=False),
    )
    try:
        call = await _registered(await h.start())
        # An abandoned final snapshot of this session: its record is made after
        # the registration, whose open_session drops the Worker's earlier ones.
        state.register_pending(
            "snapshot-1", WorkerId(_WORKER_ID), snapshot_server_id=_SERVER_A
        )
        state.discard_pending("snapshot-1")
        await call.write(
            pb.WorkerMessage(
                correlation_id="snapshot-1",
                command_result=pb.CommandResult(success=True),
            )
        )
        await _wait_until(lambda: sink.cleared, "the held release was not applied")

        assert sink.cleared == [_SERVER_A]
        assert sink.attempts == 2
        await call.done_writing()
    finally:
        await h.stop()


@pytest.mark.usefixtures("fast_hold")
async def test_a_held_late_final_snapshot_result_tells_the_sink_its_retry_is_one() -> (
    None
):
    # Issue #4062: the release the first attempt wrote may have committed, its
    # answer lost with the connection. The retry must say it is one, for the
    # sink to announce a release it finds already made.
    sink = _OutageLateSnapshotSink()
    state = ControlPlaneState(late_snapshot_sink=sink)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        control_plane=state,
        database_ping=FakeDatabasePing(reachable=False),
    )
    try:
        call = await _registered(await h.start())
        state.register_pending(
            "snapshot-1", WorkerId(_WORKER_ID), snapshot_server_id=_SERVER_A
        )
        state.discard_pending("snapshot-1")
        await call.write(_command_result_message("snapshot-1"))
        await _wait_until(lambda: sink.cleared, "the held release was not applied")

        assert sink.retries == [False, True]
        await call.done_writing()
    finally:
        await h.stop()


# ---------------------------------------------------------------------------
# Log lines and metrics read while a report is held (issue #4028)
# ---------------------------------------------------------------------------

# A short server id keeps a flood well inside the stream's flow-control window,
# so the Worker's writes complete even when the reader stops reading.
_FLOOD_SERVER = "s"


def _dropped(logs: int, metrics: int) -> str:
    return (
        f"dropped {logs} log lines and {metrics} metric samples during a database hold"
    )


def _log_message(server_id: str, line: str) -> pb.WorkerMessage:
    return pb.WorkerMessage(
        event=pb.Event(server_id=server_id, log_line=pb.LogLine(line=line))
    )


def _metrics_message(server_id: str, cpu_millis: int) -> pb.WorkerMessage:
    return pb.WorkerMessage(
        event=pb.Event(server_id=server_id, metrics=pb.Metrics(cpu_millis=cpu_millis))
    )


def _command_result_message(command_id: str) -> pb.WorkerMessage:
    return pb.WorkerMessage(
        correlation_id=command_id, command_result=pb.CommandResult(success=True)
    )


class _RecordingControlPlane(ControlPlaneState):
    """Records each command result it resolves, with the reports applied by then."""

    def __init__(self, observed: list[tuple[str, str, str]]) -> None:
        super().__init__()
        self._observed = observed
        self.resolved: list[tuple[str, list[tuple[str, str, str]]]] = []

    async def resolve(
        self,
        command_id: str,
        worker_id: WorkerId,
        result: pb.CommandResult,
        *,
        retry: bool = False,
    ) -> None:
        self.resolved.append((command_id, list(self._observed)))
        await super().resolve(command_id, worker_id, result, retry=retry)


async def _write_all(
    call: aio.StreamStreamCall, messages: list[pb.WorkerMessage]
) -> None:
    async def _write() -> None:
        for message in messages:
            await call.write(message)

    await asyncio.wait_for(_write(), timeout=30)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _GRPC_SERVER_LOGGER and r.levelno >= logging.WARNING
    ]


@pytest.mark.usefixtures("fast_hold")
async def test_a_log_flood_during_a_hold_does_not_fill_the_queue(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Queued behind a held report, a busy log stream would fill the queue, stop
    # the reader, heartbeats included, and end the session with the report when
    # they lapse. The log lines and metrics read during the hold are dropped
    # instead, and the held report, the one behind it and a command result land
    # once the database is back, in order.
    sink = _OutageSink(down=True)
    control_plane = _RecordingControlPlane(sink.observed)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
        control_plane=control_plane,
        database_ping=FakeDatabasePing(reachable=False),
    )
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))
        await _wait_until(
            lambda: sink.attempts.get(_SERVER_A, 0) >= 2, "the report was not held"
        )
        await call.write(_status_message(_SERVER_B, pb.SERVER_STATE_STOPPED))
        flood = grpc_server._INBOUND_QUEUE_MAXSIZE + 100
        await _write_all(
            call,
            [_log_message(_FLOOD_SERVER, str(i)) for i in range(flood)]
            + [_metrics_message(_FLOOD_SERVER, i) for i in range(3)]
            + [_command_result_message("result-read-during-the-hold")],
        )
        beat = _T0 + dt.timedelta(seconds=1)
        h.clock.set(beat)
        await call.write(_heartbeat_message())
        await _wait_for_heartbeat_at(h, beat)

        assert h.registry.list_workers()[0].status is WorkerStatus.ONLINE
        assert h.real_time_events.published == []

        sink.down = False
        await _wait_until(lambda: control_plane.resolved, "the result not resolved")
        reports = [
            (_SERVER_A, _WORKER_ID, "running"),
            (_SERVER_B, _WORKER_ID, "stopped"),
        ]
        assert sink.observed == reports
        assert control_plane.resolved == [("result-read-during-the-hold", reports)]
        assert _logged(caplog, _dropped(flood, 3)) != []
        # Once the hold is over, log lines are relayed again.
        await call.write(_log_message(_FLOOD_SERVER, "after"))
        await _wait_until(lambda: h.real_time_events.published, "no line relayed")
        assert [e.payload["line"] for _, e in h.real_time_events.published] == ["after"]
        await call.done_writing()
    finally:
        await h.stop()


@pytest.mark.usefixtures("fast_hold")
async def test_a_hold_purges_the_log_lines_queued_before_it_started(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A report's first attempt can run as long as a connect timeout before it
    # fails and the hold starts, and a busy log stream can fill the queue
    # meanwhile, which stops the reader. As the hold starts, the log lines
    # already queued are dropped too, so the reader reads heartbeats again. A
    # report and a command result queued among them keep their place.
    gate = asyncio.Event()
    sink = _OutageSink(gate=gate)
    control_plane = _RecordingControlPlane(sink.observed)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
        control_plane=control_plane,
        database_ping=FakeDatabasePing(reachable=False),
    )
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))
        await call.write(_status_message(_SERVER_B, pb.SERVER_STATE_STOPPED))
        await call.write(_command_result_message("result-queued-before-the-hold"))
        flood = grpc_server._INBOUND_QUEUE_MAXSIZE + 100
        await _write_all(
            call, [_log_message(_FLOOD_SERVER, str(i)) for i in range(flood)]
        )
        beat = _T0 + dt.timedelta(seconds=1)
        h.clock.set(beat)
        await call.write(_heartbeat_message())
        # A's first attempt is still running, so more than the queue holds
        # stands ahead of the heartbeat.
        await asyncio.sleep(0.2)
        assert h.registry.list_workers()[0].last_heartbeat_at == _T0

        sink.down = True
        gate.set()
        await _wait_for_heartbeat_at(h, beat)
        assert h.registry.list_workers()[0].status is WorkerStatus.ONLINE

        sink.down = False
        await _wait_until(lambda: control_plane.resolved, "the result not resolved")
        reports = [
            (_SERVER_A, _WORKER_ID, "running"),
            (_SERVER_B, _WORKER_ID, "stopped"),
        ]
        assert sink.observed == reports
        assert control_plane.resolved == [("result-queued-before-the-hold", reports)]
        await call.write(_log_message(_FLOOD_SERVER, "after"))
        await _wait_until(
            lambda: (
                [e.payload["line"] for _, e in h.real_time_events.published][-1:]
                == ["after"]
            ),
            "no line relayed",
        )
        # Only a line the stopped reader had already taken can still be relayed.
        relayed = [e.payload["line"] for _, e in h.real_time_events.published][:-1]
        assert len(relayed) <= 1
        assert _logged(caplog, _dropped(flood - len(relayed), 0)) != []
        await call.done_writing()
    finally:
        await h.stop()


class _GatedFirstWriteSink(_OutageSink):
    """The first write waits for ``gate``, as one on a database slow to answer;
    every later one lands at once. ``calls`` counts every write begun, a cut one
    included."""

    def __init__(self, gate: asyncio.Event) -> None:
        super().__init__()
        self._gate: asyncio.Event | None = gate
        self.calls: dict[str, int] = {}

    async def record_observed_state(
        self, *, server_id: str, worker_id: str, state: str, **frame: Any
    ) -> bool:
        self.calls[server_id] = self.calls.get(server_id, 0) + 1
        if self._gate is not None:
            gate, self._gate = self._gate, None
            await gate.wait()
        return await super().record_observed_state(
            server_id=server_id, worker_id=worker_id, state=state, **frame
        )


@pytest.mark.usefixtures("fast_hold")
async def test_a_slow_first_attempt_is_held_while_the_session_lives(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A database that stops answering fails a statement only at its bound, long
    # after a busy log stream has filled the queue and stopped the reader, which
    # ends the session (issue #4042). The hold starts at the first attempt's
    # deadline instead and purges the queue while the session lives. The
    # attempt itself goes on, and the reports land in order once it answers.
    monkeypatch.setattr(grpc_server, "_FIRST_ATTEMPT_TIMEOUT_SECONDS", 0.2)
    answer = asyncio.Event()
    sink = _GatedFirstWriteSink(answer)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
        database_ping=FakeDatabasePing(reachable=False),
    )
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))
        await call.write(_status_message(_SERVER_B, pb.SERVER_STATE_STOPPED))
        flood = grpc_server._INBOUND_QUEUE_MAXSIZE + 100
        await _write_all(
            call, [_log_message(_FLOOD_SERVER, str(i)) for i in range(flood)]
        )
        beat = _T0 + dt.timedelta(seconds=1)
        h.clock.set(beat)
        await call.write(_heartbeat_message())

        await _wait_for_heartbeat_at(h, beat)
        assert h.registry.list_workers()[0].status is WorkerStatus.ONLINE
        assert _logged(caplog, _HOLD_STARTED) != []
        answer.set()
        await _wait_until(lambda: len(sink.observed) == 2, "the reports did not land")
        assert sink.observed == [
            (_SERVER_A, _WORKER_ID, "running"),
            (_SERVER_B, _WORKER_ID, "stopped"),
        ]
        # The first attempt was awaited, not cut and tried again.
        assert sink.calls == {_SERVER_A: 1, _SERVER_B: 1}
        await call.done_writing()
    finally:
        await h.stop()


class _FailFirstSink(FakeServerStateSink):
    """The first write fails at once with ``exc``; every later one lands."""

    def __init__(self, exc: type[Exception]) -> None:
        super().__init__()
        self._exc: type[Exception] | None = exc

    async def record_observed_state(
        self, *, server_id: str, worker_id: str, state: str, **frame: Any
    ) -> bool:
        if self._exc is not None:
            exc, self._exc = self._exc, None
            raise exc("scripted")
        return await super().record_observed_state(
            server_id=server_id, worker_id=worker_id, state=state, **frame
        )


def _relayed_lines(h: _Harness) -> list[object]:
    return [e.payload["line"] for _, e in h.real_time_events.published]


@pytest.mark.usefixtures("fast_hold")
@pytest.mark.parametrize(
    ("sink", "settled", "holds"),
    [
        pytest.param(FakeServerStateSink, "observed", 0, id="applied-at-once"),
        pytest.param(lambda: _FailFirstSink(_BUG), "dropped", 0, id="own-error"),
        pytest.param(lambda: _FailFirstSink(_OUTAGE), "observed", 1, id="outage"),
    ],
)
async def test_a_message_done_before_the_deadline_leaves_no_hold_behind(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    sink: Callable[[], FakeServerStateSink],
    settled: str,
    holds: int,
) -> None:
    # Every message arms the first attempt's deadline (review r2 of #4049). One
    # that settles before it, however it settles, must disarm it: a deadline
    # left armed would start a hold nothing ends, and the reader would drop the
    # Worker's log lines from then on.
    caplog.set_level(logging.INFO, logger=_GRPC_SERVER_LOGGER)
    deadline = 0.2
    monkeypatch.setattr(grpc_server, "_FIRST_ATTEMPT_TIMEOUT_SECONDS", deadline)
    state_sink = sink()
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=state_sink,
        database_ping=FakeDatabasePing(reachable=False),
    )
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))
        await _wait_until(
            lambda: (
                state_sink.observed
                if settled == "observed"
                else _logged(caplog, "dropping a worker message after a handler error")
            ),
            "the report was not settled",
        )
        # Past the deadline: a timer left armed fires now.
        await asyncio.sleep(deadline * 2)
        assert len(_logged(caplog, _HOLD_STARTED)) == holds
        await call.write(_log_message(_FLOOD_SERVER, "after"))
        await _wait_until(
            lambda: _relayed_lines(h) == ["after"], "the line was dropped"
        )
        await call.done_writing()
    finally:
        await h.stop()


class _SlowFailureSink(FakeServerStateSink):
    """The first write fails with ``DatabaseTimeoutError`` only after ``slow``
    seconds, as a driver bound past the first attempt's deadline does; every
    later one lands."""

    def __init__(self, slow: float) -> None:
        super().__init__()
        self._slow: float | None = slow

    async def record_observed_state(
        self, *, server_id: str, worker_id: str, state: str, **frame: Any
    ) -> bool:
        if self._slow is not None:
            slow, self._slow = self._slow, None
            await asyncio.sleep(slow)
            raise DatabaseTimeoutError("the database did not answer")
        return await super().record_observed_state(
            server_id=server_id, worker_id=worker_id, state=state, **frame
        )


@pytest.mark.usefixtures("fast_hold")
async def test_a_first_attempt_that_fails_after_its_hold_started_logs_why(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The deadline starts the hold with no error to show, so the first
    # attempt's failure, which comes later, is logged on its own, with its
    # traceback: which bound ran out (review r2 of #4049). The hold is not
    # started a second time: one WARNING that it started, one purge.
    caplog.set_level(logging.INFO, logger=_GRPC_SERVER_LOGGER)
    monkeypatch.setattr(grpc_server, "_FIRST_ATTEMPT_TIMEOUT_SECONDS", 0.2)
    purges = 0
    purge = grpc_server._Hold.start

    def _counted_purge(hold: Any) -> None:
        nonlocal purges
        purges += 1
        purge(hold)

    monkeypatch.setattr(grpc_server._Hold, "start", _counted_purge)
    sink = _SlowFailureSink(slow=0.4)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
        database_ping=FakeDatabasePing(reachable=False),
    )
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))

        await _wait_until(lambda: sink.observed, "the report did not land")
        failures = [
            r
            for r in caplog.records
            if r.name == _GRPC_SERVER_LOGGER
            and r.exc_info is not None
            and isinstance(r.exc_info[1], DatabaseTimeoutError)
        ]
        assert len(failures) == 1
        assert failures[0].levelno == logging.WARNING
        assert len(_logged(caplog, _HOLD_STARTED)) == 1
        assert purges == 1
        await call.done_writing()
    finally:
        await h.stop()


class _LateAnswerSink(FakeServerStateSink):
    """A write's commit lands at once; the first one's answer comes ``late``
    seconds after, as on a database slow to flush its log. As the real sink does,
    a write publishes its frame once its commit answers, and only when it changed
    the row."""

    def __init__(self, late: float) -> None:
        super().__init__()
        self._late = late
        self._rows: dict[str, str] = {}
        self.frames: list[tuple[str, str]] = []

    async def record_observed_state(
        self, *, server_id: str, worker_id: str, state: str, **frame: Any
    ) -> bool:
        changed = self._rows.get(server_id) != state
        self._rows[server_id] = state
        late, self._late = self._late, 0.0
        await asyncio.sleep(late)
        if changed:
            self.frames.append((server_id, state))
        return True


@pytest.mark.usefixtures("fast_hold")
async def test_a_first_attempt_past_its_deadline_still_publishes_its_frame(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A commit that lands but answers after the deadline (review r1 of #4049):
    # cut there, the attempt would publish nothing, and its retry, finding the
    # row already at the state, would stage no frame. The attempt is awaited
    # instead, so its frame reaches subscribers once its answer comes.
    caplog.set_level(logging.INFO, logger=_GRPC_SERVER_LOGGER)
    monkeypatch.setattr(grpc_server, "_FIRST_ATTEMPT_TIMEOUT_SECONDS", 0.2)
    sink = _LateAnswerSink(late=0.6)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
        database_ping=FakeDatabasePing(reachable=True),
    )
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))

        await _wait_until(
            lambda: _logged(caplog, _HOLD_APPLIED), "the report was not applied"
        )
        assert sink.frames == [(_SERVER_A, "running")]
        assert _logged(caplog, _HOLD_STARTED) != []
        await call.done_writing()
    finally:
        await h.stop()


class _SlowRetrySink(FakeServerStateSink):
    """The first write fails as an outage at once; every later one takes
    ``slow`` seconds, longer than the first attempt's deadline, and lands."""

    def __init__(self, slow: float) -> None:
        super().__init__()
        self._slow = slow
        self._failed = False

    async def record_observed_state(
        self, *, server_id: str, worker_id: str, state: str, **frame: Any
    ) -> bool:
        if not self._failed:
            self._failed = True
            raise DatabaseTimeoutError("the database did not answer")
        await asyncio.sleep(self._slow)
        return await super().record_observed_state(
            server_id=server_id, worker_id=worker_id, state=state, **frame
        )


@pytest.mark.usefixtures("fast_hold")
async def test_a_retry_slower_than_the_first_attempt_deadline_still_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only the first attempt has the deadline: a retry has the driver's bounds,
    # so a write slower than the deadline still lands once the hold is on.
    monkeypatch.setattr(grpc_server, "_FIRST_ATTEMPT_TIMEOUT_SECONDS", 0.2)
    sink = _SlowRetrySink(slow=0.5)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
        database_ping=FakeDatabasePing(reachable=False),
    )
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))

        await _wait_until(lambda: sink.observed, "the slow retry did not land")
        assert sink.observed == [(_SERVER_A, _WORKER_ID, "running")]
        await call.done_writing()
    finally:
        await h.stop()


class _RealWriteSink(FakeServerStateSink):
    """Registers the Worker as the fake does, and writes its reports through the
    real sink."""

    def __init__(self, real: ServersServerStateSink) -> None:
        super().__init__()
        self._real = real

    async def record_observed_state(
        self, *, server_id: str, worker_id: str, state: str, **frame: Any
    ) -> bool:
        return await self._real.record_observed_state(
            server_id=server_id, worker_id=worker_id, state=state, **frame
        )


@pytest.mark.usefixtures("fast_hold")
async def test_a_connect_that_hangs_is_held_while_the_session_lives(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The real sink's write, its database a host that stopped answering, and no
    # connect bound of its own: the first attempt's deadline alone starts the
    # hold, before a log flood behind it can end the session.
    monkeypatch.setattr(grpc_server, "_FIRST_ATTEMPT_TIMEOUT_SECONDS", 0.2)
    with black_hole_database() as url:
        engine = create_engine(url)
        real = ServersServerStateSink(
            create_session_factory(engine), clock=SystemClock()
        )
        h = _Harness(
            InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
            FakeClock(_T0),
            state_sink=_RealWriteSink(real),
            database_ping=FakeDatabasePing(reachable=False),
        )
        try:
            call = await _registered(await h.start())
            await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))
            flood = grpc_server._INBOUND_QUEUE_MAXSIZE + 100
            await _write_all(
                call, [_log_message(_FLOOD_SERVER, str(i)) for i in range(flood)]
            )
            beat = _T0 + dt.timedelta(seconds=1)
            h.clock.set(beat)
            await call.write(_heartbeat_message())

            await _wait_for_heartbeat_at(h, beat)
            assert h.registry.list_workers()[0].status is WorkerStatus.ONLINE
            assert _logged(caplog, _HOLD_STARTED) != []
        finally:
            await h.stop()
            await engine.dispose()


class _TimingOutSink(FakeServerStateSink):
    """Writes that time out while the database still answers its pings, as on a
    database stalled on its disk: the next ``timeouts`` raise ``timeout()``, the
    first of them only after the first attempt's deadline if ``slow_first``, and
    the rest land."""

    def __init__(
        self, timeout: Callable[[], Exception], *, timeouts: int, slow_first: bool
    ) -> None:
        super().__init__()
        self._timeout = timeout
        self._timeouts = timeouts
        self._slow = slow_first

    async def record_observed_state(
        self, *, server_id: str, worker_id: str, state: str, **frame: Any
    ) -> bool:
        if self._slow:
            self._slow = False
            await asyncio.sleep(0.4)
        if self._timeouts:
            self._timeouts -= 1
            raise self._timeout()
        return await super().record_observed_state(
            server_id=server_id, worker_id=worker_id, state=state, **frame
        )


def _statement_timeout() -> Exception:
    return DatabaseTimeoutError("the database did not answer")


def _connect_timeout() -> Exception:
    # What an engine that translates its connect outages raises for a connect
    # that ran out its bound.
    error = DatabaseConnectError("TimeoutError: ")
    error.__cause__ = TimeoutError()
    return error


@pytest.mark.usefixtures("fast_hold")
@pytest.mark.parametrize(
    ("timeout", "slow_first"),
    [
        pytest.param(_statement_timeout, False, id="statement-bound"),
        pytest.param(_connect_timeout, False, id="connect-bound"),
        pytest.param(_statement_timeout, True, id="past-the-first-attempt-deadline"),
    ],
)
async def test_a_report_that_times_out_while_the_database_answers_stays_held(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    timeout: Callable[[], Exception],
    slow_first: bool,
) -> None:
    # A database stalled on its disk answers a ping, which only reads, while its
    # writes time out. A timeout is never the message's own error, so the pings
    # answering both sides of a retry do not drop the report: it stays held and
    # lands once its write goes through (issue #4042).
    caplog.set_level(logging.INFO, logger=_GRPC_SERVER_LOGGER)
    monkeypatch.setattr(grpc_server, "_FIRST_ATTEMPT_TIMEOUT_SECONDS", 0.2)
    sink = _TimingOutSink(timeout, timeouts=3, slow_first=slow_first)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
        database_ping=FakeDatabasePing(reachable=True),
    )
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))

        await _wait_until(lambda: sink.observed, "the report did not land")
        assert sink.observed == [(_SERVER_A, _WORKER_ID, "running")]
        assert _logged(caplog, _HOLD_REFUSED) == []
        assert _logged(caplog, _HOLD_APPLIED) != []
        await call.done_writing()
    finally:
        await h.stop()


@pytest.mark.usefixtures("fast_hold")
@pytest.mark.parametrize("end", ["applied", "refused"])
async def test_a_hold_reports_what_it_dropped_in_one_warning(
    end: str, caplog: pytest.LogCaptureFixture
) -> None:
    # The lines a hold drops are counted, not logged one by one: one WARNING
    # gives both counts as the hold ends, whether its report is applied or
    # dropped because the database answers. A log line read after it is
    # relayed again.
    caplog.set_level(logging.INFO, logger=_GRPC_SERVER_LOGGER)
    sink = _OutageSink(down=True)
    ping = FakeDatabasePing(reachable=False)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
        database_ping=ping,
    )
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))
        await _wait_until(
            lambda: sink.attempts.get(_SERVER_A, 0) >= 2, "the report was not held"
        )
        await _write_all(
            call,
            [_log_message(_SERVER_A, str(i)) for i in range(3)]
            + [_metrics_message(_SERVER_A, i) for i in range(2)],
        )
        beat = _T0 + dt.timedelta(seconds=1)
        h.clock.set(beat)
        await call.write(_heartbeat_message())
        await _wait_for_heartbeat_at(h, beat)

        if end == "applied":
            sink.down = False
        else:
            ping.reachable = True
        await _wait_until(lambda: _logged(caplog, _dropped(3, 2)), "no counts logged")
        await call.write(_log_message(_SERVER_A, "after"))
        await _wait_until(lambda: h.real_time_events.published, "no line relayed")

        expected = (
            [_HOLD_STARTED, _dropped(3, 2)]
            if end == "applied"
            else [
                _HOLD_STARTED,
                _HOLD_REFUSED,
                _dropped(3, 2),
                "dropping a worker message after a handler error",
            ]
        )
        assert _warnings(caplog) == expected
        assert [e.payload["line"] for _, e in h.real_time_events.published] == ["after"]
        await call.done_writing()
    finally:
        await h.stop()


@pytest.mark.usefixtures("fast_hold")
async def test_each_hold_of_a_session_counts_only_its_own_drops(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A session can hold one report after another. Each hold's WARNING counts
    # what was dropped during that hold, not since the session began.
    sink = _OutageSink(down=True)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
        database_ping=FakeDatabasePing(reachable=False),
    )
    try:
        call = await _registered(await h.start())
        for held, (server, lines) in enumerate(((_SERVER_A, 3), (_SERVER_B, 1)), 1):
            sink.down = True
            await call.write(_status_message(server, pb.SERVER_STATE_RUNNING))
            await _wait_until(
                lambda server=server: sink.attempts.get(server, 0) >= 2,
                "the report was not held",
            )
            await _write_all(call, [_log_message(server, str(i)) for i in range(lines)])
            beat = _T0 + dt.timedelta(seconds=held)
            h.clock.set(beat)
            await call.write(_heartbeat_message())
            await _wait_for_heartbeat_at(h, beat)
            sink.down = False
            await _wait_until(
                lambda held=held: len(sink.observed) == held,
                "the held report was not applied",
            )

        assert sink.observed == [
            (_SERVER_A, _WORKER_ID, "running"),
            (_SERVER_B, _WORKER_ID, "running"),
        ]
        assert [m for m in _warnings(caplog) if m.startswith("dropped ")] == [
            _dropped(3, 0),
            _dropped(1, 0),
        ]
        await call.done_writing()
    finally:
        await h.stop()


async def test_without_a_hold_log_lines_queue_in_order_and_back_pressure_the_reader(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Outside a hold nothing is dropped. Behind a slow write, which is no outage,
    # log lines and metrics queue; the full queue stops the reader, heartbeats
    # included, and every line and sample is relayed in order once the write
    # completes.
    gate = asyncio.Event()
    sink = _OutageSink(gate=gate)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
    )
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))
        flood = grpc_server._INBOUND_QUEUE_MAXSIZE + 100
        await _write_all(
            call,
            [_log_message(_FLOOD_SERVER, str(i)) for i in range(flood)]
            + [_metrics_message(_FLOOD_SERVER, i) for i in range(3)],
        )
        beat = _T0 + dt.timedelta(seconds=1)
        h.clock.set(beat)
        await call.write(_heartbeat_message())
        await asyncio.sleep(0.2)

        assert h.registry.list_workers()[0].last_heartbeat_at == _T0
        assert h.real_time_events.published == []

        gate.set()
        await _wait_for_heartbeat_at(h, beat)
        await _wait_until(
            lambda: len(h.real_time_events.published) == flood + 3,
            "not every line and sample relayed",
        )
        assert [e.payload for _, e in h.real_time_events.published] == [
            {"line": str(i), "stream": "stdout"} for i in range(flood)
        ] + [{"cpu_millis": i, "memory_bytes": 0, "player_count": 0} for i in range(3)]
        assert sink.observed == [(_SERVER_A, _WORKER_ID, "running")]
        assert _warnings(caplog) == []
        await call.done_writing()
    finally:
        await h.stop()


# ---------------------------------------------------------------------------
# Stamping a report as it is read (issue #4118)
# ---------------------------------------------------------------------------

_SERVER_C = "cccccccc-cccc-cccc-cccc-cccccccccccc"


class _StampSink(_OutageSink):
    """Records each write's stamp, as (server, state, observed_at), as it starts.

    With ``clock``, each write moves that clock a second on, so a stamp taken
    at a later attempt differs from the one taken as the report was read.
    """

    def __init__(self, clock: FakeClock | None = None, **outage: Any) -> None:
        super().__init__(**outage)
        self._clock = clock
        self.stamps: list[tuple[str, str, dt.datetime | None]] = []

    async def record_observed_state(
        self, *, server_id: str, worker_id: str, state: str, **frame: Any
    ) -> bool:
        self.stamps.append((server_id, state, frame.get("observed_at")))
        if self._clock is not None:
            self._clock.set(self._clock.now() + dt.timedelta(seconds=1))
        return await super().record_observed_state(
            server_id=server_id, worker_id=worker_id, state=state, **frame
        )


async def test_a_superseded_sessions_report_in_flight_keeps_its_receipt_stamp() -> None:
    # A report already being written when the Worker re-registers is not
    # checked again, and can land after the newer session's report. It was
    # stamped as it was read, before the newer session registered, so the
    # newer report's later stamp keeps it whichever lands first.
    gate = asyncio.Event()
    sink = _StampSink(gate=gate)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
    )
    try:
        stub = await h.start()
        old = await _registered(stub)
        await old.write(_status_message(_SERVER_A, pb.SERVER_STATE_STARTING))
        await _wait_until(lambda: sink.stamps, "the old report's write did not start")
        later = _T0 + dt.timedelta(seconds=1)
        h.clock.set(later)
        new = await _registered(stub)
        await new.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))
        await _wait_until(
            lambda: len(sink.stamps) == 2, "the new report's write did not start"
        )
        gate.set()
        await _wait_until(lambda: len(sink.observed) == 2, "the reports not applied")

        assert sink.stamps == [
            (_SERVER_A, "starting", _T0),
            (_SERVER_A, "running", later),
        ]
        await new.done_writing()
    finally:
        await h.stop()


@pytest.mark.usefixtures("fast_hold")
async def test_a_held_reports_retries_keep_its_receipt_stamp() -> None:
    # A retry is the same report, read once. Stamped anew, a retry landing
    # after a newer write would overwrite it with the older state.
    clock = FakeClock(_T0)
    sink = _StampSink(clock, errors={_SERVER_A: [_OUTAGE] * 2})
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        clock,
        state_sink=sink,
        database_ping=FakeDatabasePing(reachable=False),
    )
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))
        await _wait_until(lambda: sink.observed, "the held report was not applied")

        assert sink.stamps == [(_SERVER_A, "running", _T0)] * 3
        await call.done_writing()
    finally:
        await h.stop()


async def test_a_report_waiting_to_be_handled_keeps_its_receipt_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Behind a slow write, B waits in the queue, and C, read once the queue is
    # full, waits for room in it. Each is stamped as it was read, not as it is
    # queued or handled.
    monkeypatch.setattr(grpc_server, "_INBOUND_QUEUE_MAXSIZE", 1)
    read: list[str] = []
    drop = grpc_server._Hold.drop

    def _noting_drop(hold: Any, message: pb.WorkerMessage) -> bool:
        read.append(message.event.server_id)
        return drop(hold, message)

    monkeypatch.setattr(grpc_server._Hold, "drop", _noting_drop)
    gate = asyncio.Event()
    sink = _StampSink(gate=gate)
    h = _Harness(
        InMemoryWorkerRegistry(clock=FakeClock(_T0), heartbeat_timeout=_TIMEOUT),
        FakeClock(_T0),
        state_sink=sink,
    )
    try:
        call = await _registered(await h.start())
        await call.write(_status_message(_SERVER_A, pb.SERVER_STATE_RUNNING))
        await _wait_until(lambda: sink.stamps, "A's write did not start")
        queued = _T0 + dt.timedelta(seconds=1)
        h.clock.set(queued)
        await call.write(_status_message(_SERVER_B, pb.SERVER_STATE_RUNNING))
        await call.write(_heartbeat_message())
        await _wait_for_heartbeat_at(h, queued)
        blocked = _T0 + dt.timedelta(seconds=2)
        h.clock.set(blocked)
        await call.write(_status_message(_SERVER_C, pb.SERVER_STATE_RUNNING))
        await _wait_until(lambda: read[-1:] == [_SERVER_C], "C was not read")
        h.clock.set(_T0 + dt.timedelta(seconds=3))
        gate.set()
        await _wait_until(lambda: len(sink.observed) == 3, "the reports not applied")

        assert [(server, stamp) for server, _, stamp in sink.stamps] == [
            (_SERVER_A, _T0),
            (_SERVER_B, queued),
            (_SERVER_C, blocked),
        ]
        await call.done_writing()
    finally:
        await h.stop()
