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
from mc_server_dashboard_api.servers.domain.value_objects import ObservedState
from tests.client_utils import enter_client
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


@dataclass(frozen=True)
class _FakeServer:
    """The slice of the ``Server`` entity the endpoint reads (its observed state)."""

    observed_state: ObservedState


class _FakeReadServer:
    """Stands in for ReadServer: the ownership check and the snapshot read.

    ``found`` decides the ownership check (cross-community -> not found); a found
    server reports ``state`` as its observed state, which the on-subscribe
    snapshot frame carries (#1795).
    """

    def __init__(
        self, *, found: bool, state: ObservedState = ObservedState.RUNNING
    ) -> None:
        self._found = found
        self.state = state

    async def __call__(self, **_kwargs: object) -> object:
        if not self._found:
            raise ServerNotFoundError("x")
        return _FakeServer(observed_state=self.state)


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
    read_server: object | None = None,
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
    app.dependency_overrides[get_read_server] = lambda: (
        read_server if read_server is not None else _FakeReadServer(found=found)
    )
    if bus is not None:
        app.dependency_overrides[get_real_time_events] = lambda: bus
    return app


def _client(app: object) -> TestClient:
    return enter_client(TestClient(app))  # type: ignore[arg-type]


def _url(
    community: uuid.UUID, server: uuid.UUID, streams: str = "status,log,metrics"
) -> str:
    return f"/api/communities/{community}/servers/{server}/events?streams={streams}"


def _skip_snapshot(ws: object) -> None:
    """Consume the status snapshot every STATUS subscription opens with (#1795).

    The snapshot is sent only after the subscription is registered, so receiving
    it is also the barrier after which a publish is guaranteed to be delivered.
    """

    assert ws.receive_json()["stream"] == "snapshot"  # type: ignore[attr-defined]


# --- auth / authorization before the upgrade -------------------------------


def _assert_rejected(app: object, code: int) -> None:
    # A pre-accept close surfaces in the TestClient at connect time (the upgrade
    # never completes), so the handshake never reaches the client as accepted.
    client = _client(app)
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
    client = _client(app)
    with client.websocket_connect(_url(community, server)) as ws:
        _skip_snapshot(ws)
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
    client = _client(app)
    with client.websocket_connect(_url(community, server)) as ws:
        _skip_snapshot(ws)
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
    client = _client(app)
    with client.websocket_connect(_url(community, server, streams="status")) as ws:
        _skip_snapshot(ws)
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
    client = _client(app)
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


def test_slow_consumer_receives_a_gap_frame_then_a_fresh_snapshot() -> None:
    # The dropped status frames are never replayed, so the gap is followed by a
    # fresh snapshot (#1795) before the retained window resumes.
    bus = InProcessRealTimeEvents(max_queue=1)
    community, server = uuid.uuid4(), uuid.uuid4()
    read_server = _FakeReadServer(found=True, state=ObservedState.STARTING)
    app = _app(bus=bus, read_server=read_server)
    client = _client(app)
    with client.websocket_connect(_url(community, server)) as ws:
        _skip_snapshot(ws)
        read_server.state = ObservedState.RUNNING
        for i in range(3):
            bus.publish(
                server_id=str(server),
                event=RealTimeEvent(
                    stream=EventStream.STATUS, payload={"state": str(i)}
                ),
            )
        first = ws.receive_json()
        second = ws.receive_json()
        third = ws.receive_json()
    assert first["stream"] == "gap"
    assert second["stream"] == "snapshot"
    assert second["payload"] == {"state": "running"}
    assert third["payload"] == {"state": "2"}


def _drain_until_log(ws: object, line: str) -> list[dict[str, object]]:
    """Receive frames up to and including the log frame carrying ``line``."""

    frames: list[dict[str, object]] = []
    while True:
        frame = ws.receive_json()  # type: ignore[attr-defined]
        frames.append(frame)
        if frame["stream"] == "log" and frame["payload"] == {"line": line}:
            return frames


def _final_state(frames: list[dict[str, object]]) -> object:
    """The state a client holds after applying every status/snapshot frame."""

    states = [
        frame["payload"]["state"]  # type: ignore[index]
        for frame in frames
        if frame["stream"] in ("status", "snapshot")
    ]
    return states[-1]


