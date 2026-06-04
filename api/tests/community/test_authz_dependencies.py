"""Dependency-level tests for the FastAPI authorization helpers (Section 6.4).

No real endpoint uses these yet (#69 does), so the contract is exercised through
minimal test-only routes mounted on a throwaway app. The PermissionChecker and
MembershipVisibility Ports are overridden with fakes (NFR-TEST-1). Verifies the
two-layer mapping: non-member -> 404, member-without-permission -> 403,
authorized -> 200; plus the platform-admin requirement.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

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
    get_membership_visibility,
    get_permission_checker,
    require_permission,
    require_platform_admin,
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
        self.calls: list[tuple[AuthUser, Permission, ResourceRef]] = []

    async def can(
        self, *, user: AuthUser, operation: Permission, resource: ResourceRef
    ) -> bool:
        self.calls.append((user, operation, resource))
        return self._allow


def _app(
    *,
    member: bool,
    allow: bool,
    platform_admin: bool = False,
) -> tuple[FastAPI, _FakeChecker]:
    app = FastAPI()
    checker = _FakeChecker(allow=allow)

    @app.get(
        "/communities/{community_id}/ping",
        dependencies=[Depends(require_permission(Permission("server:read")))],
    )
    async def _ping() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get(
        "/admin/ping",
        dependencies=[Depends(require_platform_admin)],
    )
    async def _admin_ping() -> dict[str, str]:
        return {"ok": "admin"}

    user = make_user()
    user.is_platform_admin = platform_admin
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    app.dependency_overrides[get_permission_checker] = lambda: checker
    return app, checker


def _client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def test_non_member_gets_404() -> None:
    app, _ = _app(member=False, allow=True)
    client = next(_client(app))
    resp = client.get(f"/communities/{uuid.uuid4()}/ping")
    assert resp.status_code == 404


def test_member_without_permission_gets_403() -> None:
    app, _ = _app(member=True, allow=False)
    client = next(_client(app))
    resp = client.get(f"/communities/{uuid.uuid4()}/ping")
    assert resp.status_code == 403


def test_authorized_member_gets_200() -> None:
    app, checker = _app(member=True, allow=True)
    client = next(_client(app))
    community_id = uuid.uuid4()
    resp = client.get(f"/communities/{community_id}/ping")
    assert resp.status_code == 200
    assert resp.json() == {"ok": "yes"}
    # The checker received the path community id and the configured operation.
    (_user, operation, resource) = checker.calls[0]
    assert operation == Permission("server:read")
    assert resource.community_id == CommunityId(community_id)


def test_platform_admin_required_allows_admin() -> None:
    app, _ = _app(member=False, allow=False, platform_admin=True)
    client = next(_client(app))
    resp = client.get("/admin/ping")
    assert resp.status_code == 200
    assert resp.json() == {"ok": "admin"}


def test_platform_admin_required_rejects_non_admin() -> None:
    app, _ = _app(member=False, allow=False, platform_admin=False)
    client = next(_client(app))
    resp = client.get("/admin/ping")
    assert resp.status_code == 403
