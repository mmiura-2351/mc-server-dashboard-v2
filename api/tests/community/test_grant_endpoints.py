"""Endpoint tests for the grant router (Section 6.4, issue #71).

The HTTP boundary is exercised in-process via FastAPI's TestClient with the use
cases and authorization Ports faked (NFR-TEST-1, no database). Verifies the
two-layer gate (non-member -> 404, member-without-permission -> 403, authorized
member -> 2xx) and the domain-error -> HTTP-code mapping (non-member target 404,
duplicate grant 409, unknown resource type 422, invalid permission 422,
cross-community / missing grant 404).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.community.domain.entities import ResourceGrant
from mc_server_dashboard_api.community.domain.errors import (
    GrantResourceNotFoundError,
    GrantTargetNotMemberError,
    InvalidGrantResourceTypeError,
    ResourceGrantAlreadyExistsError,
    ResourceGrantNotFoundError,
    UnknownPermissionError,
)
from mc_server_dashboard_api.community.domain.permission_checker import (
    MembershipVisibility,
    PermissionChecker,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    AuthUser,
    CommunityId,
    Permission,
    ResourceGrantId,
    ResourceRef,
    UserId,
)
from mc_server_dashboard_api.dependencies import (
    get_create_grant,
    get_current_user,
    get_list_grants,
    get_membership_visibility,
    get_permission_checker,
    get_revoke_grant,
)
from tests.identity.fakes import make_user

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


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


def _client(app: object) -> Iterator[TestClient]:
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client


def _app(
    *,
    member: bool,
    allow: bool,
    list_uc: _FakeUseCase | None = None,
    create_uc: _FakeUseCase | None = None,
    revoke_uc: _FakeUseCase | None = None,
) -> object:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: make_user()
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(allow=allow)
    if list_uc is not None:
        app.dependency_overrides[get_list_grants] = lambda: list_uc
    if create_uc is not None:
        app.dependency_overrides[get_create_grant] = lambda: create_uc
    if revoke_uc is not None:
        app.dependency_overrides[get_revoke_grant] = lambda: revoke_uc
    return app


def _grant(community: CommunityId, user: UserId) -> ResourceGrant:
    return ResourceGrant(
        id=ResourceGrantId.new(),
        user_id=user,
        community_id=community,
        resource_type="server",
        resource_id=uuid.uuid4(),
        permissions={Permission("server:start")},
        created_at=_NOW,
        updated_at=_NOW,
    )


def _create_body(user: UserId) -> dict[str, object]:
    return {
        "user_id": str(user.value),
        "resource_type": "server",
        "resource_id": str(uuid.uuid4()),
        "permissions": ["server:start"],
    }


# --- list -------------------------------------------------------------------


def test_list_grants_authorized_returns_200() -> None:
    community = CommunityId.new()
    user = UserId(uuid.uuid4())
    app = _app(
        member=True, allow=True, list_uc=_FakeUseCase(result=[_grant(community, user)])
    )
    client = next(_client(app))
    resp = client.get(f"/communities/{community.value}/grants")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_list_grants_passes_user_filter() -> None:
    community = CommunityId.new()
    user = UserId(uuid.uuid4())
    use_case = _FakeUseCase(result=[])
    app = _app(member=True, allow=True, list_uc=use_case)
    client = next(_client(app))
    resp = client.get(
        f"/communities/{community.value}/grants", params={"user_id": str(user.value)}
    )
    assert resp.status_code == 200
    assert use_case.calls[0]["user_id"] == user


def test_list_grants_non_member_gets_404() -> None:
    app = _app(member=False, allow=True, list_uc=_FakeUseCase(result=[]))
    client = next(_client(app))
    resp = client.get(f"/communities/{uuid.uuid4()}/grants")
    assert resp.status_code == 404


def test_list_grants_without_permission_gets_403() -> None:
    app = _app(member=True, allow=False, list_uc=_FakeUseCase(result=[]))
    client = next(_client(app))
    resp = client.get(f"/communities/{uuid.uuid4()}/grants")
    assert resp.status_code == 403


# --- create -----------------------------------------------------------------


def test_create_grant_authorized_returns_201() -> None:
    community = CommunityId.new()
    user = UserId(uuid.uuid4())
    app = _app(
        member=True, allow=True, create_uc=_FakeUseCase(result=_grant(community, user))
    )
    client = next(_client(app))
    resp = client.post(
        f"/communities/{community.value}/grants", json=_create_body(user)
    )
    assert resp.status_code == 201
    assert resp.json()["resource_type"] == "server"


def test_create_grant_without_permission_gets_403() -> None:
    app = _app(member=True, allow=False, create_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(
        f"/communities/{uuid.uuid4()}/grants", json=_create_body(UserId(uuid.uuid4()))
    )
    assert resp.status_code == 403


def test_create_grant_non_member_target_gets_404() -> None:
    app = _app(
        member=True,
        allow=True,
        create_uc=_FakeUseCase(error=GrantTargetNotMemberError("x")),
    )
    client = next(_client(app))
    resp = client.post(
        f"/communities/{uuid.uuid4()}/grants", json=_create_body(UserId(uuid.uuid4()))
    )
    assert resp.status_code == 404


def test_create_grant_nonexistent_resource_gets_404() -> None:
    app = _app(
        member=True,
        allow=True,
        create_uc=_FakeUseCase(error=GrantResourceNotFoundError("x")),
    )
    client = next(_client(app))
    resp = client.post(
        f"/communities/{uuid.uuid4()}/grants", json=_create_body(UserId(uuid.uuid4()))
    )
    assert resp.status_code == 404


def test_create_grant_unknown_resource_type_returns_422() -> None:
    app = _app(
        member=True,
        allow=True,
        create_uc=_FakeUseCase(error=InvalidGrantResourceTypeError("x")),
    )
    client = next(_client(app))
    resp = client.post(
        f"/communities/{uuid.uuid4()}/grants", json=_create_body(UserId(uuid.uuid4()))
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "invalid_resource_type"


def test_create_grant_invalid_permission_returns_422() -> None:
    app = _app(
        member=True,
        allow=True,
        create_uc=_FakeUseCase(error=UnknownPermissionError("x")),
    )
    client = next(_client(app))
    resp = client.post(
        f"/communities/{uuid.uuid4()}/grants", json=_create_body(UserId(uuid.uuid4()))
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "invalid_permission"


def test_create_grant_duplicate_returns_409() -> None:
    app = _app(
        member=True,
        allow=True,
        create_uc=_FakeUseCase(error=ResourceGrantAlreadyExistsError("x")),
    )
    client = next(_client(app))
    resp = client.post(
        f"/communities/{uuid.uuid4()}/grants", json=_create_body(UserId(uuid.uuid4()))
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "grant_exists"


# --- revoke -----------------------------------------------------------------


def test_revoke_grant_authorized_returns_204() -> None:
    app = _app(member=True, allow=True, revoke_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.delete(f"/communities/{uuid.uuid4()}/grants/{uuid.uuid4()}")
    assert resp.status_code == 204


def test_revoke_grant_cross_community_gets_404() -> None:
    app = _app(
        member=True,
        allow=True,
        revoke_uc=_FakeUseCase(error=ResourceGrantNotFoundError("x")),
    )
    client = next(_client(app))
    resp = client.delete(f"/communities/{uuid.uuid4()}/grants/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_revoke_grant_without_permission_gets_403() -> None:
    app = _app(member=True, allow=False, revoke_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.delete(f"/communities/{uuid.uuid4()}/grants/{uuid.uuid4()}")
    assert resp.status_code == 403
