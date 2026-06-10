"""In-process integration tests for the control-plane gRPC server.

Starts a real grpc.aio server on an ephemeral localhost port and dials it with
a real client channel, so the full stream lifecycle is exercised end to end
(CONTROL_PLANE.md Section 4) without any database (NFR-TEST-1):

- register -> RegisterAck{accepted, heartbeat_interval};
- a wrong/missing credential is rejected with UNAUTHENTICATED;
- a non-Register first message is rejected with FAILED_PRECONDITION;
- Event{Heartbeat} refreshes liveness in the shared registry;
- closing the stream marks the Worker offline (FR-WRK-4).

These tests need no Postgres and run in the unit-runnable suite.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from collections.abc import AsyncIterator

import grpc
import pytest
from grpc import aio

from mc_server_dashboard_api.fleet.adapters.control_plane import ControlPlaneState
from mc_server_dashboard_api.fleet.adapters.grpc_server import WorkerSessionServicer
from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.domain.entities import WorkerStatus
from mc_server_dashboard_api.fleet.domain.real_time_events import EventStream
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId
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
# The API persists assigned_worker_id as a UUID column, so a Worker must
# register with a UUID-format id (CONFIGURATION.md Section 6.1, issue #99).
_WORKER_ID = "22222222-2222-2222-2222-222222222222"


def _register_message(
    worker_id: str = _WORKER_ID,
    held_servers: dict[str, int] | None = None,
) -> pb.WorkerMessage:
    caps = pb.WorkerCapabilities(
        drivers=[pb.EXECUTION_DRIVER_KIND_HOST_PROCESS],
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
        state_sink: FakeServerStateSink | None = None,
        control_plane: ControlPlaneState | None = None,
        real_time_events: RecordingRealTimeEvents | None = None,
    ) -> None:
        self.registry = registry
        self.clock = clock
        self.state_sink = state_sink or FakeServerStateSink()
        self.control_plane = control_plane or ControlPlaneState()
        self.real_time_events = real_time_events or RecordingRealTimeEvents()
        self._server: aio.Server | None = None
        self._port: int | None = None
        self._channels: list[aio.Channel] = []

    async def start(self) -> WorkerServiceStub:
        server = aio.server()
        servicer = WorkerSessionServicer(
            registry=self.registry,
            clock=self.clock,
            worker_credential=_CREDENTIAL,
            heartbeat_timeout=_TIMEOUT,
            control_plane=self.control_plane,
            state_sink=self.state_sink,
            real_time_events=self.real_time_events,
        )
        add_WorkerServiceServicer_to_server(servicer, server)
        self._port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
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
    assert response.register_ack.accepted is True
    assert response.register_ack.heartbeat_interval.ToTimedelta() == _TIMEOUT / 3
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


async def test_non_register_first_message_is_rejected(harness: _Harness) -> None:
    await harness.start()

    code = await _terminal_code(
        harness, _auth(_CREDENTIAL), message=_heartbeat_message()
    )

    assert code == grpc.StatusCode.FAILED_PRECONDITION


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
    # A transient DB error while handling ONE StatusChange must not tear down the
    # whole worker session; the bad event is logged-and-skipped and a later
    # StatusChange is still reconciled (issue #776).
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
        control_plane=control_plane,
        state_sink=FakeServerStateSink(),
        real_time_events=RecordingRealTimeEvents(),
    )

    gen = servicer.Session(_requests(), _Ctx())

    async def _drain() -> None:
        async for _ in gen:
            pass

    driver = asyncio.ensure_future(_drain())
    # Wait until the generator has opened its outbound queue and parked a getter
    # on it (the body's outbound.get()).
    queue: asyncio.Queue[pb.ApiMessage] | None = None
    for _ in range(200):
        queue = control_plane.outbound_for(WorkerId(_WORKER_ID))
        if queue is not None and queue._getters:  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0.01)
    assert queue is not None and queue._getters, (  # type: ignore[attr-defined]
        "outbound.get() should be parked on the queue"
    )

    # Abrupt RPC cancel: cancel the consumer driving the generator.
    driver.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await driver

    # The pending outbound.get() must have been cancelled, leaving no waiter.
    assert not queue._getters  # type: ignore[attr-defined]


async def test_reregister_rebuilds_assignment_count_from_tally() -> None:
    # The Worker is reported (via the authoritative tally) to be running 2 servers;
    # on (re)register the registry must rebuild its load to 2, not the reset 0.
    sink = FakeServerStateSink(running_counts={_WORKER_ID: 2})
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


async def _drain_until_heartbeat_recorded(harness: _Harness) -> None:
    import asyncio

    for _ in range(100):
        snapshot = harness.registry.list_workers()[0]
        if snapshot.last_heartbeat_at != _T0:
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


_SERVER_ID = "11111111-1111-1111-1111-111111111111"


async def test_status_change_is_published_to_real_time_events(
    harness: _Harness,
) -> None:
    stub = await harness.start()
    call = stub.Session(metadata=_auth(_CREDENTIAL))
    await call.write(_register_message())
    await call.read()  # ack

    await call.write(_status_message(_SERVER_ID, pb.SERVER_STATE_RUNNING))
    await _wait_for_published(harness, 1)

    server_id, event = harness.real_time_events.published[0]
    assert server_id == _SERVER_ID
    assert event.stream is EventStream.STATUS
    assert event.payload["state"] == "running"
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


async def test_emitted_at_is_propagated_to_published_event(
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
    await _wait_for_published(harness, 1)

    _server_id, event = harness.real_time_events.published[0]
    assert event.emitted_at == emitted
    await call.done_writing()


async def test_unset_emitted_at_falls_back_to_none(harness: _Harness) -> None:
    stub = await harness.start()
    call = stub.Session(metadata=_auth(_CREDENTIAL))
    await call.write(_register_message())
    await call.read()  # ack

    # No emitted_at set on the message: the relayed event carries None, so the
    # transport falls back to receive time.
    await call.write(_status_message(_SERVER_ID, pb.SERVER_STATE_RUNNING))
    await _wait_for_published(harness, 1)

    _server_id, event = harness.real_time_events.published[0]
    assert event.emitted_at is None
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
