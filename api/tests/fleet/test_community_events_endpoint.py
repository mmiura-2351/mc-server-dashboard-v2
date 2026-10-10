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

import asyncio
import datetime as dt
import json
import time
import uuid
from dataclasses import dataclass

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
    get_list_servers,
    get_membership_visibility,
    get_permission_checker,
    get_real_time_events,
    get_server_community_lookup,
    get_ws_reauthentication,
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
from mc_server_dashboard_api.identity.application.authenticate_request import (
    Authentication,
)
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.servers.domain.value_objects import (
    ObservedState,
    ServerId,
)
from tests.client_utils import enter_client
from tests.identity.fakes import make_authentication, make_user


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


@dataclass(frozen=True)
class _FakeServer:
    """The slice of the ``Server`` entity the snapshot reads (id + observed state)."""

    id: ServerId
    observed_state: ObservedState


class _FakeListServers:
    """Stands in for ListServers: the community's servers, read for the snapshot."""

    def __init__(self, servers: list[_FakeServer]) -> None:
        self.servers = servers

    async def __call__(self, **_kwargs: object) -> list[_FakeServer]:
        return list(self.servers)


class _Account:
    """The account behind a socket, as each re-authentication finds it (#3227).

    Stands in for the per-request user load REST performs: ``None`` once the
    account is deactivated, else the account's current row.
    """

    def __init__(self, user: User) -> None:
        self.user: User | None = user

    def deactivate(self) -> None:
        self.user = None

    async def __call__(self) -> Authentication | None:
        return None if self.user is None else make_authentication(self.user)


_shared_app: FastAPI


@pytest.fixture(autouse=True)
def _bind_shared_app(shared_app: FastAPI) -> None:
    global _shared_app
    _shared_app = shared_app


def _app(
    *,
    member: bool = True,
    allow: bool = True,
    authenticated: bool = True,
    expires_in: dt.timedelta = dt.timedelta(hours=1),
    bus: RealTimeEvents | None = None,
    lookup: dict[str, uuid.UUID] | None = None,
    list_servers: _FakeListServers | None = None,
) -> object:
    # Reuse the per-worker shared app; clear overrides on entry so a helper called
    # twice in one test starts clean (the shared_app wrapper clears between tests).
    app = _shared_app
    app.dependency_overrides.clear()
    user = make_user()

    def _user_or_none() -> object | None:
        if not authenticated:
            return None
        return make_authentication(user, expires_in=expires_in)

    app.dependency_overrides[get_current_user_ws] = _user_or_none
    app.dependency_overrides[get_ws_reauthentication] = lambda: _Account(user)
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(allow=allow)
    app.dependency_overrides[get_server_community_lookup] = lambda: _FakeLookup(
        lookup or {}
    )
    app.dependency_overrides[get_list_servers] = lambda: (
        list_servers if list_servers is not None else _FakeListServers([])
    )
    if bus is not None:
        app.dependency_overrides[get_real_time_events] = lambda: bus
    return app


def _client(app: object) -> TestClient:
    return enter_client(TestClient(app))  # type: ignore[arg-type]


def _url(community: uuid.UUID) -> str:
    return f"/api/communities/{community}/events"


def _skip_snapshot(ws: object) -> None:
    """Consume the status snapshot every community stream opens with (#1795).

    The snapshot is sent only after the subscription is registered, so receiving
    it is also the barrier after which a publish is guaranteed to be delivered.
    """

    assert ws.receive_json()["stream"] == "snapshot"  # type: ignore[attr-defined]


# --- auth / authorization before the upgrade -------------------------------


def _assert_rejected(app: object, code: int) -> None:
    client = _client(app)
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
    client = _client(app)
    with client.websocket_connect(_url(community)) as ws:
        _skip_snapshot(ws)
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
    client = _client(app)
    with client.websocket_connect(_url(community)) as ws:
        _skip_snapshot(ws)
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
    client = _client(app)
    with client.websocket_connect(_url(community_a)) as ws:
        _skip_snapshot(ws)
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


