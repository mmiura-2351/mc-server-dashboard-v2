"""Endpoint tests for the real-time events WebSocket (Section 6.13, FR-MON-1..4).

Exercised in-process via FastAPI's TestClient with the auth/authorization Ports,
the server-ownership lookup, and the real-time bus faked (NFR-TEST-1, no DB / no
gRPC). Verifies the WebSocket handshake honours the two-layer gate *before* the
upgrade (Layer-1 no-existence-signal posture preserved), that frames flow for all
three streams, that ``?streams=`` filters, that a slow consumer gets a gap frame,
and that a client disconnect cleans up its subscription (no leak).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mc_server_dashboard_api.community.domain.permission_checker import (
    MembershipVisibility,
    PermissionChecker,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    AuthUser,
    CommunityId,
    Permission,
    ResourceRef,
    UserId,
)
from mc_server_dashboard_api.dependencies import (
    get_current_user_ws,
    get_membership_visibility,
    get_permission_checker,
    get_read_server,
    get_real_time_events,
)
from mc_server_dashboard_api.fleet.adapters.real_time_events import (
    InProcessRealTimeEvents,
)
from mc_server_dashboard_api.fleet.domain.real_time_events import (
    EventStream,
    RealTimeEvent,
    RealTimeEvents,
    notification_event,
)
from mc_server_dashboard_api.servers.domain.errors import ServerNotFoundError
from tests.identity.fakes import make_user


class _FakeVisibility(MembershipVisibility):
    def __init__(self, *, member: bool) -> None:
        self._member = member

    async def is_member(self, *, user_id: UserId, community_id: CommunityId) -> bool:
        return self._member


class _FakeChecker(PermissionChecker):
    def __init__(self, *, allow: bool) -> None:
        self._allow = allow

    async def can(
        self, *, user: AuthUser, operation: Permission, resource: ResourceRef
    ) -> bool:
        return self._allow


class _FakeReadServer:
    """Stands in for the ReadServer ownership check: found, or cross-community."""

    def __init__(self, *, found: bool) -> None:
        self._found = found

    async def __call__(self, **_kwargs: object) -> object:
        if not self._found:
            raise ServerNotFoundError("x")
        return object()


_shared_app: FastAPI


@pytest.fixture(autouse=True)
def _bind_shared_app(shared_app: FastAPI) -> None:
    global _shared_app
    _shared_app = shared_app


def _app(
    *,
    member: bool = True,
    allow: bool = True,
    found: bool = True,
    authenticated: bool = True,
    bus: RealTimeEvents | None = None,
) -> object:
    # Reuse the per-worker shared app; clear overrides on entry so a helper called
    # twice in one test starts clean (the shared_app wrapper clears between tests).
    app = _shared_app
    app.dependency_overrides.clear()
    user = make_user()

    def _user_or_none() -> object | None:
        return user if authenticated else None

    app.dependency_overrides[get_current_user_ws] = _user_or_none
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(allow=allow)
    app.dependency_overrides[get_read_server] = lambda: _FakeReadServer(found=found)
    if bus is not None:
        app.dependency_overrides[get_real_time_events] = lambda: bus
    return app


def _client(app: object) -> Iterator[TestClient]:
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client


def _url(
    community: uuid.UUID, server: uuid.UUID, streams: str = "status,log,metrics"
) -> str:
    return f"/api/communities/{community}/servers/{server}/events?streams={streams}"


# --- auth / authorization before the upgrade -------------------------------


def _assert_rejected(app: object, code: int) -> None:
    # A pre-accept close surfaces in the TestClient at connect time (the upgrade
    # never completes), so the handshake never reaches the client as accepted.
    client = next(_client(app))
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(_url(uuid.uuid4(), uuid.uuid4())):
            pass
    assert exc.value.code == code


def test_unauthenticated_is_closed_before_accept() -> None:
    _assert_rejected(_app(authenticated=False), 4401)


def test_non_member_gets_no_existence_signal() -> None:
    _assert_rejected(_app(member=False), 4404)


def test_member_without_permission_is_closed() -> None:
    _assert_rejected(_app(member=True, allow=False), 4403)


def test_cross_community_server_gets_same_404_as_unknown() -> None:
    _assert_rejected(_app(member=True, allow=True, found=False), 4404)


# --- end-to-end frame flow -------------------------------------------------


def test_status_event_is_delivered_as_a_frame() -> None:
    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus)
    client = next(_client(app))
    with client.websocket_connect(_url(community, server)) as ws:
        bus.publish(
            server_id=str(server),
            event=RealTimeEvent(
                stream=EventStream.STATUS, payload={"state": "running"}
            ),
        )
        frame = ws.receive_json()
    assert frame["stream"] == "status"
    assert frame["payload"] == {"state": "running"}
    assert "ts" in frame


def test_log_and_metrics_events_are_delivered() -> None:
    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus)
    client = next(_client(app))
    with client.websocket_connect(_url(community, server)) as ws:
        bus.publish(
            server_id=str(server),
            event=RealTimeEvent(stream=EventStream.LOG, payload={"line": "hi"}),
        )
        bus.publish(
            server_id=str(server),
            event=RealTimeEvent(stream=EventStream.METRICS, payload={"cpu_millis": 1}),
        )
        log = ws.receive_json()
        metrics = ws.receive_json()
    assert log["stream"] == "log"
    assert metrics["stream"] == "metrics"


def test_streams_query_filters_delivered_events() -> None:
    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus)
    client = next(_client(app))
    with client.websocket_connect(_url(community, server, streams="status")) as ws:
        bus.publish(
            server_id=str(server),
            event=RealTimeEvent(stream=EventStream.LOG, payload={"line": "x"}),
        )
        bus.publish(
            server_id=str(server),
            event=RealTimeEvent(
                stream=EventStream.STATUS, payload={"state": "running"}
            ),
        )
        frame = ws.receive_json()
    # The LOG event was filtered; the first frame is the STATUS event.
    assert frame["stream"] == "status"


def test_notification_stream_is_subscribable_and_delivered() -> None:
    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus)
    client = next(_client(app))
    with client.websocket_connect(
        _url(community, server, streams="notification")
    ) as ws:
        bus.publish(
            server_id=str(server),
            event=notification_event(
                kind="schedule_failed",
                title="Scheduled restart failed",
                detail="worker unavailable",
            ),
        )
        frame = ws.receive_json()
    assert frame["stream"] == "notification"
    assert frame["payload"] == {
        "kind": "schedule_failed",
        "title": "Scheduled restart failed",
        "detail": "worker unavailable",
    }
    assert "ts" in frame


def test_slow_consumer_receives_a_gap_frame() -> None:
    bus = InProcessRealTimeEvents(max_queue=1)
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus)
    client = next(_client(app))
    with client.websocket_connect(_url(community, server)) as ws:
        for i in range(3):
            bus.publish(
                server_id=str(server),
                event=RealTimeEvent(
                    stream=EventStream.STATUS, payload={"state": str(i)}
                ),
            )
        first = ws.receive_json()
        second = ws.receive_json()
    assert first["stream"] == "gap"
    assert second["payload"] == {"state": "2"}


# --- frame ts carries the worker's emitted_at -----------------------------


def test_frame_ts_uses_worker_emitted_at() -> None:
    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus)
    client = next(_client(app))
    emitted = dt.datetime(2026, 6, 3, 12, 0, 0, tzinfo=dt.timezone.utc)
    with client.websocket_connect(_url(community, server)) as ws:
        bus.publish(
            server_id=str(server),
            event=RealTimeEvent(
                stream=EventStream.STATUS,
                payload={"state": "running"},
                emitted_at=emitted,
            ),
        )
        frame = ws.receive_json()
    # The wire frame ``ts`` is the canonical RFC 3339 ``Z`` form (#674), not the
    # ``+00:00`` offset that ``datetime.isoformat()`` would emit for UTC.
    assert frame["ts"] == "2026-06-03T12:00:00Z"


def test_frame_ts_falls_back_to_receive_time_when_unset() -> None:
    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus)
    client = next(_client(app))
    before = dt.datetime.now(dt.timezone.utc)
    with client.websocket_connect(_url(community, server)) as ws:
        bus.publish(
            server_id=str(server),
            event=RealTimeEvent(
                stream=EventStream.STATUS, payload={"state": "running"}
            ),
        )
        frame = ws.receive_json()
    after = dt.datetime.now(dt.timezone.utc)
    assert frame["ts"].endswith("Z")
    ts = dt.datetime.fromisoformat(frame["ts"])
    assert before <= ts <= after


# --- frame encoding: exact wire bytes, shared across subscribers (#1701) ---


def test_frame_wire_text_is_the_exact_compact_json() -> None:
    """Pins the wire bytes: key order, compact separators, unescaped non-ASCII.

    The frame used to be serialized by Starlette's ``send_json``
    (``json.dumps(..., separators=(",", ":"), ensure_ascii=False)``); encoding
    once per event must keep the bytes identical for existing clients.
    """

    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus)
    client = next(_client(app))
    emitted = dt.datetime(2026, 6, 3, 12, 0, 0, tzinfo=dt.timezone.utc)
    with client.websocket_connect(_url(community, server)) as ws:
        bus.publish(
            server_id=str(server),
            event=RealTimeEvent(
                stream=EventStream.LOG,
                payload={"line": "héllo", "stream": "stdout"},
                emitted_at=emitted,
            ),
        )
        text = ws.receive_text()
    assert text == (
        '{"stream":"log","ts":"2026-06-03T12:00:00Z",'
        '"payload":{"line":"héllo","stream":"stdout"}}'
    )


def test_frame_is_encoded_once_for_many_subscribers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One event fanned out to N subscribers builds its frame once (#1701)."""

    from mc_server_dashboard_api.fleet.api import events as events_module

    calls = 0
    real_frame = events_module._frame

    def _counting_frame(event: RealTimeEvent) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return real_frame(event)

    monkeypatch.setattr(events_module, "_frame", _counting_frame)

    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus)
    client = next(_client(app))
    with (
        client.websocket_connect(_url(community, server)) as ws1,
        client.websocket_connect(_url(community, server)) as ws2,
        client.websocket_connect(_url(community, server)) as ws3,
    ):
        # All three subscriptions must be registered before the publish.
        for _ in range(100):
            if bus.subscriber_count(str(server)) == 3:
                break
            time.sleep(0.01)
        assert bus.subscriber_count(str(server)) == 3
        bus.publish(
            server_id=str(server),
            event=RealTimeEvent(
                stream=EventStream.STATUS, payload={"state": "running"}
            ),
        )
        frames = [ws.receive_json() for ws in (ws1, ws2, ws3)]
    assert all(frame == frames[0] for frame in frames)
    assert calls == 1


