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

import pytest
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
    ServerUpdateAuthz,
    get_current_user,
    get_membership_visibility,
    get_permission_checker,
    require_permission,
    require_platform_admin,
    require_server_update_authz,
)
from mc_server_dashboard_api.http_problem import install_problem_handlers
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
    install_problem_handlers(app)
    checker = _FakeChecker(allow=allow)

    @app.get(
        "/api/communities/{community_id}/ping",
        dependencies=[Depends(require_permission(Permission("server:read")))],
    )
    async def _ping() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get(
        "/api/admin/ping",
        dependencies=[Depends(require_platform_admin)],
    )
    async def _admin_ping() -> dict[str, str]:
        return {"ok": "admin"}

    @app.delete(
        "/api/communities/{community_id}/thing",
        dependencies=[
            Depends(
                require_permission(
                    Permission("community:delete"), allow_platform_admin=True
                )
            )
        ],
    )
    async def _thing() -> dict[str, str]:
        return {"ok": "deleted"}

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
    resp = client.get(f"/api/communities/{uuid.uuid4()}/ping")
    assert resp.status_code == 404


def test_member_without_permission_gets_403() -> None:
    app, _ = _app(member=True, allow=False)
    client = next(_client(app))
    resp = client.get(f"/api/communities/{uuid.uuid4()}/ping")
    assert resp.status_code == 403


def test_403_body_names_the_required_permission() -> None:
    # The 403 problem body carries the checked operation as the ``permission``
    # extension member so the Web UI can name it in the denial toast (#425),
    # while ``reason`` stays the stable ``"forbidden"`` code.
    app, _ = _app(member=True, allow=False)
    client = next(_client(app))
    resp = client.get(f"/api/communities/{uuid.uuid4()}/ping")
    assert resp.status_code == 403
    body = resp.json()
    assert body["reason"] == "forbidden"
    assert body["permission"] == "server:read"


def test_authorized_member_gets_200() -> None:
    app, checker = _app(member=True, allow=True)
    client = next(_client(app))
    community_id = uuid.uuid4()
    resp = client.get(f"/api/communities/{community_id}/ping")
    assert resp.status_code == 200
    assert resp.json() == {"ok": "yes"}
    # The checker received the path community id and the configured operation.
    (_user, operation, resource) = checker.calls[0]
    assert operation == Permission("server:read")
    assert resource.community_id == CommunityId(community_id)


def test_malformed_community_id_reported_once() -> None:
    # Real community-scoped routes declare ``community_id: uuid.UUID`` on the
    # handler too. The auth dependency must NOT re-declare it as its own typed
    # parameter, or FastAPI validates and reports the same malformed id twice
    # in the 422 errors array (#631). It must appear exactly once.
    app = FastAPI()
    install_problem_handlers(app)
    checker = _FakeChecker(allow=True)

    @app.get(
        "/api/communities/{community_id}/ping",
        dependencies=[Depends(require_permission(Permission("server:read")))],
    )
    async def _ping(community_id: uuid.UUID) -> dict[str, str]:
        return {"ok": "yes"}

    user = make_user()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=True
    )
    app.dependency_overrides[get_permission_checker] = lambda: checker

    client = next(_client(app))
    resp = client.get("/api/communities/notauuid/ping")
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    community_id_errors = [e for e in errors if e["loc"] == ["path", "community_id"]]
    assert len(community_id_errors) == 1
    assert community_id_errors[0]["type"] == "uuid_parsing"


def _roles_app() -> FastAPI:
    # A two-path-param route mirroring real ``/communities/{community_id}/roles/
    # {role_id}`` handlers, which declare BOTH ids as ``uuid.UUID`` params.
    app = FastAPI()
    install_problem_handlers(app)
    checker = _FakeChecker(allow=True)

    @app.get(
        "/api/communities/{community_id}/roles/{role_id}",
        dependencies=[
            Depends(
                require_permission(
                    Permission("role:read"),
                    resource_type="role",
                    resource_id_param="role_id",
                )
            )
        ],
    )
    async def _role(community_id: uuid.UUID, role_id: uuid.UUID) -> dict[str, str]:
        return {"ok": "yes"}

    user = make_user()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=True
    )
    app.dependency_overrides[get_permission_checker] = lambda: checker
    return app