def test_log_and_metrics_events_are_not_streamed() -> None:
    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus, lookup={str(server): community})
    client = _client(app)
    with client.websocket_connect(_url(community)) as ws:
        _skip_snapshot(ws)
        # Log lines and metrics are per-server detail, not operator events;
        # only the following STATUS frame must be delivered.
        bus.publish(
            server_id=str(server),
            event=RealTimeEvent(stream=EventStream.LOG, payload={"line": "x"}),
        )
        bus.publish(
            server_id=str(server),
            event=RealTimeEvent(stream=EventStream.METRICS, payload={"cpu_millis": 1}),
        )
        bus.publish(
            server_id=str(server),
            event=RealTimeEvent(
                stream=EventStream.STATUS, payload={"state": "running"}
            ),
        )
        frame = ws.receive_json()
    assert frame["stream"] == "status"


# --- notification stream (#1836) --------------------------------------------


def test_notification_event_is_delivered_with_server_id() -> None:
    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus, lookup={str(server): community})
    client = _client(app)
    with client.websocket_connect(_url(community)) as ws:
        _skip_snapshot(ws)
        bus.publish(
            server_id=str(server),
            event=notification_event(
                kind="schedule_failed",
                title="Scheduled backup failed",
                detail="exit status 1",
            ),
        )
        frame = ws.receive_json()
    assert frame["stream"] == "notification"
    assert frame["server_id"] == str(server)
    assert frame["payload"] == {
        "kind": "schedule_failed",
        "title": "Scheduled backup failed",
        "detail": "exit status 1",
    }
    assert "ts" in frame


def test_other_communitys_notification_never_appears() -> None:
    bus = InProcessRealTimeEvents()
    community_a, community_b = uuid.uuid4(), uuid.uuid4()
    server_a, server_b = uuid.uuid4(), uuid.uuid4()
    app = _app(
        bus=bus,
        lookup={str(server_a): community_a, str(server_b): community_b},
    )
    client = _client(app)
    with client.websocket_connect(_url(community_a)) as ws:
        _skip_snapshot(ws)
        # Community B's notification is published first; it must be filtered out
        # so the only frame the A-stream sees is A's server's notification.
        bus.publish(
            server_id=str(server_b),
            event=notification_event(kind="schedule_failed", title="b"),
        )
        bus.publish(
            server_id=str(server_a),
            event=notification_event(kind="schedule_failed", title="a"),
        )
        frame = ws.receive_json()
    assert frame["server_id"] == str(server_a)
    assert frame["payload"] == {"kind": "schedule_failed", "title": "a", "detail": ""}


# --- frame encoding: exact wire bytes (#1701) -------------------------------


def test_community_frame_wire_text_is_the_exact_compact_json() -> None:
    """Pins the wire bytes of the community shape (``server_id`` appended last).

    The frame used to be serialized by Starlette's ``send_json``
    (``json.dumps(..., separators=(",", ":"), ensure_ascii=False)``); encoding
    once per event must keep the bytes identical for existing clients.
    """

    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus, lookup={str(server): community})
    client = _client(app)
    emitted = dt.datetime(2026, 6, 3, 12, 0, 0, tzinfo=dt.timezone.utc)
    with client.websocket_connect(_url(community)) as ws:
        _skip_snapshot(ws)
        bus.publish(
            server_id=str(server),
            event=RealTimeEvent(
                stream=EventStream.STATUS,
                payload={"state": "running"},
                emitted_at=emitted,
            ),
        )
        text = ws.receive_text()
    assert text == (
        '{"stream":"status","ts":"2026-06-03T12:00:00Z",'
        f'"payload":{{"state":"running"}},"server_id":"{server}"}}'
    )


# --- status snapshot on subscribe (#1795) ----------------------------------


