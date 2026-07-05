"""Unit tests for WebSocket access-token extraction via subprotocol (#1596).

The ``_ws_access_token`` helper resolves the Bearer token from either the
``Authorization`` header (non-browser clients / test client) or the
``Sec-WebSocket-Protocol`` subprotocol pair ``["access_token", "<jwt>"]``
(browser path).  The query-parameter ``?token=`` path has been removed.

``ws_accept_subprotocol`` returns the ``access_token`` marker when the client
offered it, so the server can echo it in the accept (RFC 6455).
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, WebSocket
from starlette.testclient import TestClient

from mc_server_dashboard_api.dependencies import (
    _ws_access_token,
    ws_accept_subprotocol,
)

# ---------------------------------------------------------------------------
# Minimal app with a single echo route — no create_app(), no DB, no lifespan.
# ---------------------------------------------------------------------------


def _echo_app() -> FastAPI:
    app = FastAPI()

    @app.websocket("/echo")
    async def _echo(websocket: WebSocket) -> None:
        token = _ws_access_token(websocket)
        subprotocol = ws_accept_subprotocol(websocket)
        await websocket.accept(subprotocol=subprotocol)
        await websocket.send_json({"token": token, "accepted_subprotocol": subprotocol})
        await websocket.close()

    return app


# ---------------------------------------------------------------------------
# _ws_access_token
# ---------------------------------------------------------------------------


def test_extracts_token_from_authorization_header() -> None:
    with TestClient(_echo_app()) as client:
        with client.websocket_connect(
            "/echo", headers={"Authorization": "Bearer my-jwt"}
        ) as ws:
            data = ws.receive_json()
    assert data["token"] == "my-jwt"


def test_extracts_token_from_subprotocol_header() -> None:
    with TestClient(_echo_app()) as client:
        with client.websocket_connect(
            "/echo", subprotocols=["access_token", "the-jwt"]
        ) as ws:
            data = ws.receive_json()
    assert data["token"] == "the-jwt"
    assert data["accepted_subprotocol"] == "access_token"


def test_returns_none_when_no_token_provided() -> None:
    with TestClient(_echo_app()) as client:
        with client.websocket_connect("/echo") as ws:
            data = ws.receive_json()
    assert data["token"] is None
    assert data["accepted_subprotocol"] is None


def test_query_param_token_is_not_accepted() -> None:
    """The old ``?token=`` query-parameter path must no longer work."""
    with TestClient(_echo_app()) as client:
        with client.websocket_connect("/echo?token=leaked") as ws:
            data = ws.receive_json()
    assert data["token"] is None


def test_subprotocol_marker_without_following_token_returns_none() -> None:
    """``access_token`` offered alone (no JWT after it) yields ``None``."""
    with TestClient(_echo_app()) as client:
        with client.websocket_connect("/echo", subprotocols=["access_token"]) as ws:
            data = ws.receive_json()
    assert data["token"] is None
    assert data["accepted_subprotocol"] == "access_token"


def test_authorization_header_takes_precedence_over_subprotocol() -> None:
    with TestClient(_echo_app()) as client:
        with client.websocket_connect(
            "/echo",
            headers={"Authorization": "Bearer header-jwt"},
            subprotocols=["access_token", "subproto-jwt"],
        ) as ws:
            data = ws.receive_json()
    assert data["token"] == "header-jwt"


# ---------------------------------------------------------------------------
# Endpoint-level: accepted_subprotocol echoed on the real events routes
# ---------------------------------------------------------------------------


def _events_app() -> FastAPI:
    """Full app with auth/authz overridden so events endpoints are reachable."""
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
        get_server_community_lookup,
    )
    from mc_server_dashboard_api.fleet.adapters.real_time_events import (
        InProcessRealTimeEvents,
    )
    from tests.identity.fakes import make_user

    app = create_app()
    user = make_user()

    class _AllowAll(PermissionChecker):
        async def can(
            self, *, user: AuthUser, operation: Permission, resource: ResourceRef
        ) -> bool:
            return True

    class _IsMember(MembershipVisibility):
        async def is_member(
            self, *, user_id: UserId, community_id: CommunityId
        ) -> bool:
            return True

    class _Found:
        async def __call__(self, **_kw: object) -> object:
            return object()

    app.dependency_overrides[get_current_user_ws] = lambda: user
    app.dependency_overrides[get_membership_visibility] = _IsMember
    app.dependency_overrides[get_permission_checker] = _AllowAll
    app.dependency_overrides[get_read_server] = _Found
    app.dependency_overrides[get_real_time_events] = InProcessRealTimeEvents
    app.dependency_overrides[get_server_community_lookup] = lambda: lambda **_kw: None
    return app


def test_server_events_echoes_accepted_subprotocol() -> None:
    cid, sid = uuid.uuid4(), uuid.uuid4()
    url = f"/api/communities/{cid}/servers/{sid}/events?streams=status"
    with TestClient(_events_app()) as client:
        with client.websocket_connect(url, subprotocols=["access_token", "jwt"]) as ws:
            assert ws.accepted_subprotocol == "access_token"


def test_community_events_echoes_accepted_subprotocol() -> None:
    cid = uuid.uuid4()
    url = f"/api/communities/{cid}/events"
    with TestClient(_events_app()) as client:
        with client.websocket_connect(url, subprotocols=["access_token", "jwt"]) as ws:
            assert ws.accepted_subprotocol == "access_token"


def test_server_events_accepts_none_subprotocol_without_subprotocols() -> None:
    cid, sid = uuid.uuid4(), uuid.uuid4()
    url = f"/api/communities/{cid}/servers/{sid}/events?streams=status"
    with TestClient(_events_app()) as client:
        with client.websocket_connect(url) as ws:
            assert ws.accepted_subprotocol is None