def test_two_malformed_path_ids_have_no_duplicate_community_id() -> None:
    # With both ids malformed the gate rejects on ``community_id`` first (it is
    # parsed at the edge to scope the membership check, before the per-resource
    # ``role_id``); the key contract is that ``community_id`` is never duplicated
    # in the 422 errors array (#631), which the pre-fix dependency violated.
    client = next(_client(_roles_app()))
    resp = client.get("/api/communities/badc/roles/badr")
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    community_errors = [e for e in errors if e["loc"] == ["path", "community_id"]]
    assert len(community_errors) == 1


def test_malformed_role_id_reported_once() -> None:
    # A valid community but malformed ``role_id`` still yields the per-resource
    # 422 exactly once (#630 behavior preserved, #631 no duplication).
    client = next(_client(_roles_app()))
    resp = client.get(f"/api/communities/{uuid.uuid4()}/roles/badr")
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    role_errors = [e for e in errors if e["loc"] == ["path", "role_id"]]
    assert len(role_errors) == 1
    assert role_errors[0]["type"] == "uuid_parsing"


def test_platform_admin_required_allows_admin() -> None:
    app, _ = _app(member=False, allow=False, platform_admin=True)
    client = next(_client(app))
    resp = client.get("/api/admin/ping")
    assert resp.status_code == 200
    assert resp.json() == {"ok": "admin"}


def test_platform_admin_required_rejects_non_admin() -> None:
    app, _ = _app(member=False, allow=False, platform_admin=False)
    client = next(_client(app))
    resp = client.get("/api/admin/ping")
    assert resp.status_code == 403


def test_allow_platform_admin_bypasses_isolation_for_admin() -> None:
    # A platform admin who is NOT a member passes an allow_platform_admin gate
    # (community deletion / orphan cleanup, #489), without the two-layer check.
    app, checker = _app(member=False, allow=False, platform_admin=True)
    client = next(_client(app))
    resp = client.delete(f"/api/communities/{uuid.uuid4()}/thing")
    assert resp.status_code == 200
    assert resp.json() == {"ok": "deleted"}
    # The Layer-2 checker was never consulted (the bypass short-circuits first).
    assert checker.calls == []


def test_allow_platform_admin_does_not_help_non_admin() -> None:
    # The bypass is admin-only: a non-admin non-member still gets the 404.
    app, _ = _app(member=False, allow=True, platform_admin=False)
    client = next(_client(app))
    resp = client.delete(f"/api/communities/{uuid.uuid4()}/thing")
    assert resp.status_code == 404


def test_resource_type_without_id_param_is_a_construction_error() -> None:
    # both-or-neither: passing only resource_type fails fast at wiring time.
    with pytest.raises(ValueError):
        require_permission(Permission("server:stop"), resource_type="server")


def test_resource_id_param_without_type_is_a_construction_error() -> None:
    with pytest.raises(ValueError):
        require_permission(Permission("server:stop"), resource_id_param="server_id")


def test_missing_path_param_is_a_server_misconfiguration() -> None:
    # The route omits the {server_id} segment the dependency expects: fail-closed
    # with a 500, not an opaque KeyError.
    app = FastAPI()
    checker = _FakeChecker(allow=True)

    @app.get(
        "/api/communities/{community_id}/oops",
        dependencies=[
            Depends(
                require_permission(
                    Permission("server:stop"),
                    resource_type="server",
                    resource_id_param="server_id",
                )
            )
        ],
    )
    async def _oops() -> dict[str, str]:
        return {"ok": "yes"}

    user = make_user()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=True
    )
    app.dependency_overrides[get_permission_checker] = lambda: checker

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(f"/api/communities/{uuid.uuid4()}/oops")
    assert resp.status_code == 500


