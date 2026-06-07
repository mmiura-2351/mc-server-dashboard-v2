"""Endpoint tests for the community-scoped operator events WebSocket (#288).

``WS /communities/{community_id}/events`` streams server status-change events for
*all* servers of one community as typed JSON frames. Authorization is the same
two-layer gate as the per-server stream but at community level (``server:read``
with no specific resource), enforced *before* the upgrade so the Layer-1
no-existence-signal posture holds during the handshake (Section 6.4).

Exercised in-process via FastAPI's TestClient with the auth/authorization Ports,
the server->community lookup, and the real-time bus faked (NFR-TEST-1, no DB / no
gRPC): the accept-time gate, the frame shape (carries ``server_id``), fan-out
across two servers of the community, cross-community isolation, and disconnect
cleanup of the firehose subscription.
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
    get_real_time_events,
    get_server_community_lookup,
)
from mc_server_dashboard_api.fleet.adapters.real_time_events import (
    InProcessRealTimeEvents,
)
from mc_server_dashboard_api.fleet.domain.real_time_events import (
    EventStream,
    RealTimeEvent,
    RealTimeEvents,
)
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


class _FakeLookup:
    """Stands in for the server->community lookup: server_id (str) -> community."""

    def __init__(self, mapping: dict[str, uuid.UUID]) -> None:
        self._mapping = mapping

    async def __call__(self, *, server_id: str) -> uuid.UUID | None:
        return self._mapping.get(server_id)


def _app(
    *,
    member: bool = True,
    allow: bool = True,
    authenticated: bool = True,
    bus: RealTimeEvents | None = None,
    lookup: dict[str, uuid.UUID] | None = None,
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
    app.dependency_overrides[get_server_community_lookup] = lambda: _FakeLookup(
        lookup or {}
    )
    if bus is not None:
        app.dependency_overrides[get_real_time_events] = lambda: bus
    return app


def _client(app: object) -> Iterator[TestClient]:
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client


def _url(community: uuid.UUID) -> str:
    return f"/api/communities/{community}/events"


# --- auth / authorization before the upgrade -------------------------------


def _assert_rejected(app: object, code: int) -> None:
    client = next(_client(app))
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(_url(uuid.uuid4())):
            pass
    assert exc.value.code == code


def test_unauthenticated_is_closed_before_accept() -> None:
    _assert_rejected(_app(authenticated=False), 4401)


def test_non_member_gets_no_existence_signal() -> None:
    _assert_rejected(_app(member=False), 4404)


def test_member_without_permission_is_closed() -> None:
    _assert_rejected(_app(member=True, allow=False), 4403)


# --- frame shape + fan-out -------------------------------------------------


def test_status_event_is_delivered_with_server_id() -> None:
    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus, lookup={str(server): community})
    client = next(_client(app))
    with client.websocket_connect(_url(community)) as ws:
        bus.publish(
            server_id=str(server),
            event=RealTimeEvent(
                stream=EventStream.STATUS, payload={"state": "running"}
            ),
        )
        frame = ws.receive_json()
    assert frame["stream"] == "status"
    assert frame["server_id"] == str(server)
    assert frame["payload"] == {"state": "running"}
    assert "ts" in frame


def test_fan_out_two_servers_in_community_both_arrive() -> None:
    bus = InProcessRealTimeEvents()
    community = uuid.uuid4()
    server_a, server_b = uuid.uuid4(), uuid.uuid4()
    app = _app(
        bus=bus,
        lookup={str(server_a): community, str(server_b): community},
    )
    client = next(_client(app))
    with client.websocket_connect(_url(community)) as ws:
        bus.publish(
            server_id=str(server_a),
            event=RealTimeEvent(stream=EventStream.STATUS, payload={"state": "a"}),
        )
        bus.publish(
            server_id=str(server_b),
            event=RealTimeEvent(stream=EventStream.STATUS, payload={"state": "b"}),
        )
        delivered = {ws.receive_json()["server_id"] for _ in range(2)}
    assert delivered == {str(server_a), str(server_b)}


def test_other_communitys_server_never_appears() -> None:
    bus = InProcessRealTimeEvents()
    community_a, community_b = uuid.uuid4(), uuid.uuid4()
    server_a, server_b = uuid.uuid4(), uuid.uuid4()
    app = _app(
        bus=bus,
        lookup={str(server_a): community_a, str(server_b): community_b},
    )
    client = next(_client(app))
    with client.websocket_connect(_url(community_a)) as ws:
        # Community B's server is published first; it must be filtered out so the
        # only frame the A-stream sees is A's server.
        bus.publish(
            server_id=str(server_b),
            event=RealTimeEvent(stream=EventStream.STATUS, payload={"state": "b"}),
        )
        bus.publish(
            server_id=str(server_a),
            event=RealTimeEvent(stream=EventStream.STATUS, payload={"state": "a"}),
        )
        frame = ws.receive_json()
    assert frame["server_id"] == str(server_a)
    assert frame["payload"] == {"state": "a"}


def test_only_status_events_are_streamed() -> None:
    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus, lookup={str(server): community})
    client = next(_client(app))
    with client.websocket_connect(_url(community)) as ws:
        # A log line for a community server is not an operator status transition;
        # only the following STATUS frame must be delivered.
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
    assert frame["stream"] == "status"


# --- connection lifecycle --------------------------------------------------


def test_disconnect_cleans_up_subscription() -> None:
    bus = InProcessRealTimeEvents()
    community = uuid.uuid4()
    app = _app(bus=bus)
    client = next(_client(app))
    with client.websocket_connect(_url(community)):
        assert bus.firehose_subscriber_count() == 1
    for _ in range(100):
        if bus.firehose_subscriber_count() == 0:
            break
        import time

        time.sleep(0.01)
    assert bus.firehose_subscriber_count() == 0
