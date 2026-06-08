"""Endpoint tests for the audit-log query surface (FR-AUD-3).

Exercised in-process via TestClient with the query use case and authorization
Ports faked (NFR-TEST-1, no database). Verifies the platform-admin gate, the
community two-layer gate (non-member -> 404, member-without-audit:read -> 403),
the Community-scoping invariant (the path Community is forced onto the filter),
and that the query filters are wired through.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.audit.application.list_audit_log import ListAuditLog
from mc_server_dashboard_api.audit.domain.events import AuditRecord, Outcome
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
    get_list_audit_log,
    get_membership_visibility,
    get_permission_checker,
)
from tests.audit.fakes import CapturingAuditQuery
from tests.identity.fakes import make_user

_COMMUNITY = uuid.uuid4()
_RECORD = AuditRecord(
    id=uuid.uuid4(),
    operation="server:create",
    outcome=Outcome.SUCCESS,
    created_at=dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc),
    actor_id=uuid.uuid4(),
    community_id=_COMMUNITY,
    target_type="server",
    target_id=uuid.uuid4(),
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


def _client(app: object) -> Iterator[TestClient]:
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client


def _app(
    query: CapturingAuditQuery,
    *,
    platform_admin: bool = False,
    member: bool = True,
    allow: bool = True,
) -> object:
    app = create_app()
    user = make_user()
    user.is_platform_admin = platform_admin
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_list_audit_log] = lambda: ListAuditLog(query=query)
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(allow=allow)
    return app


# --- platform-admin global view -------------------------------------------


def test_platform_audit_requires_platform_admin() -> None:
    app = _app(CapturingAuditQuery(), platform_admin=False)
    client = next(_client(app))
    assert client.get("/api/audit").status_code == 403


def test_platform_audit_lists_records() -> None:
    query = CapturingAuditQuery(records=[_RECORD])
    app = _app(query, platform_admin=True)
    client = next(_client(app))

    resp = client.get("/api/audit")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["records"]) == 1
    assert body["records"][0]["operation"] == "server:create"
    assert body["records"][0]["outcome"] == "success"
    # Canonical RFC 3339 UTC form: the ``Z`` suffix, not ``+00:00`` (issue #632).
    assert body["records"][0]["created_at"] == "2026-06-04T12:00:00Z"


def test_platform_audit_passes_filters_through() -> None:
    query = CapturingAuditQuery()
    app = _app(query, platform_admin=True)
    client = next(_client(app))
    actor = uuid.uuid4()

    resp = client.get(
        "/api/audit",
        params={
            "community": str(_COMMUNITY),
            "operation": "server:create",
            "actor": str(actor),
            "since": "2026-06-01T00:00:00+00:00",
            "until": "2026-06-30T00:00:00+00:00",
            "limit": 10,
            "offset": 5,
        },
    )

    assert resp.status_code == 200
    assert query.last_filter is not None
    assert query.last_filter.community_id == _COMMUNITY
    assert query.last_filter.operation == "server:create"
    assert query.last_filter.actor_id == actor
    assert query.last_filter.limit == 10
    assert query.last_filter.offset == 5


def test_platform_audit_rejects_oversized_limit() -> None:
    app = _app(CapturingAuditQuery(), platform_admin=True)
    client = next(_client(app))
    assert client.get("/api/audit", params={"limit": 9999}).status_code == 422


# --- community-scoped view -------------------------------------------------


def test_community_audit_non_member_is_404() -> None:
    app = _app(CapturingAuditQuery(), member=False)
    client = next(_client(app))
    assert client.get(f"/api/communities/{_COMMUNITY}/audit").status_code == 404


def test_community_audit_member_without_permission_is_403() -> None:
    app = _app(CapturingAuditQuery(), member=True, allow=False)
    client = next(_client(app))
    assert client.get(f"/api/communities/{_COMMUNITY}/audit").status_code == 403


def test_community_audit_authorized_member_lists_records() -> None:
    query = CapturingAuditQuery(records=[_RECORD])
    app = _app(query, member=True, allow=True)
    client = next(_client(app))

    resp = client.get(f"/api/communities/{_COMMUNITY}/audit")

    assert resp.status_code == 200
    assert len(resp.json()["records"]) == 1


def test_community_audit_forces_path_community_onto_filter() -> None:
    query = CapturingAuditQuery()
    app = _app(query, member=True, allow=True)
    client = next(_client(app))
    other_community = uuid.uuid4()

    # A member cannot read another Community's trail even by passing filters: the
    # path Community is forced onto the query, and there is no community filter param.
    resp = client.get(
        f"/api/communities/{_COMMUNITY}/audit",
        params={"operation": "server:create", "community": str(other_community)},
    )

    assert resp.status_code == 200
    assert query.last_filter is not None
    assert query.last_filter.community_id == _COMMUNITY
