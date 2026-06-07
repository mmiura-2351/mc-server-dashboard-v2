"""Endpoint tests for the role router (Section 6.4, issue #71).

The HTTP boundary is exercised in-process via FastAPI's TestClient with the use
cases and authorization Ports faked (NFR-TEST-1, no database). Verifies the
two-layer gate (non-member -> 404, member-without-permission -> 403, authorized
member -> 2xx) and the domain-error -> HTTP-code mapping (preset role 409,
duplicate name 409, cross-community / missing role 404, invalid permission 422).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.community.domain.entities import Role
from mc_server_dashboard_api.community.domain.errors import (
    PresetRoleNotEditableError,
    RoleAlreadyExistsError,
    RoleNotFoundError,
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
    ResourceRef,
    RoleId,
    RoleName,
    UserId,
)
from mc_server_dashboard_api.dependencies import (
    get_create_role,
    get_current_user,
    get_delete_role,
    get_list_roles,
    get_membership_visibility,
    get_permission_checker,
    get_read_role,
    get_update_role,
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
    read_uc: _FakeUseCase | None = None,
    create_uc: _FakeUseCase | None = None,
    update_uc: _FakeUseCase | None = None,
    delete_uc: _FakeUseCase | None = None,
) -> object:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: make_user()
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(allow=allow)
    if list_uc is not None:
        app.dependency_overrides[get_list_roles] = lambda: list_uc
    if read_uc is not None:
        app.dependency_overrides[get_read_role] = lambda: read_uc
    if create_uc is not None:
        app.dependency_overrides[get_create_role] = lambda: create_uc
    if update_uc is not None:
        app.dependency_overrides[get_update_role] = lambda: update_uc
    if delete_uc is not None:
        app.dependency_overrides[get_delete_role] = lambda: delete_uc
    return app


def _role(community: CommunityId, *, is_preset: bool = False) -> Role:
    return Role(
        id=RoleId.new(),
        community_id=community,
        name=RoleName("Editor"),
        permissions={Permission("server:read")},
        created_at=_NOW,
        updated_at=_NOW,
        is_preset=is_preset,
    )


# --- list / read ------------------------------------------------------------


def test_list_roles_authorized_returns_200() -> None:
    community = CommunityId.new()
    app = _app(member=True, allow=True, list_uc=_FakeUseCase(result=[_role(community)]))
    client = next(_client(app))
    resp = client.get(f"/api/communities/{community.value}/roles")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_list_roles_non_member_gets_404() -> None:
    app = _app(member=False, allow=True, list_uc=_FakeUseCase(result=[]))
    client = next(_client(app))
    resp = client.get(f"/api/communities/{uuid.uuid4()}/roles")
    assert resp.status_code == 404


def test_list_roles_without_permission_gets_403() -> None:
    app = _app(member=True, allow=False, list_uc=_FakeUseCase(result=[]))
    client = next(_client(app))
    resp = client.get(f"/api/communities/{uuid.uuid4()}/roles")
    assert resp.status_code == 403


def test_read_role_cross_community_gets_404() -> None:
    app = _app(
        member=True, allow=True, read_uc=_FakeUseCase(error=RoleNotFoundError("x"))
    )
    client = next(_client(app))
    resp = client.get(f"/api/communities/{uuid.uuid4()}/roles/{uuid.uuid4()}")
    assert resp.status_code == 404


# --- create -----------------------------------------------------------------


def test_create_role_authorized_returns_201() -> None:
    community = CommunityId.new()
    app = _app(member=True, allow=True, create_uc=_FakeUseCase(result=_role(community)))
    client = next(_client(app))
    resp = client.post(
        f"/api/communities/{community.value}/roles",
        json={"name": "Editor", "permissions": ["server:read"]},
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "Editor"


def test_create_role_without_permission_gets_403() -> None:
    app = _app(member=True, allow=False, create_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/roles",
        json={"name": "Editor", "permissions": []},
    )
    assert resp.status_code == 403


def test_create_role_duplicate_name_returns_409() -> None:
    app = _app(
        member=True,
        allow=True,
        create_uc=_FakeUseCase(error=RoleAlreadyExistsError("x")),
    )
    client = next(_client(app))
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/roles",
        json={"name": "Editor", "permissions": []},
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "name_taken"


def test_create_role_unknown_permission_returns_422() -> None:
    app = _app(
        member=True,
        allow=True,
        create_uc=_FakeUseCase(error=UnknownPermissionError("x")),
    )
    client = next(_client(app))
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/roles",
        json={"name": "Editor", "permissions": ["server:read"]},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_permission"


def test_create_role_malformed_permission_returns_422() -> None:
    # Shape failure is caught at the edge before the use case runs.
    app = _app(member=True, allow=True, create_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(
        f"/api/communities/{uuid.uuid4()}/roles",
        json={"name": "Editor", "permissions": ["not-a-permission"]},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "invalid_permission"


# --- update -----------------------------------------------------------------


def test_update_role_preset_returns_409() -> None:
    app = _app(
        member=True,
        allow=True,
        update_uc=_FakeUseCase(error=PresetRoleNotEditableError("x")),
    )
    client = next(_client(app))
    resp = client.patch(
        f"/api/communities/{uuid.uuid4()}/roles/{uuid.uuid4()}",
        json={"name": "X"},
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "preset_role"


def test_update_role_cross_community_gets_404() -> None:
    app = _app(
        member=True, allow=True, update_uc=_FakeUseCase(error=RoleNotFoundError("x"))
    )
    client = next(_client(app))
    resp = client.patch(
        f"/api/communities/{uuid.uuid4()}/roles/{uuid.uuid4()}",
        json={"name": "X"},
    )
    assert resp.status_code == 404


# --- delete -----------------------------------------------------------------


def test_delete_role_authorized_returns_204() -> None:
    app = _app(member=True, allow=True, delete_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.delete(f"/api/communities/{uuid.uuid4()}/roles/{uuid.uuid4()}")
    assert resp.status_code == 204


def test_delete_role_preset_returns_409() -> None:
    app = _app(
        member=True,
        allow=True,
        delete_uc=_FakeUseCase(error=PresetRoleNotEditableError("x")),
    )
    client = next(_client(app))
    resp = client.delete(f"/api/communities/{uuid.uuid4()}/roles/{uuid.uuid4()}")
    assert resp.status_code == 409
    assert resp.json()["reason"] == "preset_role"


def test_delete_role_cross_community_gets_404() -> None:
    app = _app(
        member=True, allow=True, delete_uc=_FakeUseCase(error=RoleNotFoundError("x"))
    )
    client = next(_client(app))
    resp = client.delete(f"/api/communities/{uuid.uuid4()}/roles/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_delete_role_without_permission_gets_403() -> None:
    app = _app(member=True, allow=False, delete_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.delete(f"/api/communities/{uuid.uuid4()}/roles/{uuid.uuid4()}")
    assert resp.status_code == 403