def test_stream_opens_with_a_snapshot_of_every_community_server() -> None:
    bus = InProcessRealTimeEvents()
    community = uuid.uuid4()
    server_a, server_b = uuid.uuid4(), uuid.uuid4()
    servers = _FakeListServers(
        [
            _FakeServer(id=ServerId(server_a), observed_state=ObservedState.RUNNING),
            _FakeServer(id=ServerId(server_b), observed_state=ObservedState.STOPPED),
        ]
    )
    app = _app(bus=bus, list_servers=servers)
    client = _client(app)
    with client.websocket_connect(_url(community)) as ws:
        frame = ws.receive_json()
    assert frame["stream"] == "snapshot"
    assert frame["server_id"] is None
    assert frame["payload"] == {
        "servers": [
            {"server_id": str(server_a), "state": "running"},
            {"server_id": str(server_b), "state": "stopped"},
        ]
    }
    assert frame["ts"].endswith("Z")


def test_snapshot_wire_text_is_the_compact_community_frame_shape() -> None:
    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    servers = _FakeListServers(
        [_FakeServer(id=ServerId(server), observed_state=ObservedState.RUNNING)]
    )
    app = _app(bus=bus, list_servers=servers)
    client = _client(app)
    with client.websocket_connect(_url(community)) as ws:
        text = ws.receive_text()
    ts = json.loads(text)["ts"]
    assert text == (
        f'{{"stream":"snapshot","ts":"{ts}",'
        f'"payload":{{"servers":[{{"server_id":"{server}","state":"running"}}]}},'
        '"server_id":null}'
    )


def test_gap_is_followed_by_a_fresh_snapshot() -> None:
    bus = InProcessRealTimeEvents(max_queue=1)
    community, server = uuid.uuid4(), uuid.uuid4()
    servers = _FakeListServers(
        [_FakeServer(id=ServerId(server), observed_state=ObservedState.STARTING)]
    )
    app = _app(bus=bus, lookup={str(server): community}, list_servers=servers)
    client = _client(app)
    with client.websocket_connect(_url(community)) as ws:
        _skip_snapshot(ws)
        servers.servers = [
            _FakeServer(id=ServerId(server), observed_state=ObservedState.RUNNING)
        ]
        for i in range(3):
            bus.publish(
                server_id=str(server),
                event=RealTimeEvent(
                    stream=EventStream.STATUS, payload={"state": str(i)}
                ),
            )
        first = ws.receive_json()
        second = ws.receive_json()
        # The retained status frames predate the snapshot and are superseded
        # by it; live delivery resumes with the next transition.
        bus.publish(
            server_id=str(server),
            event=RealTimeEvent(stream=EventStream.STATUS, payload={"state": "3"}),
        )
        third = ws.receive_json()
    assert first["stream"] == "gap"
    assert second["stream"] == "snapshot"
    assert second["payload"] == {
        "servers": [{"server_id": str(server), "state": "running"}]
    }
    assert third["payload"] == {"state": "3"}


def test_buffered_status_older_than_the_post_gap_snapshot_is_discarded() -> None:
    """A retained status frame must not undo the snapshot that superseded it.

    ``running`` is published and still buffered when a later write persists
    ``unknown``. The overflow's post-gap snapshot reads ``unknown``; delivering
    the older buffered ``running`` after it would roll the client back to a
    state the snapshot had already superseded. The later write's own frame is
    left out so that the discard alone decides the outcome.
    """

    bus = InProcessRealTimeEvents(max_queue=2)
    community, server = uuid.uuid4(), uuid.uuid4()
    servers = _FakeListServers(
        [_FakeServer(id=ServerId(server), observed_state=ObservedState.RUNNING)]
    )
    app = _app(bus=bus, lookup={str(server): community}, list_servers=servers)
    client = _client(app)
    with client.websocket_connect(_url(community)) as ws:
        _skip_snapshot(ws)
        for _ in range(2):
            bus.publish(
                server_id=str(server),
                event=RealTimeEvent(
                    stream=EventStream.STATUS, payload={"state": "running"}
                ),
            )
        # The later write, as the snapshot read will see it.
        servers.servers = [
            _FakeServer(id=ServerId(server), observed_state=ObservedState.UNKNOWN)
        ]
        bus.publish(
            server_id=str(server),
            event=notification_event(kind="k", title="end"),
        )
        frames = []
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["stream"] == "notification":
                break
    states = []
    for frame in frames:
        if frame["stream"] == "status":
            states.append(frame["payload"]["state"])
        elif frame["stream"] == "snapshot":
            states.extend(entry["state"] for entry in frame["payload"]["servers"])
    assert "gap" in [frame["stream"] for frame in frames]
    assert states[-1] == "unknown"


