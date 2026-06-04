"""Endpoint tests for the membership routers (Section 6.3, 6.4).

The HTTP boundary is exercised in-process via FastAPI's TestClient with the use
cases and authorization Ports faked (NFR-TEST-1, no database). Verifies:

- every route applies the two-layer gate: non-member -> 404,
  member-without-permission -> 403, authorized member -> 2xx;
- domain errors map to the documented codes (unknown user 422, duplicate 409,
  last-Owner 409, missing member / cross-community role -> 404).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.community.application.manage_membership import MemberView
from mc_server_dashboard_api.community.domain.entities import Membership
from mc_server_dashboard_api.community.domain.errors import (
    LastOwnerRemovalError,
    MembershipAlreadyExistsError,
    MembershipNotFoundError,
    MemberUserNotFoundError,
    RoleNotFoundError,
)
from mc_server_dashboard_api.community.domain.permission_checker import (
    MembershipVisibility,
    PermissionChecker,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    AuthUser,
    CommunityId,
    MembershipId,
    Permission,
    ResourceRef,
    UserId,
)
from mc_server_dashboard_api.dependencies import (
    get_add_member,
    get_assign_role,
    get_current_user,
    get_list_members,
    get_membership_visibility,
    get_permission_checker,
    get_remove_member,
    get_unassign_role,
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
    add_uc: _FakeUseCase | None = None,
    remove_uc: _FakeUseCase | None = None,
    list_uc: _FakeUseCase | None = None,
    assign_uc: _FakeUseCase | None = None,
    unassign_uc: _FakeUseCase | None = None,
) -> object:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: make_user()
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(allow=allow)
    if add_uc is not None:
        app.dependency_overrides[get_add_member] = lambda: add_uc
    if remove_uc is not None:
        app.dependency_overrides[get_remove_member] = lambda: remove_uc
    if list_uc is not None:
        app.dependency_overrides[get_list_members] = lambda: list_uc
    if assign_uc is not None:
        app.dependency_overrides[get_assign_role] = lambda: assign_uc
    if unassign_uc is not None:
        app.dependency_overrides[get_unassign_role] = lambda: unassign_uc
    return app


def _membership(community_id: CommunityId, user_id: UserId) -> Membership:
    return Membership(
        id=MembershipId.new(),
        user_id=user_id,
        community_id=community_id,
        created_at=_NOW,
    )


# --- add member -------------------------------------------------------------


def test_add_member_authorized_returns_201() -> None:
    community = CommunityId.new()
    user = UserId(uuid.uuid4())
    use_case = _FakeUseCase(result=_membership(community, user))
    app = _app(member=True, allow=True, add_uc=use_case)
    client = next(_client(app))
    resp = client.post(
        f"/communities/{community.value}/members",
        json={"user_id": str(user.value)},
    )
    assert resp.status_code == 201
    assert resp.json()["user_id"] == str(user.value)


def test_add_member_non_member_gets_404() -> None:
    app = _app(member=False, allow=True, add_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(
        f"/communities/{uuid.uuid4()}/members", json={"user_id": str(uuid.uuid4())}
    )
    assert resp.status_code == 404


def test_add_member_without_permission_gets_403() -> None:
    app = _app(member=True, allow=False, add_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(
        f"/communities/{uuid.uuid4()}/members", json={"user_id": str(uuid.uuid4())}
    )
    assert resp.status_code == 403


def test_add_member_unknown_user_returns_422() -> None:
    app = _app(
        member=True, allow=True, add_uc=_FakeUseCase(error=MemberUserNotFoundError("x"))
    )
    client = next(_client(app))
    resp = client.post(
        f"/communities/{uuid.uuid4()}/members", json={"user_id": str(uuid.uuid4())}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "user_not_found"


def test_add_member_duplicate_returns_409() -> None:
    app = _app(
        member=True,
        allow=True,
        add_uc=_FakeUseCase(error=MembershipAlreadyExistsError("x")),
    )
    client = next(_client(app))
    resp = client.post(
        f"/communities/{uuid.uuid4()}/members", json={"user_id": str(uuid.uuid4())}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "already_member"


def test_add_member_invalid_user_id_returns_422() -> None:
    app = _app(member=True, allow=True, add_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(
        f"/communities/{uuid.uuid4()}/members", json={"user_id": "not-a-uuid"}
    )
    assert resp.status_code == 422


# --- list members -----------------------------------------------------------


def test_list_members_authorized_returns_200() -> None:
    community = CommunityId.new()
    user = UserId(uuid.uuid4())
    view = MemberView(
        user_id=user, membership_id=MembershipId.new(), role_names=["Owner"]
    )
    app = _app(member=True, allow=True, list_uc=_FakeUseCase(result=[view]))
    client = next(_client(app))
    resp = client.get(f"/communities/{community.value}/members")
    assert resp.status_code == 200
    assert resp.json()[0]["role_names"] == ["Owner"]
    assert resp.json()[0]["user_id"] == str(user.value)


def test_list_members_non_member_gets_404() -> None:
    app = _app(member=False, allow=True, list_uc=_FakeUseCase(result=[]))
    client = next(_client(app))
    resp = client.get(f"/communities/{uuid.uuid4()}/members")
    assert resp.status_code == 404


def test_list_members_without_permission_gets_403() -> None:
    app = _app(member=True, allow=False, list_uc=_FakeUseCase(result=[]))
    client = next(_client(app))
    resp = client.get(f"/communities/{uuid.uuid4()}/members")
    assert resp.status_code == 403


# --- remove member ----------------------------------------------------------


def test_remove_member_authorized_returns_204() -> None:
    app = _app(member=True, allow=True, remove_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.delete(f"/communities/{uuid.uuid4()}/members/{uuid.uuid4()}")
    assert resp.status_code == 204


def test_remove_member_non_member_gets_404() -> None:
    app = _app(member=False, allow=True, remove_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.delete(f"/communities/{uuid.uuid4()}/members/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_remove_member_without_permission_gets_403() -> None:
    app = _app(member=True, allow=False, remove_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.delete(f"/communities/{uuid.uuid4()}/members/{uuid.uuid4()}")
    assert resp.status_code == 403


def test_remove_missing_member_returns_404() -> None:
    app = _app(
        member=True,
        allow=True,
        remove_uc=_FakeUseCase(error=MembershipNotFoundError("x")),
    )
    client = next(_client(app))
    resp = client.delete(f"/communities/{uuid.uuid4()}/members/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_remove_last_owner_returns_409() -> None:
    app = _app(
        member=True,
        allow=True,
        remove_uc=_FakeUseCase(error=LastOwnerRemovalError("x")),
    )
    client = next(_client(app))
    resp = client.delete(f"/communities/{uuid.uuid4()}/members/{uuid.uuid4()}")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "last_owner"


# --- assign / unassign role -------------------------------------------------


def test_assign_role_authorized_returns_204() -> None:
    app = _app(member=True, allow=True, assign_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(
        f"/communities/{uuid.uuid4()}/members/{uuid.uuid4()}/roles",
        json={"role_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 204


def test_assign_role_without_permission_gets_403() -> None:
    app = _app(member=True, allow=False, assign_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(
        f"/communities/{uuid.uuid4()}/members/{uuid.uuid4()}/roles",
        json={"role_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 403


def test_assign_role_cross_community_returns_404() -> None:
    app = _app(
        member=True, allow=True, assign_uc=_FakeUseCase(error=RoleNotFoundError("x"))
    )
    client = next(_client(app))
    resp = client.post(
        f"/communities/{uuid.uuid4()}/members/{uuid.uuid4()}/roles",
        json={"role_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 404


def test_assign_role_invalid_role_id_returns_422() -> None:
    app = _app(member=True, allow=True, assign_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(
        f"/communities/{uuid.uuid4()}/members/{uuid.uuid4()}/roles",
        json={"role_id": "not-a-uuid"},
    )
    assert resp.status_code == 422


def test_unassign_role_authorized_returns_204() -> None:
    app = _app(member=True, allow=True, unassign_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.delete(
        f"/communities/{uuid.uuid4()}/members/{uuid.uuid4()}/roles/{uuid.uuid4()}"
    )
    assert resp.status_code == 204


def test_unassign_last_owner_returns_409() -> None:
    app = _app(
        member=True,
        allow=True,
        unassign_uc=_FakeUseCase(error=LastOwnerRemovalError("x")),
    )
    client = next(_client(app))
    resp = client.delete(
        f"/communities/{uuid.uuid4()}/members/{uuid.uuid4()}/roles/{uuid.uuid4()}"
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "last_owner"


def test_unassign_role_non_member_gets_404() -> None:
    app = _app(member=False, allow=True, unassign_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.delete(
        f"/communities/{uuid.uuid4()}/members/{uuid.uuid4()}/roles/{uuid.uuid4()}"
    )
    assert resp.status_code == 404
