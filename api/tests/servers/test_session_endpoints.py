"""Endpoint tests for the server game-sessions route (RELAY.md Sections 8, 14).

The HTTP boundary is exercised in-process via FastAPI's TestClient with the use
case and authorization Ports faked (NFR-TEST-1, no database). Verifies:

- the two-layer gate (non-member -> 404, member-without ``session:read`` -> 403,
  authorized member -> 200);
- the response shape (claimed identity fields, IP, start/end);
- pagination params are forwarded to the use case (limit/offset);
- a missing/cross-community server -> 404.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

from fastapi.testclient import TestClient

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
    get_current_user,
    get_list_game_sessions,
    get_membership_visibility,
    get_permission_checker,
)
from mc_server_dashboard_api.servers.domain.errors import ServerNotFoundError
from mc_server_dashboard_api.servers.domain.game_session import GameSession
from mc_server_dashboard_api.servers.domain.value_objects import ServerId
from tests.identity.fakes import make_user

_NOW = dt.datetime(2026, 6, 12, 12, 0, tzinfo=dt.timezone.utc)


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


class _FakeUseCase:
    def __init__(self, *, result: object = None, error: Exception | None = None):
        self._result = result
        self._error = error
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._result


def _session(server_id: ServerId, *, ended: bool = True) -> GameSession:
    return GameSession(
        id=uuid.uuid4(),
        server_id=server_id,
        hostname="amber-falcon-42",
        player_ip="203.0.113.7",
        username="steve",
        player_uuid=uuid.UUID("66666666-6666-6666-6666-666666666666"),
        started_at=_NOW,
        ended_at=_NOW + dt.timedelta(minutes=5) if ended else None,
    )


def _client(app: object) -> Iterator[TestClient]:
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client


def _app(*, member: bool, allow: bool, list_: _FakeUseCase | None = None) -> object:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: make_user()
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(allow=allow)
    if list_ is not None:
        app.dependency_overrides[get_list_game_sessions] = lambda: list_
    return app


def _url(community: uuid.UUID, server: uuid.UUID, suffix: str = "") -> str:
    return f"/api/communities/{community}/servers/{server}/sessions{suffix}"


def test_non_member_gets_404() -> None:
    app = _app(member=False, allow=True, list_=_FakeUseCase(result=[]))
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()))
    assert resp.status_code == 404


def test_member_without_session_read_gets_403() -> None:
    app = _app(member=True, allow=False, list_=_FakeUseCase(result=[]))
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4()))
    assert resp.status_code == 403


def test_authorized_member_gets_session_list() -> None:
    server = ServerId(uuid.uuid4())
    use_case = _FakeUseCase(result=[_session(server)])
    app = _app(member=True, allow=True, list_=use_case)
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), server.value))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["sessions"]) == 1
    row = body["sessions"][0]
    assert row["hostname"] == "amber-falcon-42"
    assert row["player_ip"] == "203.0.113.7"
    # Claimed (pre-auth) Login Start identity.
    assert row["username"] == "steve"
    assert row["player_uuid"] == "66666666-6666-6666-6666-666666666666"
    assert row["ended_at"] is not None


def test_pagination_params_are_forwarded() -> None:
    server = ServerId(uuid.uuid4())
    use_case = _FakeUseCase(result=[])
    app = _app(member=True, allow=True, list_=use_case)
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), server.value, "?limit=10&offset=20"))
    assert resp.status_code == 200
    assert use_case.calls[0]["limit"] == 10
    assert use_case.calls[0]["offset"] == 20


def test_default_pagination_window() -> None:
    server = ServerId(uuid.uuid4())
    use_case = _FakeUseCase(result=[])
    app = _app(member=True, allow=True, list_=use_case)
    client = next(_client(app))
    client.get(_url(uuid.uuid4(), server.value))
    assert use_case.calls[0]["limit"] == 50
    assert use_case.calls[0]["offset"] == 0


def test_out_of_range_limit_is_rejected() -> None:
    server = ServerId(uuid.uuid4())
    app = _app(member=True, allow=True, list_=_FakeUseCase(result=[]))
    client = next(_client(app))
    assert client.get(_url(uuid.uuid4(), server.value, "?limit=0")).status_code == 422
    assert client.get(_url(uuid.uuid4(), server.value, "?limit=999")).status_code == 422


def test_missing_server_gives_404() -> None:
    server = ServerId(uuid.uuid4())
    use_case = _FakeUseCase(error=ServerNotFoundError(str(server.value)))
    app = _app(member=True, allow=True, list_=use_case)
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), server.value))
    assert resp.status_code == 404