def test_gap_that_dropped_no_status_reads_no_snapshot() -> None:
    """A notification flood overflowing a slow client costs no snapshot read."""

    bus = InProcessRealTimeEvents(max_queue=2)
    community, server = uuid.uuid4(), uuid.uuid4()

    class _CountingListServers(_FakeListServers):
        reads = 0

        async def __call__(self, **kwargs: object) -> list[_FakeServer]:
            self.reads += 1
            return await super().__call__(**kwargs)

    servers = _CountingListServers(
        [_FakeServer(id=ServerId(server), observed_state=ObservedState.RUNNING)]
    )
    app = _app(bus=bus, lookup={str(server): community}, list_servers=servers)
    client = _client(app)
    with client.websocket_connect(_url(community)) as ws:
        _skip_snapshot(ws)
        for i in range(20):
            bus.publish(
                server_id=str(server),
                event=notification_event(kind="k", title=str(i)),
            )
        bus.publish(
            server_id=str(server),
            event=notification_event(kind="k", title="end"),
        )
        frames = []
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame["stream"] == "notification" and frame["payload"]["title"] == "end":
                break
    streams = [frame["stream"] for frame in frames]
    assert "gap" in streams
    assert "snapshot" not in streams
    assert servers.reads == 1  # the subscribe snapshot only


# --- session lifetime tied to the access token (#1862) ---------------------


def test_socket_closes_4419_when_the_access_token_expires() -> None:
    # Re-authz is a minute away: the close comes from the token's own expiry.
    community = uuid.uuid4()
    client = _client(_app(expires_in=dt.timedelta(seconds=0.5)))

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(_url(community)) as ws:
            _skip_snapshot(ws)
            ws.receive_json()
    assert exc.value.code == 4419


def test_token_expiry_during_a_membership_lookup_closes_without_the_frame() -> None:
    """A lookup still in flight at expiry is abandoned; its event is never sent."""

    class _SlowLookup(_FakeLookup):
        async def __call__(self, *, server_id: str) -> uuid.UUID | None:
            await asyncio.sleep(2.0)
            return await super().__call__(server_id=server_id)

    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus, expires_in=dt.timedelta(seconds=0.5))
    app.dependency_overrides[get_server_community_lookup] = lambda: _SlowLookup(  # type: ignore[attr-defined]
        {str(server): community}
    )
    client = _client(app)

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(_url(community)) as ws:
            _skip_snapshot(ws)
            bus.publish(
                server_id=str(server),
                event=RealTimeEvent(
                    stream=EventStream.NOTIFICATION,
                    payload={"kind": "k", "title": "t", "detail": "d"},
                ),
            )
            ws.receive_json()
    assert exc.value.code == 4419


# --- mid-stream re-authorization (#3227) -----------------------------------


