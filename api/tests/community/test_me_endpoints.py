"""Endpoint tests for the effective-permissions self-service route (issue #354).

The HTTP boundary is exercised in-process via FastAPI's TestClient with the use
case and the Layer-1 visibility Port faked (NFR-TEST-1, no database). Verifies
the Layer-1-only gate (non-member -> 404 with no existence signal, member -> 200)
and that the response shape mirrors the use-case result (role-union codes + own
grants), independent of any per-operation permission.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mc_server_dashboard_api.community.application.read_my_permissions import (
    EffectivePermissions,
)
from mc_server_dashboard_api.community.domain.entities import ResourceGrant
from mc_server_dashboard_api.community.domain.permission_checker import (
    MembershipVisibility,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId,
    Permission,
    ResourceGrantId,
    UserId,
)
from mc_server_dashboard_api.dependencies import (
    get_current_user,
    get_membership_visibility,
    get_read_my_effective_permissions,
)
from tests.identity.fakes import make_user

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


class _FakeVisibility(MembershipVisibility):
    def __init__(self, *, member: bool) -> None:
        self._member = member

    async def is_member(self, *, user_id: UserId, community_id: CommunityId) -> bool:
        return self._member


class _FakeUseCase:
    def __init__(self, *, result: EffectivePermissions) -> None:
        self._result = result
        self.calls: list[dict[str, object]] = []

    async def __call__(self, **kwargs: object) -> EffectivePermissions:
        self.calls.append(kwargs)
        return self._result


_shared_app: FastAPI


@pytest.fixture(autouse=True)
def _bind_shared_app(shared_app: FastAPI) -> None:
    global _shared_app
    _shared_app = shared_app


def _client(app: object) -> Iterator[TestClient]:
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client


def _app(*, member: bool, result: EffectivePermissions) -> object:
    # Reuse the per-worker shared app; clear overrides on entry so a helper called
    # twice in one test starts clean (the shared_app wrapper clears between tests).
    app = _shared_app
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: make_user()
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    app.dependency_overrides[get_read_my_effective_permissions] = lambda: _FakeUseCase(
        result=result
    )
    return app


def _grant(community: CommunityId) -> ResourceGrant:
    return ResourceGrant(
        id=ResourceGrantId.new(),
        user_id=UserId(uuid.uuid4()),
        community_id=community,
        resource_type="server",
        resource_id=uuid.uuid4(),
        permissions={Permission("server:start"), Permission("server:stop")},
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_non_member_gets_404() -> None:
    community = CommunityId.new()
    result = EffectivePermissions(permissions=set(), grants=[])
    app = _app(member=False, result=result)
    for client in _client(app):
        response = client.get(f"/api/communities/{community.value}/me/permissions")
        assert response.status_code == 404


def test_member_sees_role_union_and_own_grants() -> None:
    community = CommunityId.new()
    grant = _grant(community)
    result = EffectivePermissions(
        permissions={Permission("server:read"), Permission("file:read")},
        grants=[grant],
    )
    app = _app(member=True, result=result)
    for client in _client(app):
        response = client.get(f"/api/communities/{community.value}/me/permissions")
        assert response.status_code == 200
        body = response.json()
        assert body["permissions"] == ["file:read", "server:read"]
        assert body["grants"] == [
            {
                "resource_type": "server",
                "resource_id": str(grant.resource_id),
                "permissions": ["server:start", "server:stop"],
            }
        ]


def test_member_with_no_permissions_gets_empty_sets() -> None:
    community = CommunityId.new()
    result = EffectivePermissions(permissions=set(), grants=[])
    app = _app(member=True, result=result)
    for client in _client(app):
        response = client.get(f"/api/communities/{community.value}/me/permissions")
        assert response.status_code == 200
        assert response.json() == {"permissions": [], "grants": []}
