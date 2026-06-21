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
    get_audit_name_resolver,
    get_current_user,
    get_list_audit_log,
    get_membership_visibility,
    get_permission_checker,
)
from tests.audit.fakes import CapturingAuditQuery, FakeNameResolver
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
    resolver: FakeNameResolver | None = None,
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
    app.dependency_overrides[get_audit_name_resolver] = lambda: (
        resolver if resolver is not None else FakeNameResolver()
    )
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


# --- read-time name enrichment (issue #682) --------------------------------


def _record(
    *,
    actor_id: uuid.UUID | None = None,
    community_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
) -> AuditRecord:
    return AuditRecord(
        id=uuid.uuid4(),
        operation="server:create",
        outcome=Outcome.SUCCESS,
        created_at=dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc),
        actor_id=actor_id,
        community_id=community_id,
        target_type=target_type,
        target_id=target_id,
    )


def test_resolves_actor_username_and_community_name() -> None:
    actor = uuid.uuid4()
    record = _record(actor_id=actor, community_id=_COMMUNITY)
    resolver = FakeNameResolver(
        usernames={actor: "alice"}, community_names={_COMMUNITY: "Acme"}
    )
    app = _app(
        CapturingAuditQuery(records=[record]), platform_admin=True, resolver=resolver
    )
    client = next(_client(app))

    row = client.get("/api/audit").json()["records"][0]

    assert row["actor_id"] == str(actor)
    assert row["actor_username"] == "alice"
    assert row["community_name"] == "Acme"


def test_resolves_user_target_to_username() -> None:
    target = uuid.uuid4()
    record = _record(target_type="user", target_id=target)
    resolver = FakeNameResolver(usernames={target: "bob"})
    app = _app(
        CapturingAuditQuery(records=[record]), platform_admin=True, resolver=resolver
    )
    client = next(_client(app))

    row = client.get("/api/audit").json()["records"][0]

    assert row["target_name"] == "bob"


def test_resolves_server_target_to_server_name() -> None:
    target = uuid.uuid4()
    record = _record(target_type="server", target_id=target)
    resolver = FakeNameResolver(server_names={target: "survival"})
    app = _app(
        CapturingAuditQuery(records=[record]), platform_admin=True, resolver=resolver
    )
    client = next(_client(app))

    row = client.get("/api/audit").json()["records"][0]

    assert row["target_name"] == "survival"


def test_resolves_file_target_as_server_name() -> None:
    # A `file` target's id is the owning server's UUID (audit convention), so it
    # resolves to the server name.
    target = uuid.uuid4()
    record = _record(target_type="file", target_id=target)
    resolver = FakeNameResolver(server_names={target: "creative"})
    app = _app(
        CapturingAuditQuery(records=[record]), platform_admin=True, resolver=resolver
    )
    client = next(_client(app))

    row = client.get("/api/audit").json()["records"][0]

    assert row["target_name"] == "creative"


def test_target_name_null_for_type_with_no_name_source() -> None:
    # A `role` target has no name source the resolver knows about: leave it null.
    record = _record(target_type="role", target_id=uuid.uuid4())
    app = _app(CapturingAuditQuery(records=[record]), platform_admin=True)
    client = next(_client(app))

    row = client.get("/api/audit").json()["records"][0]

    assert row["target_name"] is None


def test_deleted_subject_falls_back_to_null_names() -> None:
    # Soft-referenced ids outlive their subjects: a deleted actor/target/community
    # is absent from the resolver, so the display fields are null (the client keeps
    # showing the raw id).
    record = _record(
        actor_id=uuid.uuid4(),
        community_id=uuid.uuid4(),
        target_type="server",
        target_id=uuid.uuid4(),
    )
    app = _app(
        CapturingAuditQuery(records=[record]),
        platform_admin=True,
        resolver=FakeNameResolver(),
    )
    client = next(_client(app))

    row = client.get("/api/audit").json()["records"][0]

    assert row["actor_username"] is None
    assert row["target_name"] is None
    assert row["community_name"] is None


def test_batches_lookups_across_rows() -> None:
    # Distinct ids on the page are resolved in a single batched call per kind, not
    # per row (no N+1). Two rows share one actor; both their distinct user ids are
    # asked for at once.
    actor = uuid.uuid4()
    user_target = uuid.uuid4()
    records = [
        _record(actor_id=actor, target_type="user", target_id=user_target),
        _record(actor_id=actor, target_type="user", target_id=user_target),
    ]
    resolver = FakeNameResolver(usernames={actor: "alice", user_target: "bob"})
    app = _app(
        CapturingAuditQuery(records=records), platform_admin=True, resolver=resolver
    )
    client = next(_client(app))

    rows = client.get("/api/audit").json()["records"]

    assert [r["actor_username"] for r in rows] == ["alice", "alice"]
    assert [r["target_name"] for r in rows] == ["bob", "bob"]
    # One batched user lookup, holding the two distinct user ids.
    assert len(resolver.user_id_calls) == 1
    assert set(resolver.user_id_calls[0]) == {actor, user_target}


def test_community_audit_enriches_names() -> None:
    actor = uuid.uuid4()
    record = _record(actor_id=actor, community_id=_COMMUNITY)
    resolver = FakeNameResolver(usernames={actor: "carol"})
    app = _app(
        CapturingAuditQuery(records=[record]),
        member=True,
        allow=True,
        resolver=resolver,
    )
    client = next(_client(app))

    row = client.get(f"/api/communities/{_COMMUNITY}/audit").json()["records"][0]

    assert row["actor_username"] == "carol"