def test_buffered_status_older_than_the_post_gap_snapshot_is_discarded() -> None:
    """A retained status frame must not undo the snapshot that superseded it.

    ``running`` is published and still buffered when the worker disconnects:
    that write commits ``unknown`` without publishing anything. The overflow's
    post-gap snapshot reads ``unknown``; delivering the older buffered
    ``running`` after it would leave the client on ``running`` indefinitely.
    """

    bus = InProcessRealTimeEvents(max_queue=2)
    community, server = uuid.uuid4(), uuid.uuid4()
    read_server = _FakeReadServer(found=True, state=ObservedState.RUNNING)
    app = _app(bus=bus, read_server=read_server)
    client = _client(app)
    with client.websocket_connect(_url(community, server, "status,log")) as ws:
        _skip_snapshot(ws)
        for _ in range(2):
            bus.publish(
                server_id=str(server),
                event=RealTimeEvent(
                    stream=EventStream.STATUS, payload={"state": "running"}
                ),
            )
        # The worker-disconnect write: persisted, never published.
        read_server.state = ObservedState.UNKNOWN
        bus.publish(
            server_id=str(server),
            event=RealTimeEvent(stream=EventStream.LOG, payload={"line": "end"}),
        )
        frames = _drain_until_log(ws, "end")
    assert "gap" in [frame["stream"] for frame in frames]
    assert _final_state(frames) == "unknown"


def test_gap_on_a_stream_without_status_sends_no_snapshot() -> None:
    bus = InProcessRealTimeEvents(max_queue=1)
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus)
    client = _client(app)
    with client.websocket_connect(_url(community, server, streams="log")) as ws:
        _await_subscribers(bus, str(server), 1)
        for i in range(3):
            bus.publish(
                server_id=str(server),
                event=RealTimeEvent(stream=EventStream.LOG, payload={"line": str(i)}),
            )
        first = ws.receive_json()
        second = ws.receive_json()
    assert first["stream"] == "gap"
    assert second["payload"] == {"line": "2"}


# --- status snapshot on subscribe (#1795) ----------------------------------


def _await_subscribers(bus: InProcessRealTimeEvents, server: str, count: int) -> None:
    for _ in range(100):
        if bus.subscriber_count(server) == count:
            break
        time.sleep(0.01)
    assert bus.subscriber_count(server) == count


def test_status_subscription_opens_with_a_snapshot_of_the_observed_state() -> None:
    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    read_server = _FakeReadServer(found=True, state=ObservedState.CRASHED)
    app = _app(bus=bus, read_server=read_server)
    client = _client(app)
    with client.websocket_connect(_url(community, server)) as ws:
        frame = ws.receive_json()
    assert frame["stream"] == "snapshot"
    assert frame["payload"] == {"state": "crashed"}
    assert frame["ts"].endswith("Z")


def test_snapshot_wire_text_is_the_compact_frame_shape() -> None:
    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus)
    client = _client(app)
    with client.websocket_connect(_url(community, server)) as ws:
        text = ws.receive_text()
    ts = json.loads(text)["ts"]
    assert (
        text == f'{{"stream":"snapshot","ts":"{ts}","payload":{{"state":"running"}}}}'
    )


def test_no_snapshot_without_the_status_stream() -> None:
    # A log-only subscriber never asked for status, so it gets none: its first
    # frame is the first published log line.
    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus)
    client = _client(app)
    with client.websocket_connect(_url(community, server, streams="log")) as ws:
        _await_subscribers(bus, str(server), 1)
        bus.publish(
            server_id=str(server),
            event=RealTimeEvent(stream=EventStream.LOG, payload={"line": "hi"}),
        )
        frame = ws.receive_json()
    assert frame["stream"] == "log"


def test_transition_racing_the_snapshot_read_is_delivered_after_it() -> None:
    """The subscription is registered before the snapshot is read.

    A transition published while the snapshot read is in flight therefore lands
    in the subscription's buffer and is delivered after the snapshot -- never
    lost between the read and the subscribe.
    """

    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()

    class _RacingReadServer(_FakeReadServer):
        reads = 0

        async def __call__(self, **kwargs: object) -> object:
            self.reads += 1
            if self.reads == 2:  # the snapshot read (the first is the authz gate)
                bus.publish(
                    server_id=str(server),
                    event=RealTimeEvent(
                        stream=EventStream.STATUS, payload={"state": "stopping"}
                    ),
                )
            return await super().__call__(**kwargs)

    app = _app(bus=bus, read_server=_RacingReadServer(found=True))
    client = _client(app)
    with client.websocket_connect(_url(community, server)) as ws:
        first = ws.receive_json()
        second = ws.receive_json()
    assert first["stream"] == "snapshot"
    assert second["stream"] == "status"
    assert second["payload"] == {"state": "stopping"}