def test_gap_marker_frame_is_never_cached() -> None:
    """The adapter reuses one GAP instance across subscriptions and time; a
    cached encoding would freeze its send-time ``ts`` at the first gap forever.
    """

    from mc_server_dashboard_api.fleet.api import events as events_module

    gap = RealTimeEvent(stream=EventStream.GAP)
    calls = 0

    def _build(event: RealTimeEvent) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return events_module._frame(event)

    events_module._encoded(gap, events_module._FRAME_SLOT, _build)
    events_module._encoded(gap, events_module._FRAME_SLOT, _build)
    assert calls == 2


# --- streams parameter: omitted=all, present-but-invalid=rejected ----------


def test_unknown_stream_token_is_rejected_before_accept() -> None:
    bus = InProcessRealTimeEvents()
    app = _app(bus=bus)
    client = next(_client(app))
    community, server = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(_url(community, server, streams="bogus")):
            pass
    assert exc.value.code == 4400
    # Rejected before accept, so no subscription was ever created.
    assert bus.subscriber_count(str(server)) == 0


def test_omitted_streams_subscribes_to_all() -> None:
    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus)
    client = next(_client(app))
    url = f"/api/communities/{community}/servers/{server}/events"
    cases: list[tuple[EventStream, dict[str, object]]] = [
        (EventStream.STATUS, {"state": "running"}),
        (EventStream.LOG, {"line": "hi"}),
        (EventStream.METRICS, {"cpu_millis": 1}),
        (EventStream.NOTIFICATION, {"kind": "k", "title": "t", "detail": ""}),
    ]
    with client.websocket_connect(url) as ws:
        for stream, payload in cases:
            bus.publish(
                server_id=str(server),
                event=RealTimeEvent(stream=stream, payload=payload),
            )
        delivered = {ws.receive_json()["stream"] for _ in range(4)}
    assert delivered == {"status", "log", "metrics", "notification"}


