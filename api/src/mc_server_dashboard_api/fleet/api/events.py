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

- ``4401`` — unauthenticated (missing / invalid / expired token);
- ``4403`` — authenticated member without ``server:read`` on the resource;
- ``4404`` — not a member, or the server does not exist in this community.

Delivery is best-effort and decoupled from REST (FR-MON-4): if no event ever
arrives, the socket simply stays quiet; a slow client that overflows its buffer
gets a ``gap`` frame and keeps the newest events. Nothing else depends on a
subscriber, so a closed socket never affects the control plane or REST.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

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
    get_current_user_ws,
    get_membership_visibility,
    get_permission_checker,
    get_read_server,
    get_real_time_events,
)
from mc_server_dashboard_api.fleet.domain.real_time_events import (
    EventStream,
    RealTimeEvent,
    RealTimeEvents,
)
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

_CLOSE_UNAUTHENTICATED = 4401
_CLOSE_FORBIDDEN = 4403
_CLOSE_NOT_FOUND = 4404

# The streams a client may subscribe to (the gap marker is always delivered and
# is never selectable). An unknown ?streams= token is ignored.
_SUBSCRIBABLE: dict[str, EventStream] = {
    EventStream.STATUS.value: EventStream.STATUS,
    EventStream.LOG.value: EventStream.LOG,
    EventStream.METRICS.value: EventStream.METRICS,
}


def _parse_streams(raw: str | None) -> frozenset[EventStream]:
    """Map a comma-separated ``streams`` query to the subscribable set.

    Defaults to all three streams when omitted/blank; unknown tokens are dropped.
    """

    if not raw:
        return frozenset(_SUBSCRIBABLE.values())
    selected = {
        _SUBSCRIBABLE[token]
        for token in (t.strip() for t in raw.split(","))
        if token in _SUBSCRIBABLE
    }
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

    # Layer-1: a non-member gets no existence signal (same posture as REST 404).
    if not await visibility.is_member(
        user_id=auth_user.user_id, community_id=community
    ):
        await websocket.close(code=_CLOSE_NOT_FOUND)
        return

    # Layer-2: per-resource server:read (a grant on this server opens it).
    resource = ResourceRef(
        community_id=community,
        resource_type=_SERVER_RESOURCE_TYPE,
        resource_id=server_id,
    )
    if not await checker.can(user=auth_user, operation=_SERVER_READ, resource=resource):
        await websocket.close(code=_CLOSE_FORBIDDEN)
        return

    # A server outside this community is reported as not-found, indistinguishable
    # from a wholly unknown one (no cross-community existence signal).
    try:
        await read_server(
            community_id=ServersCommunityId(community_id),
            server_id=ServerId(server_id),
        )
    except ServerNotFoundError:
        await websocket.close(code=_CLOSE_NOT_FOUND)
        return

    streams = _parse_streams(websocket.query_params.get("streams"))
    await websocket.accept()
    # The bus is keyed by the worker-reported server id string (the UUID's text
    # form, as it arrives on the control-plane stream).
    subscription = bus.subscribe(server_id=str(server_id), streams=streams)
    try:
        async for event in subscription:
            await websocket.send_json(_frame(event))
    except WebSocketDisconnect:
        # Client went away; fall through to clean up the subscription.
        pass
    finally:
        await subscription.aclose()


def _frame(event: RealTimeEvent) -> dict[str, object]:
    """Render an event as the wire frame ``{stream, ts, payload}`` (Section 6.13)."""

    return {
        "stream": event.stream.value,
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "payload": event.payload,
    }