def test_deactivated_user_is_closed_4401_at_the_next_reauthorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deactivated account loses its open socket like its next REST call."""

    from mc_server_dashboard_api.fleet.api import events as events_module

    monkeypatch.setattr(events_module, "_REAUTHZ_INTERVAL_SECONDS", 0.05)

    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus, lookup={str(server): community})
    account = _Account(make_user())
    app.dependency_overrides[get_ws_reauthentication] = lambda: account  # type: ignore[attr-defined]
    client = _client(app)

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(_url(community)) as ws:
            _skip_snapshot(ws)
            # Several re-authorizations pass while the account is active.
            time.sleep(0.2)
            bus.publish(
                server_id=str(server),
                event=RealTimeEvent(
                    stream=EventStream.STATUS, payload={"state": "running"}
                ),
            )
            assert ws.receive_json()["payload"] == {"state": "running"}
            account.deactivate()
            ws.receive_json()
    assert exc.value.code == 4401


# --- client gone during the membership lookup (#3225) ----------------------


class _GoneDuringLookupSocket:
    """A client that disconnects while a membership lookup is in flight.

    Once the disconnect has been received, any send or close is what uvicorn
    rejects with ``RuntimeError``; the relay must notice the client is gone
    instead.
    """

    def __init__(self) -> None:
        self.scope: dict[str, object] = {}
        self.sent: list[str] = []
        self.lookup_started = asyncio.Event()
        self.gone = asyncio.Event()

    async def accept(self, subprotocol: str | None = None) -> None:
        pass

    async def receive(self) -> dict[str, object]:
        await self.lookup_started.wait()
        self.gone.set()
        return {"type": "websocket.disconnect"}

    async def send_text(self, text: str) -> None:
        if self.gone.is_set():
            raise RuntimeError(
                "Unexpected ASGI message 'websocket.send', after sending "
                "'websocket.close' or response already completed."
            )
        self.sent.append(text)

    async def close(self, code: int) -> None:
        raise RuntimeError("Unexpected ASGI message 'websocket.close'")


async def test_client_gone_during_the_membership_lookup_ends_the_relay_quietly() -> (
    None
):
    from mc_server_dashboard_api.fleet.api import events as events_module

    socket = _GoneDuringLookupSocket()
    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    user = make_user()

    async def _lookup(*, server_id: str) -> uuid.UUID | None:
        socket.lookup_started.set()
        await socket.gone.wait()
        await asyncio.sleep(0)  # the reader task observes the disconnect
        return community

    handler = asyncio.create_task(
        events_module.community_events(
            socket,  # type: ignore[arg-type]
            community,
            authentication=make_authentication(user),
            reauthenticate=_Account(user),
            visibility=_FakeVisibility(member=True),
            checker=_FakeChecker(allow=True),
            lookup=_lookup,
            list_servers=_FakeListServers([]),  # type: ignore[arg-type]
            bus=bus,
        )
    )
    # The snapshot is sent only after the subscription is registered.
    while not socket.sent:
        await asyncio.sleep(0.001)
    bus.publish(
        server_id=str(server),
        event=RealTimeEvent(stream=EventStream.STATUS, payload={"state": "running"}),
    )

    await asyncio.wait_for(handler, timeout=5)
    assert [json.loads(text)["stream"] for text in socket.sent] == ["snapshot"]


# --- connection lifecycle --------------------------------------------------


def test_disconnect_cleans_up_subscription() -> None:
    bus = InProcessRealTimeEvents()
    community = uuid.uuid4()
    app = _app(bus=bus)
    client = _client(app)
    with client.websocket_connect(_url(community)):
        assert bus.firehose_subscriber_count() == 1
    for _ in range(100):
        if bus.firehose_subscriber_count() == 0:
            break
        import time

        time.sleep(0.01)
    assert bus.firehose_subscriber_count() == 0


def test_disconnect_on_quiet_stream_releases_subscription() -> None:
    """A client gone from a community with no status traffic is cleaned up.

    The TestClient context exit *cancels* the handler task, which runs the
    cleanup regardless of the bug (#1695); the disconnect is therefore sent
    while the session is still alive — as under uvicorn, only reading the
    socket can surface it on a stream that never publishes.
    """

    bus = InProcessRealTimeEvents()
    community = uuid.uuid4()
    app = _app(bus=bus)
    client = _client(app)
    with client.websocket_connect(_url(community)) as ws:
        _skip_snapshot(ws)
        assert bus.firehose_subscriber_count() == 1
        ws.close(1000)
        for _ in range(100):
            if bus.firehose_subscriber_count() == 0:
                break
            time.sleep(0.01)
        assert bus.firehose_subscriber_count() == 0
