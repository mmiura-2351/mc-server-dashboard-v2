"""WebSocket relay of a server's real-time events (Section 6.13, FR-MON-1..4).

``GET /communities/{community_id}/servers/{server_id}/events?streams=...`` upgrades
to a WebSocket that streams the server's status / log / metrics events as typed
JSON frames. Authorization is the same two-layer gate as the REST server routes
(``server:read``, per-resource), enforced *before* the upgrade is accepted so the
Layer-1 no-existence-signal posture holds during the handshake (Section 6.4): a
non-member, an unknown server, and a server in another community are all rejected
with the same close code, indistinguishable from one another.

Close codes (application range, RFC 6455 4000-4999) mirror the REST status the
same condition would produce:

- ``4400`` — a malformed request: ``?streams=`` is present but names an unknown
  stream (the REST 422-equivalent). An *omitted/blank* ``streams`` still means
  "all three streams"; only a present-but-invalid token is rejected, so a typo
  fails loudly instead of silently subscribing to everything;
- ``4401`` — unauthenticated (missing / invalid / expired token);
- ``4403`` — authenticated member without ``server:read`` on the resource;
- ``4404`` — not a member, or the server does not exist in this community.

Authorization is re-checked mid-stream: the two-layer gate is re-run every
:data:`_REAUTHZ_INTERVAL_SECONDS` while the socket is idle, so a member removed
or a grant revoked after accept stops receiving on the next interval instead of
keeping events until disconnect. The re-check is two indexed queries and runs in
the receive loop's idle path, so it never blocks frame delivery; on failure the
socket closes with the same code the accept-time gate would have used.

Delivery is best-effort and decoupled from REST (FR-MON-4): if no event ever
arrives, the socket simply stays quiet; a slow client that overflows its buffer
gets a ``gap`` frame and keeps the newest events. Nothing else depends on a
subscriber, so a closed socket never affects the control plane or REST.

The endpoints are send-only, but the socket is still read: a companion reader
task drains (and discards) anything the client sends, because uvicorn surfaces
a client disconnect only through ``receive()`` or a failing send — without the
reader, a client gone from a quiet topic would park its handler forever and
leak the subscription and its re-authz queries (#1695).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, WebSocket
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
)
from mc_server_dashboard_api.community.domain.value_objects import (
    UserId as CommunityUserId,
)
from mc_server_dashboard_api.dependencies import (
    ServerCommunityLookup,
    get_current_user_ws,
    get_membership_visibility,
    get_permission_checker,
    get_read_server,
    get_real_time_events,
    get_server_community_lookup,
    ws_accept_subprotocol,
)
from mc_server_dashboard_api.fleet.domain.real_time_events import (
    EventStream,
    EventSubscription,
    RealTimeEvent,
    RealTimeEvents,
)
from mc_server_dashboard_api.http_datetime import serialize_utc
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.servers.application.manage_server import ReadServer
from mc_server_dashboard_api.servers.domain.errors import ServerNotFoundError
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId as ServersCommunityId,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId

router = APIRouter()

_SERVER_RESOURCE_TYPE = "server"
_SERVER_READ = Permission("server:read")

_CLOSE_BAD_REQUEST = 4400
_CLOSE_UNAUTHENTICATED = 4401
_CLOSE_FORBIDDEN = 4403
_CLOSE_NOT_FOUND = 4404

# How often the two-layer authorization gate is re-run while the socket is idle.
# A constant, not a config knob: the check is two indexed queries, and a minute
# is a tight-enough bound on how long a removed member can keep receiving without
# adding query load. Re-checking only when idle keeps it off the delivery path.
_REAUTHZ_INTERVAL_SECONDS = 60.0

# The streams a client may subscribe to (the gap marker is always delivered and
# is never selectable).
_SUBSCRIBABLE: dict[str, EventStream] = {
    EventStream.STATUS.value: EventStream.STATUS,
    EventStream.LOG.value: EventStream.LOG,
    EventStream.METRICS.value: EventStream.METRICS,
}


class _UnknownStreamError(ValueError):
    """Raised when ``?streams=`` is present but names an unknown stream."""


def _parse_streams(raw: str | None) -> frozenset[EventStream]:
    """Map a comma-separated ``streams`` query to the subscribable set.

    Omitted/blank means all three streams. A present token that names no known
    stream raises :class:`_UnknownStreamError` so a typo is rejected rather than
    silently widening the subscription to everything.
    """

    if not raw:
        return frozenset(_SUBSCRIBABLE.values())
    selected = set()
    for token in (t.strip() for t in raw.split(",")):
        if not token:
            continue
        if token not in _SUBSCRIBABLE:
            raise _UnknownStreamError(token)
        selected.add(_SUBSCRIBABLE[token])
    return frozenset(selected) or frozenset(_SUBSCRIBABLE.values())


@router.websocket("/communities/{community_id}/servers/{server_id}/events")
async def server_events(
    websocket: WebSocket,
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    user: Annotated[User | None, Depends(get_current_user_ws)],
    visibility: Annotated[MembershipVisibility, Depends(get_membership_visibility)],
    checker: Annotated[PermissionChecker, Depends(get_permission_checker)],
    read_server: Annotated[ReadServer, Depends(get_read_server)],
    bus: Annotated[RealTimeEvents, Depends(get_real_time_events)],
) -> None:
    if user is None:
        await websocket.close(code=_CLOSE_UNAUTHENTICATED)
        return

    community = CommunityId(community_id)
    auth_user = AuthUser(
        user_id=CommunityUserId(user.id.value),
        is_platform_admin=user.is_platform_admin,
    )

    # The two-layer gate, applied before accept; re-applied mid-stream by the
    # relay loop every idle re-authz interval.
    recheck = functools.partial(
        _authorize,
        auth_user=auth_user,
        community=community,
        community_id=community_id,
        server_id=server_id,
        visibility=visibility,
        checker=checker,
        read_server=read_server,
    )
    denied = await recheck()
    if denied is not None:
        await websocket.close(code=denied)
        return

    # A malformed ?streams= is rejected before accept (the REST 422-equivalent),
    # matching the accept-time rejection style. Omitted/blank still means "all".
    try:
        streams = _parse_streams(websocket.query_params.get("streams"))
    except _UnknownStreamError:
        await websocket.close(code=_CLOSE_BAD_REQUEST)
        return

    await websocket.accept(subprotocol=ws_accept_subprotocol(websocket))
    # The bus is keyed by the worker-reported server id string (the UUID's text
    # form, as it arrives on the control-plane stream).
    subscription = bus.subscribe(server_id=str(server_id), streams=streams)

    async def _deliver(event: RealTimeEvent) -> None:
        await websocket.send_text(_encoded(event, _FRAME_SLOT, _frame))

    try:
        await _relay(websocket, subscription, reauthorize=recheck, deliver=_deliver)
    finally:
        await subscription.aclose()


@router.websocket("/communities/{community_id}/events")
async def community_events(
    websocket: WebSocket,
    community_id: uuid.UUID,
    user: Annotated[User | None, Depends(get_current_user_ws)],
    visibility: Annotated[MembershipVisibility, Depends(get_membership_visibility)],
    checker: Annotated[PermissionChecker, Depends(get_permission_checker)],
    lookup: Annotated[ServerCommunityLookup, Depends(get_server_community_lookup)],
    bus: Annotated[RealTimeEvents, Depends(get_real_time_events)],
) -> None:
    """Stream status-change events for every server of one community (#288).

    The community-level analogue of :func:`server_events`: the two-layer gate is
    applied before accept (and re-applied mid-stream), but at community scope —
    ``server:read`` with no specific resource, the honest gate for a stream that
    carries the lifecycle of all the community's servers. Only the STATUS stream
    is forwarded; log/metrics are per-server detail, not operator notifications.

    Fan-out is a firehose subscription over the relay filtered by community
    membership of each event's server. The server->community mapping is resolved
    lazily and cached for the life of the connection, so a hot server is one
    bounded lookup; servers created after connect are picked up because the
    firehose carries every server's events.

    Worker online/offline/draining transitions are *not* included: worker state
    changes never flow through this relay today (the registry mutates in place and
    publishes nothing), so there is no honest event source to fan out — only
    server-status fan-out ships here.
    """

    if user is None:
        await websocket.close(code=_CLOSE_UNAUTHENTICATED)
        return

    community = CommunityId(community_id)
    auth_user = AuthUser(
        user_id=CommunityUserId(user.id.value),
        is_platform_admin=user.is_platform_admin,
    )

    recheck = functools.partial(
        _authorize_community,
        auth_user=auth_user,
        community=community,
        visibility=visibility,
        checker=checker,
    )
    denied = await recheck()
    if denied is not None:
        await websocket.close(code=denied)
        return

    await websocket.accept(subprotocol=ws_accept_subprotocol(websocket))
    subscription = bus.subscribe_all(streams=frozenset({EventStream.STATUS}))
    # Per-connection server->community cache: each server id is looked up at most
    # once, bounding queries while the firehose may carry many servers' events.
    membership: dict[str, bool] = {}

    async def _deliver(event: RealTimeEvent) -> None:
        # The GAP marker has no server; it is forwarded as-is so the client
        # still learns it fell behind (best-effort delivery, FR-MON-4).
        if event.stream is not EventStream.GAP and not await _in_community(
            event=event,
            community_id=community_id,
            lookup=lookup,
            membership=membership,
        ):
            return
        await websocket.send_text(
            _encoded(event, _COMMUNITY_FRAME_SLOT, _community_frame)
        )

    try:
        await _relay(websocket, subscription, reauthorize=recheck, deliver=_deliver)
    finally:
        await subscription.aclose()


async def _client_gone(websocket: WebSocket) -> None:
    """Return when the client disconnects.

    The endpoints are send-only, so anything the client does send is read and
    discarded; draining is what lets the server observe the eventual
    ``websocket.disconnect`` message (#1695).
    """

    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


async def _relay(
    websocket: WebSocket,
    subscription: EventSubscription,
    *,
    reauthorize: Callable[[], Awaitable[int | None]],
    deliver: Callable[[RealTimeEvent], Awaitable[None]],
) -> None:
    """Deliver subscription events until the client goes away or authz is revoked.

    Each turn of the loop races three outcomes: the next buffered event (handed
    to ``deliver``), the re-authz interval elapsing idle (``reauthorize`` re-runs
    the accept-time gate without touching delivery; a denial closes the socket
    with its code), and the client disconnecting. Disconnect is observed by a
    companion reader task (:func:`_client_gone`) because a client gone from a
    quiet topic never wakes the delivery wait (#1695). The pending-event task is
    kept across idle re-checks, so no event is dropped; every exit path —
    disconnect, subscription end, revocation, an exception, cancellation on
    server shutdown — discards both helper tasks. The caller owns
    ``subscription.aclose()``.
    """

    disconnected = asyncio.create_task(_client_gone(websocket))
    next_event = asyncio.ensure_future(subscription.__anext__())
    try:
        while True:
            done, _pending = await asyncio.wait(
                {next_event, disconnected},
                timeout=_REAUTHZ_INTERVAL_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnected in done:
                return
            if next_event in done:
                try:
                    event = next_event.result()
                except StopAsyncIteration:
                    return
                next_event = asyncio.ensure_future(subscription.__anext__())
                await deliver(event)
            else:
                # Idle window elapsed: re-run the gate. A revoked subscriber is
                # closed with the accept-time code.
                denied = await reauthorize()
                if denied is not None:
                    await websocket.close(code=denied)
                    return
    except WebSocketDisconnect:
        # Client went away mid-send; the caller cleans up the subscription.
        pass
    finally:
        # Cancel without awaiting: the loop collects a cancelled task on its
        # next tick, while a suspension point here would let a concurrent
        # cancellation (server shutdown, or the TestClient's scope teardown)
        # resurface through the bare-cancelled helpers stripped of its original
        # cause — an anyio cancel scope would then refuse to absorb it.
        _discard(disconnected)
        _discard(next_event)


def _discard(task: asyncio.Future[Any]) -> None:
    """Abandon a helper ``task``: cancel it, or consume an already-final outcome.

    Retrieving the result/exception of a task that finished just as the loop
    exited (e.g. a ``StopAsyncIteration`` completing alongside the disconnect)
    keeps the event loop from logging it as never retrieved.
    """

    if task.done():
        if not task.cancelled():
            task.exception()
    else:
        task.cancel()


async def _in_community(
    *,
    event: RealTimeEvent,
    community_id: uuid.UUID,
    lookup: ServerCommunityLookup,
    membership: dict[str, bool],
) -> bool:
    """Return whether ``event``'s server belongs to ``community_id`` (cached)."""

    server_id = event.server_id
    if server_id is None:
        return False
    cached = membership.get(server_id)
    if cached is None:
        owner = await lookup(server_id=server_id)
        cached = owner == community_id
        membership[server_id] = cached
    return cached


async def _authorize_community(
    *,
    auth_user: AuthUser,
    community: CommunityId,
    visibility: MembershipVisibility,
    checker: PermissionChecker,
) -> int | None:
    """Run the two-layer gate at community scope; return a close code or ``None``.

    Layer-1 non-membership collapses to ``4404`` (no existence signal); a member
    lacking community-level ``server:read`` is ``4403``. There is no per-resource
    server here, so the permission is checked against the community alone.
    """

    if not await visibility.is_member(
        user_id=auth_user.user_id, community_id=community
    ):
        return _CLOSE_NOT_FOUND
    resource = ResourceRef(community_id=community)
    if not await checker.can(user=auth_user, operation=_SERVER_READ, resource=resource):
        return _CLOSE_FORBIDDEN
    return None


def _community_frame(event: RealTimeEvent) -> dict[str, object]:
    """Render a community-stream frame: the per-server frame plus ``server_id``.

    ``server_id`` lets the operator route the event to the right server; it is
    ``None`` on the GAP marker (which is server-agnostic).
    """

    frame = _frame(event)
    frame["server_id"] = event.server_id
    return frame


async def _authorize(
    *,
    auth_user: AuthUser,
    community: CommunityId,
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    visibility: MembershipVisibility,
    checker: PermissionChecker,
    read_server: ReadServer,
) -> int | None:
    """Run the two-layer gate; return a close code on denial, else ``None``.

    Layer-1 membership and the cross-community existence check both collapse to
    ``4404`` (no existence signal, Section 6.4); a member lacking ``server:read``
    is ``4403``. Used both before accept and on the mid-stream re-check.
    """

    # Layer-1: a non-member gets no existence signal (same posture as REST 404).
    if not await visibility.is_member(
        user_id=auth_user.user_id, community_id=community
    ):
        return _CLOSE_NOT_FOUND

    # Layer-2: per-resource server:read (a grant on this server opens it).
    resource = ResourceRef(
        community_id=community,
        resource_type=_SERVER_RESOURCE_TYPE,
        resource_id=server_id,
    )
    if not await checker.can(user=auth_user, operation=_SERVER_READ, resource=resource):
        return _CLOSE_FORBIDDEN

    # A server outside this community is reported as not-found, indistinguishable
    # from a wholly unknown one (no cross-community existence signal).
    try:
        await read_server(
            community_id=ServersCommunityId(community_id),
            server_id=ServerId(server_id),
        )
    except ServerNotFoundError:
        return _CLOSE_NOT_FOUND
    return None


def _frame(event: RealTimeEvent) -> dict[str, object]:
    """Render an event as the wire frame ``{stream, ts, payload}`` (Section 6.13).

    ``ts`` carries the Worker's authoritative ``emitted_at`` when the event has
    one, so a queued subscriber sees true event time; it falls back to the
    relay's send time when the Worker left ``emitted_at`` unset/zero (and for the
    adapter-synthesised gap marker, which has none). Frames are encoded once per
    event and shared (:func:`_encoded`), so the fallback is the first delivery's
    send time, identical for every subscriber.
    """

    ts = event.emitted_at or dt.datetime.now(dt.timezone.utc)
    return {
        "stream": event.stream.value,
        "ts": serialize_utc(ts),
        "payload": event.payload,
    }


# Cache slots for the encoded wire text, stashed on the event instance itself —
# one per frame shape, because the community frame carries an extra
# ``server_id`` and must never share an encoding with the per-server shape.
_FRAME_SLOT = "_wire_frame_text"
_COMMUNITY_FRAME_SLOT = "_wire_community_frame_text"


def _encoded(
    event: RealTimeEvent,
    slot: str,
    build: Callable[[RealTimeEvent], dict[str, object]],
) -> str:
    """Return ``event``'s frame text, encoded once and shared by all subscribers.

    The bus hands every subscriber of a topic the *same* event object (frozen,
    built fresh per publish, never mutated afterwards), so the first delivery
    encodes the frame and stashes the text in ``slot`` on the instance; the
    remaining deliveries reuse it — O(events) serializations instead of
    O(events x subscribers) (#1701). The stash goes through ``__dict__``
    because the dataclass is frozen; it is invisible to equality/repr and is
    not carried over by ``dataclasses.replace``.

    The GAP marker is exempt: the adapter reuses one module-level instance
    across subscriptions and time, and its frame's ``ts`` is the send time,
    which must stay fresh per occurrence (a cached gap would carry the first
    gap's timestamp forever). A gap is per-subscriber anyway, so no sharing is
    lost.

    ``json.dumps`` arguments mirror Starlette's ``send_json`` so the wire bytes
    are unchanged; ``send_text`` delivers the shared text.
    """

    if event.stream is EventStream.GAP:
        return json.dumps(build(event), separators=(",", ":"), ensure_ascii=False)
    text = event.__dict__.get(slot)
    if text is None:
        text = json.dumps(build(event), separators=(",", ":"), ensure_ascii=False)
        event.__dict__[slot] = text
    return text