def test_malformed_resource_id_param_returns_422() -> None:
    # A non-UUID {server_id} must surface as the same 422 FastAPI emits for a
    # bad ``server_id: uuid.UUID`` route param, not the 500 the unguarded
    # ``uuid.UUID(...)`` parse used to raise from the auth dependency (#630).
    app = FastAPI()
    install_problem_handlers(app)
    checker = _FakeChecker(allow=True)

    @app.get(
        "/api/communities/{community_id}/servers/{server_id}",
        dependencies=[
            Depends(
                require_permission(
                    Permission("server:read"),
                    resource_type="server",
                    resource_id_param="server_id",
                )
            )
        ],
    )
    async def _server() -> dict[str, str]:
        return {"ok": "yes"}

    user = make_user()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=True
    )
    app.dependency_overrides[get_permission_checker] = lambda: checker

    client = next(_client(app))
    resp = client.get(f"/api/communities/{uuid.uuid4()}/servers/badsrv")
    assert resp.status_code == 422
    body = resp.json()
    assert body["errors"][0]["loc"] == ["path", "server_id"]
    assert body["errors"][0]["type"] == "uuid_parsing"


# --- require_server_update_authz (issue #458) ------------------------------
#
# The server-PATCH gate cannot pin a single operation: the required code depends
# on which keys the PATCH changes (server:update vs backup:schedule), known only
# in the use case. So this dependency does Layer-1 membership at the edge and
# hands back an ``authorize(code)`` callable bound to the server resource.


def _authz_app() -> tuple[FastAPI, _FakeChecker]:
    app = FastAPI()
    install_problem_handlers(app)
    checker = _FakeChecker(allow=True)

    @app.patch("/api/communities/{community_id}/servers/{server_id}")
    async def _patch(
        authz: ServerUpdateAuthz = Depends(
            require_server_update_authz(
                resource_type="server", resource_id_param="server_id"
            )
        ),
    ) -> dict[str, bool]:
        # Exercise the bound callable so the test can observe the checker call.
        allowed = await authz.authorize("backup:schedule")
        return {"allowed": allowed}

    user = make_user()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=True
    )
    app.dependency_overrides[get_permission_checker] = lambda: checker
    return app, checker


def test_server_update_authz_non_member_gets_404() -> None:
    app, _ = _authz_app()
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=False
    )
    client = next(_client(app))
    resp = client.patch(
        f"/api/communities/{uuid.uuid4()}/servers/{uuid.uuid4()}", json={}
    )
    assert resp.status_code == 404


def test_server_update_authz_binds_callable_to_resource() -> None:
    app, checker = _authz_app()
    client = next(_client(app))
    community_id = uuid.uuid4()
    server_id = uuid.uuid4()
    resp = client.patch(f"/api/communities/{community_id}/servers/{server_id}", json={})
    assert resp.status_code == 200
    assert resp.json() == {"allowed": True}
    # The callable delegated to the checker with the requested code and the
    # per-server resource scope.
    (_user, operation, resource) = checker.calls[0]
    assert operation == Permission("backup:schedule")
    assert resource.community_id == CommunityId(community_id)
    assert resource.resource_type == "server"
    assert resource.resource_id == server_id


def test_server_update_authz_malformed_server_id_returns_422() -> None:
    # The PATCH gate shares ``_resource_id_from_path``; a non-UUID {server_id}
    # must likewise yield a 422, not a 500 (#630).
    app, _ = _authz_app()
    client = next(_client(app))
    resp = client.patch(f"/api/communities/{uuid.uuid4()}/servers/badsrv", json={})
    assert resp.status_code == 422
    body = resp.json()
    assert body["errors"][0]["loc"] == ["path", "server_id"]
    assert body["errors"][0]["type"] == "uuid_parsing"