# --- mid-stream revocation -------------------------------------------------


class _FlippableChecker(PermissionChecker):
    """Allows until ``revoke()`` is called, then denies (mid-stream revocation)."""

    def __init__(self) -> None:
        self._allow = True

    def revoke(self) -> None:
        self._allow = False

    async def can(
        self, *, user: AuthUser, operation: Permission, resource: ResourceRef
    ) -> bool:
        return self._allow


def test_mid_stream_revocation_closes_with_policy_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Shrink the idle re-check window so the test does not wait a real minute.
    from mc_server_dashboard_api.fleet.api import events as events_module

    monkeypatch.setattr(events_module, "_REAUTHZ_INTERVAL_SECONDS", 0.05)

    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    checker = _FlippableChecker()
    app = _shared_app
    app.dependency_overrides.clear()
    user = make_user()
    app.dependency_overrides[get_current_user_ws] = lambda: user
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=True
    )
    app.dependency_overrides[get_permission_checker] = lambda: checker
    app.dependency_overrides[get_read_server] = lambda: _FakeReadServer(found=True)
    app.dependency_overrides[get_real_time_events] = lambda: bus
    client = next(_client(app))

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(_url(community, server)) as ws:
            # A frame published before the flip is still delivered.
            bus.publish(
                server_id=str(server),
                event=RealTimeEvent(
                    stream=EventStream.STATUS, payload={"state": "running"}
                ),
            )
            frame = ws.receive_json()
            assert frame["payload"] == {"state": "running"}
            # Revoke; the next idle re-check must close the socket.
            checker.revoke()
            ws.receive_json()
    assert exc.value.code == 4403