def test_server_deleted_before_the_snapshot_read_closes_4404() -> None:
    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()

    class _DeletedAfterAuthz(_FakeReadServer):
        reads = 0

        async def __call__(self, **kwargs: object) -> object:
            self.reads += 1
            if self.reads > 1:
                raise ServerNotFoundError("x")
            return await super().__call__(**kwargs)

    app = _app(bus=bus, read_server=_DeletedAfterAuthz(found=True))
    client = _client(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(_url(community, server)) as ws:
            ws.receive_json()
    assert exc.value.code == 4404
    _await_subscribers(bus, str(server), 0)


# --- frame ts carries the worker's emitted_at -----------------------------


def test_frame_ts_uses_worker_emitted_at() -> None:
    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus)
    client = _client(app)
    emitted = dt.datetime(2026, 6, 3, 12, 0, 0, tzinfo=dt.timezone.utc)
    with client.websocket_connect(_url(community, server)) as ws:
        _skip_snapshot(ws)
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
    client = _client(app)
    before = dt.datetime.now(dt.timezone.utc)
    with client.websocket_connect(_url(community, server)) as ws:
        _skip_snapshot(ws)
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
    client = _client(app)
    emitted = dt.datetime(2026, 6, 3, 12, 0, 0, tzinfo=dt.timezone.utc)
    with client.websocket_connect(_url(community, server)) as ws:
        _skip_snapshot(ws)
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
    client = _client(app)
    with (
        client.websocket_connect(_url(community, server)) as ws1,
        client.websocket_connect(_url(community, server)) as ws2,
        client.websocket_connect(_url(community, server)) as ws3,
    ):
        # All three subscriptions must be registered before the publish; each
        # one's snapshot is sent only after it is.
        for ws in (ws1, ws2, ws3):
            _skip_snapshot(ws)
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
    client = _client(app)
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
    client = _client(app)
    url = f"/api/communities/{community}/servers/{server}/events"
    cases: list[tuple[EventStream, dict[str, object]]] = [
        (EventStream.STATUS, {"state": "running"}),
        (EventStream.LOG, {"line": "hi"}),
        (EventStream.METRICS, {"cpu_millis": 1}),
        (EventStream.NOTIFICATION, {"kind": "k", "title": "t", "detail": ""}),
    ]
    with client.websocket_connect(url) as ws:
        _skip_snapshot(ws)
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
    client = _client(app)

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(_url(community, server)) as ws:
            _skip_snapshot(ws)
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


def test_mid_stream_revocation_closes_despite_busy_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A revoked member on a busy stream is disconnected within the re-authz interval.

    Pre-fix: the fresh timeout resets on every delivered frame, so reauthorize()
    never runs and the revoked member keeps receiving indefinitely.
    Post-fix: the wall-clock deadline elapses regardless of frame traffic and the
    gate re-runs, closing the socket with 4403.
    """

    from mc_server_dashboard_api.fleet.api import events as events_module

    monkeypatch.setattr(events_module, "_REAUTHZ_INTERVAL_SECONDS", 0.5)

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
    client = _client(app)

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(_url(community, server)) as ws:
            _skip_snapshot(ws)
            # Revoke immediately — the stream will stay busy the whole time.
            checker.revoke()
            # Pump frames faster than the re-authz interval so the old code's
            # timeout would reset on every iteration. The client reads each frame
            # so the server loop progresses without send-buffer back-pressure.
            for i in range(30):
                bus.publish(
                    server_id=str(server),
                    event=RealTimeEvent(
                        stream=EventStream.STATUS, payload={"state": str(i)}
                    ),
                )
                time.sleep(0.05)
                ws.receive_json()
            # If we get here the socket was never closed — fail explicitly.
            pytest.fail("socket was not closed despite revocation")
    assert exc.value.code == 4403


def test_disconnect_cleans_up_subscription() -> None:
    bus = InProcessRealTimeEvents()
    community, server = uuid.uuid4(), uuid.uuid4()
    app = _app(bus=bus)
    client = _client(app)
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
    client = _client(app)
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
    client = _client(app)

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
    client = _client(app)
    with client.websocket_connect(_url(community, server)) as ws:
        _skip_snapshot(ws)
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
            snapshot=None,
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
