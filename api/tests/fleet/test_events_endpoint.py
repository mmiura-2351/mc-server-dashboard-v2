"""Endpoint tests for the real-time events WebSocket (Section 6.13, FR-MON-1..4).

Exercised in-process via FastAPI's TestClient with the auth/authorization Ports,
the server-ownership lookup, and the real-time bus faked (NFR-TEST-1, no DB / no
gRPC). Verifies the WebSocket handshake honours the two-layer gate *before* the
upgrade (Layer-1 no-existence-signal posture preserved), that frames flow for all
three streams, that ``?streams=`` filters, that a slow consumer gets a gap frame,
and that a client disconnect cleans up its subscription (no leak).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mc_server_dashboard_api.app import create_app
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


def _app(
    *,
    member: bool = True,
    allow: bool = True,
    found: bool = True,
    authenticated: bool = True,
    bus: RealTimeEvents | None = None,
) -> object:
    app = create_app()
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
    ]
    with client.websocket_connect(url) as ws:
        for stream, payload in cases:
            bus.publish(
                server_id=str(server),
                event=RealTimeEvent(stream=stream, payload=payload),
            )
        delivered = {ws.receive_json()["stream"] for _ in range(3)}
    assert delivered == {"status", "log", "metrics"}


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
    app = create_app()
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
