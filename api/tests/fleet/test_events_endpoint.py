"""Endpoint tests for the real-time events WebSocket (Section 6.13, FR-MON-1..4).

Exercised in-process via FastAPI's TestClient with the auth/authorization Ports,
the server-ownership lookup, and the real-time bus faked (NFR-TEST-1, no DB / no
gRPC). Verifies the WebSocket handshake honours the two-layer gate *before* the
upgrade (Layer-1 no-existence-signal posture preserved), that frames flow for all
three streams, that ``?streams=`` filters, that a slow consumer gets a gap frame,
and that a client disconnect cleans up its subscription (no leak).
"""

from __future__ import annotations

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
    return f"/communities/{community}/servers/{server}/events?streams={streams}"


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
