"""Endpoint tests for the community routers (Section 6.2, 6.4).

The HTTP boundary is exercised in-process via FastAPI's TestClient with the use
cases and authorization Ports faked (NFR-TEST-1, no database). Verifies:

- provisioning is platform-admin only (403 otherwise) and maps domain errors;
- read/update/delete apply the two-layer gate: non-member -> 404,
  member-without-permission -> 403, authorized member -> 200/204;
- a platform admin who is not a member still gets 404 on read (the admin axis
  governs provisioning, not community internals — FR-AUTHZ-5 + FR-COMM-3);
- list-my-communities returns the requesting user's communities;
- a grant/role in community A does not open community B's routes.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mc_server_dashboard_api.community.application.list_all_communities import (
    CommunityPage,
)
from mc_server_dashboard_api.community.domain.entities import (
    Community,
    CommunitySummary,
)
from mc_server_dashboard_api.community.domain.errors import (
    CommunityAlreadyExistsError,
    CommunityNotFoundError,
    OwnerUserNotFoundError,
)
from mc_server_dashboard_api.community.domain.permission_checker import (
    MembershipVisibility,
    PermissionChecker,
)
from mc_server_dashboard_api.community.domain.value_objects import (
    AuthUser,
    CommunityId,
    CommunityName,
    Permission,
    ResourceRef,
    UserId,
)
from mc_server_dashboard_api.dependencies import (
    get_current_user,
    get_delete_community,
    get_list_all_communities,
    get_list_my_communities,
    get_membership_visibility,
    get_permission_checker,
    get_provision_community,
    get_read_community,
    get_rename_community,
)
from tests.identity.fakes import make_user

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


def _community(name: str = "guild") -> Community:
    return Community(
        id=CommunityId.new(),
        name=CommunityName(name),
        created_at=_NOW,
        updated_at=_NOW,
    )


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


_shared_app: FastAPI


@pytest.fixture(autouse=True)
def _bind_shared_app(shared_app: FastAPI) -> None:
    global _shared_app
    _shared_app = shared_app


def _client(app: object) -> Iterator[TestClient]:
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client


# The app-building helpers below reuse the per-worker shared app and clear its
# dependency overrides on entry, so a helper called twice in one test starts
# clean (the shared_app wrapper clears between tests).


# --- provisioning (platform-admin axis) ------------------------------------


def _provision_app(
    *, platform_admin: bool, use_case: _FakeUseCase | None = None
) -> object:
    app = _shared_app
    app.dependency_overrides.clear()
    user = make_user()
    user.is_platform_admin = platform_admin
    app.dependency_overrides[get_current_user] = lambda: user
    if use_case is not None:
        app.dependency_overrides[get_provision_community] = lambda: use_case
    return app


def test_provision_requires_platform_admin() -> None:
    app = _provision_app(platform_admin=False, use_case=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(
        "/api/communities",
        json={"name": "guild", "owner_user_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 403


def test_provision_returns_201() -> None:
    community = _community()
    use_case = _FakeUseCase(result=community)
    app = _provision_app(platform_admin=True, use_case=use_case)
    client = next(_client(app))
    resp = client.post(
        "/api/communities",
        json={"name": "guild", "owner_user_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 201
    assert resp.json() == {"id": str(community.id.value), "name": "guild"}


def test_provision_unknown_owner_returns_422() -> None:
    use_case = _FakeUseCase(error=OwnerUserNotFoundError("x"))
    app = _provision_app(platform_admin=True, use_case=use_case)
    client = next(_client(app))
    resp = client.post(
        "/api/communities",
        json={"name": "guild", "owner_user_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 422
    assert resp.json()["reason"] == "owner_not_found"


def test_provision_duplicate_name_returns_409() -> None:
    use_case = _FakeUseCase(error=CommunityAlreadyExistsError("guild"))
    app = _provision_app(platform_admin=True, use_case=use_case)
    client = next(_client(app))
    resp = client.post(
        "/api/communities",
        json={"name": "guild", "owner_user_id": str(uuid.uuid4())},
    )
    assert resp.status_code == 409


def test_provision_invalid_owner_id_returns_422() -> None:
    app = _provision_app(platform_admin=True, use_case=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(
        "/api/communities",
        json={"name": "guild", "owner_user_id": "not-a-uuid"},
    )
    assert resp.status_code == 422


# --- read/update/delete two-layer gate -------------------------------------


def _managed_app(
    *,
    member: bool,
    allow: bool,
    platform_admin: bool = False,
    read_uc: _FakeUseCase | None = None,
    rename_uc: _FakeUseCase | None = None,
    delete_uc: _FakeUseCase | None = None,
) -> object:
    app = _shared_app
    app.dependency_overrides.clear()
    user = make_user()
    user.is_platform_admin = platform_admin
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(allow=allow)
    if read_uc is not None:
        app.dependency_overrides[get_read_community] = lambda: read_uc
    if rename_uc is not None:
        app.dependency_overrides[get_rename_community] = lambda: rename_uc
    if delete_uc is not None:
        app.dependency_overrides[get_delete_community] = lambda: delete_uc
    return app


def test_read_non_member_gets_404() -> None:
    app = _managed_app(member=False, allow=True, read_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.get(f"/api/communities/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_read_member_without_permission_gets_403() -> None:
    app = _managed_app(member=True, allow=False, read_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.get(f"/api/communities/{uuid.uuid4()}")
    assert resp.status_code == 403


def test_read_authorized_member_gets_200() -> None:
    community = _community()
    app = _managed_app(member=True, allow=True, read_uc=_FakeUseCase(result=community))
    client = next(_client(app))
    resp = client.get(f"/api/communities/{community.id.value}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "guild"


def test_read_concurrent_delete_gets_404() -> None:
    # The community vanished between the visibility check and the read: keep the
    # no-existence-signal posture (404), matching rename/delete (Section 6.4).
    app = _managed_app(
        member=True,
        allow=True,
        read_uc=_FakeUseCase(error=CommunityNotFoundError("gone")),
    )
    client = next(_client(app))
    resp = client.get(f"/api/communities/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_read_platform_admin_non_member_still_gets_404() -> None:
    # The admin axis governs provisioning, not community internals (FR-AUTHZ-5).
    app = _managed_app(
        member=False, allow=True, platform_admin=True, read_uc=_FakeUseCase()
    )
    client = next(_client(app))
    resp = client.get(f"/api/communities/{uuid.uuid4()}")
    assert resp.status_code == 404


def test_rename_authorized_returns_200() -> None:
    community = _community("new")
    app = _managed_app(
        member=True, allow=True, rename_uc=_FakeUseCase(result=community)
    )
    client = next(_client(app))
    resp = client.patch(f"/api/communities/{community.id.value}", json={"name": "new"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "new"


def test_rename_member_without_permission_gets_403() -> None:
    app = _managed_app(member=True, allow=False, rename_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.patch(f"/api/communities/{uuid.uuid4()}", json={"name": "new"})
    assert resp.status_code == 403


def test_delete_authorized_returns_204() -> None:
    app = _managed_app(member=True, allow=True, delete_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.delete(f"/api/communities/{uuid.uuid4()}")
    assert resp.status_code == 204


def test_delete_204_carries_no_content_type() -> None:
    # A 204 No Content must not advertise an entity body (issue #633): the
    # default JSONResponse otherwise stamps Content-Type: application/json onto
    # the empty body. Asserted on the representative community delete; the strip
    # is centralized so it covers every 204 route.
    app = _managed_app(member=True, allow=True, delete_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.delete(f"/api/communities/{uuid.uuid4()}")
    assert resp.status_code == 204
    assert "content-type" not in resp.headers
    assert "content-length" not in resp.headers


def test_delete_non_member_gets_404() -> None:
    app = _managed_app(member=False, allow=True, delete_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.delete(f"/api/communities/{uuid.uuid4()}")
    assert resp.status_code == 404


# --- list-my-communities (FR-MEM-4) ----------------------------------------


def test_list_my_communities_returns_user_communities() -> None:
    community = _community()
    use_case = _FakeUseCase(result=[community])
    app = _shared_app
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: make_user()
    app.dependency_overrides[get_list_my_communities] = lambda: use_case
    client = next(_client(app))
    resp = client.get("/api/communities")
    assert resp.status_code == 200
    assert resp.json() == [{"id": str(community.id.value), "name": "guild"}]


# --- delete: platform-admin bypass (issue #489) ----------------------------


def test_delete_platform_admin_non_member_returns_204() -> None:
    # The admin axis pierces isolation for deletion only: a platform admin who is
    # NOT a member can delete any community (orphan cleanup), unlike read/rename.
    app = _managed_app(
        member=False, allow=False, platform_admin=True, delete_uc=_FakeUseCase()
    )
    client = next(_client(app))
    resp = client.delete(f"/api/communities/{uuid.uuid4()}")
    assert resp.status_code == 204


def test_delete_non_admin_non_member_still_gets_404() -> None:
    # The bypass is admin-only; a non-admin non-member keeps the no-signal 404.
    app = _managed_app(member=False, allow=True, delete_uc=_FakeUseCase())
    client = next(_client(app))
    resp = client.delete(f"/api/communities/{uuid.uuid4()}")
    assert resp.status_code == 404


# --- list-all-communities (platform-admin axis, issue #489) ----------------


def _summary(name: str, *, members: int = 0, servers: int = 0) -> CommunitySummary:
    return CommunitySummary(
        id=CommunityId.new(),
        name=CommunityName(name),
        created_at=_NOW,
        member_count=members,
        server_count=servers,
    )


def _admin_list_app(*, platform_admin: bool, use_case: _FakeUseCase) -> object:
    app = _shared_app
    app.dependency_overrides.clear()
    user = make_user()
    user.is_platform_admin = platform_admin
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_list_all_communities] = lambda: use_case
    return app


def test_list_all_communities_requires_platform_admin() -> None:
    use_case = _FakeUseCase(result=CommunityPage(communities=[], total=0))
    app = _admin_list_app(platform_admin=False, use_case=use_case)
    client = next(_client(app))
    assert client.get("/api/admin/communities").status_code == 403


def test_list_all_communities_returns_page_with_counts() -> None:
    summary = _summary("guild", members=3, servers=2)
    use_case = _FakeUseCase(result=CommunityPage(communities=[summary], total=1))
    app = _admin_list_app(platform_admin=True, use_case=use_case)
    client = next(_client(app))
    resp = client.get("/api/admin/communities?limit=10&offset=0")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["limit"] == 10
    assert body["offset"] == 0
    row = body["communities"][0]
    assert row["id"] == str(summary.id.value)
    assert row["name"] == "guild"
    assert row["member_count"] == 3
    assert row["server_count"] == 2
    assert "created_at" in row
    assert use_case.calls == [{"limit": 10, "offset": 0}]


def test_list_all_communities_default_pagination() -> None:
    use_case = _FakeUseCase(result=CommunityPage(communities=[], total=0))
    app = _admin_list_app(platform_admin=True, use_case=use_case)
    client = next(_client(app))
    assert client.get("/api/admin/communities").status_code == 200
    assert use_case.calls == [{"limit": 50, "offset": 0}]


def test_list_all_communities_rejects_out_of_range_limit() -> None:
    use_case = _FakeUseCase(result=CommunityPage(communities=[], total=0))
    app = _admin_list_app(platform_admin=True, use_case=use_case)
    client = next(_client(app))
    assert client.get("/api/admin/communities?limit=0").status_code == 422
    assert client.get("/api/admin/communities?limit=101").status_code == 422


# --- cross-community isolation ---------------------------------------------


def test_grant_in_one_community_does_not_open_another() -> None:
    """A member visible+permitted only in A must not pass the gate for B.

    The visibility/checker decision is keyed on the path ``community_id``; this
    asserts the route forwards the *path* community to the gate, so a yes for one
    community cannot be reused for another.
    """

    community_a = _community("a")
    community_b = _community("b")

    class _ScopedVisibility(MembershipVisibility):
        async def is_member(
            self, *, user_id: UserId, community_id: CommunityId
        ) -> bool:
            return community_id == community_a.id

    app = _shared_app
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: make_user()
    app.dependency_overrides[get_membership_visibility] = _ScopedVisibility
    app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(allow=True)
    app.dependency_overrides[get_read_community] = lambda: _FakeUseCase(
        result=community_a
    )
    client = next(_client(app))

    # Member of A -> 200.
    assert client.get(f"/api/communities/{community_a.id.value}").status_code == 200
    # Not a member of B -> 404, despite full permission in A.
    assert client.get(f"/api/communities/{community_b.id.value}").status_code == 404