def test_disconnect_cleans_up_subscription() -> None:
    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus)
    client = next(_client(app))
    with client.websocket_connect(_url(community, server)):
        assert bus.subscriber_count(str(server)) == 1
    # Leaving the context disconnects the client; the subscription is released.
    for _ in range(100):
        if bus.subscriber_count(str(server)) == 0:
            break
        import time

        time.sleep(0.01)
    assert bus.subscriber_count(str(server)) == 0


def test_disconnect_on_quiet_topic_releases_subscription() -> None:
    """A client that vanishes from a topic with NO traffic is still cleaned up.

    The TestClient context exit *cancels* the handler task outright, which would
    run the cleanup regardless of the bug (#1695). So the disconnect is sent
    while the session is still alive: exactly as under uvicorn, the handler can
    only notice it by reading the socket — nothing else ever wakes it on a topic
    that never publishes.
    """

    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus)
    client = next(_client(app))
    with client.websocket_connect(_url(community, server)) as ws:
        assert bus.subscriber_count(str(server)) == 1
        ws.close(1000)
        for _ in range(100):
            if bus.subscriber_count(str(server)) == 0:
                break
            time.sleep(0.01)
        assert bus.subscriber_count(str(server)) == 0


def test_no_reauthz_queries_after_disconnect_on_quiet_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disconnected client's handler stops re-running the authz gate (#1695)."""

    from mc_server_dashboard_api.fleet.api import events as events_module

    monkeypatch.setattr(events_module, "_REAUTHZ_INTERVAL_SECONDS", 0.05)

    class _CountingChecker(PermissionChecker):
        def __init__(self) -> None:
            self.calls = 0

        async def can(
            self, *, user: AuthUser, operation: Permission, resource: ResourceRef
        ) -> bool:
            self.calls += 1
            return True

    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    checker = _CountingChecker()
    app = _shared_app
    app.dependency_overrides.clear()
    user = make_user()
    app.dependency_overrides[get_current_user_ws] = lambda: user
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=True
    )
    app.dependency_overrides[get_permission_checker] = lambda: checker
    app.dependency_overrides[get_read_server] = lambda: _FakeReadServer(found=True)
    app.dependency_overrides[get_real_time_events] = lambda: bus
    client = next(_client(app))

    with client.websocket_connect(_url(community, server)) as ws:
        ws.close(1000)
        # Once the subscription is released the handler has exited its loop, so
        # the call count observed afterwards can only change if a zombie re-authz
        # survived the disconnect.
        for _ in range(100):
            if bus.subscriber_count(str(server)) == 0:
                break
            time.sleep(0.01)
        assert bus.subscriber_count(str(server)) == 0
        calls_after_release = checker.calls
        time.sleep(0.2)  # several re-authz intervals
        assert checker.calls == calls_after_release


def test_client_sent_data_is_ignored_and_delivery_continues() -> None:
    """The socket is send-only: client frames are read and discarded (#1695)."""

    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus)
    client = next(_client(app))
    with client.websocket_connect(_url(community, server)) as ws:
        ws.send_text("ping")
        bus.publish(
            server_id=str(server),
            event=RealTimeEvent(
                stream=EventStream.STATUS, payload={"state": "running"}
            ),
        )
        frame = ws.receive_json()
    assert frame["stream"] == "status"


async def test_cancellation_while_parked_leaves_no_orphan_tasks() -> None:
    """Server shutdown cancels a parked handler; its helper tasks die with it.

    Drives the delivery loop directly: cancelled while idle (no events, client
    still connected), it must tear down its companion reader and pending-event
    tasks before the cancellation propagates — an orphaned reader would outlive
    every connection parked at shutdown.
    """

    from mc_server_dashboard_api.fleet.api import events as events_module

    class _ParkedSocket:
        async def receive(self) -> dict[str, object]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    bus = InProcessRealTimeEvents()
    subscription = bus.subscribe(server_id="s", streams=frozenset({EventStream.STATUS}))

    async def _reauthorize() -> int | None:
        return None

    async def _deliver(event: RealTimeEvent) -> None:
        raise AssertionError("no events are published in this test")

    task = asyncio.create_task(
        events_module._relay(
            _ParkedSocket(),  # type: ignore[arg-type]
            subscription,
            reauthorize=_reauthorize,
            deliver=_deliver,
        )
    )
    await asyncio.sleep(0.01)  # let the loop park on its helper tasks
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await subscription.aclose()
    # The helpers are cancelled without being awaited (the teardown must not
    # suspend); give the loop a few ticks to collect them, then require that
    # nothing survived.
    current = asyncio.current_task()
    for _ in range(10):
        leftovers = {t for t in asyncio.all_tasks() if t is not current}
        if not leftovers:
            break
        await asyncio.wait(leftovers, timeout=1)
    assert {t for t in asyncio.all_tasks() if t is not current} == set()
