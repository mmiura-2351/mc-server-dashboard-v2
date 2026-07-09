"""Endpoint tests for the dependency auto-resolution routes (issue #1309).

The HTTP boundary is exercised in-process via FastAPI's TestClient with the use
cases and authorization Ports faked (NFR-TEST-1, no database). Verifies the
two-layer gate per route, the plan/apply 200s, and the domain-error -> HTTP-code
mapping (server_unsettled 409, catalog_unavailable 502).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
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
    get_apply_plugin_resolution,
    get_current_user,
    get_membership_visibility,
    get_permission_checker,
    get_resolve_plugin_dependencies,
)
from mc_server_dashboard_api.servers.application.plugin_resolution import (
    ResolutionEntry,
    ResolutionPlan,
    WillImport,
)
from mc_server_dashboard_api.servers.domain.errors import (
    CatalogUnavailableError,
    ServerFilesUnsettledError,
)
from mc_server_dashboard_api.servers.domain.plugin import (
    LoaderType,
    PluginId,
    PluginSource,
    ServerPlugin,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId
from tests.identity.fakes import make_user

_NOW = dt.datetime(2026, 6, 20, 12, 0, tzinfo=dt.timezone.utc)


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

    async def __call__(self, **kwargs: object) -> object:
        if self._error is not None:
            raise self._error
        return self._result


def _client(app: object) -> Iterator[TestClient]:
    with TestClient(app) as client:  # type: ignore[arg-type]
        yield client


_shared_app: FastAPI


@pytest.fixture(autouse=True)
def _bind_shared_app(shared_app: FastAPI) -> None:
    global _shared_app
    _shared_app = shared_app


def _app(
    *,
    member: bool,
    allow: bool,
    resolve: _FakeUseCase | None = None,
    apply: _FakeUseCase | None = None,
) -> object:
    app = _shared_app
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: make_user()
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(allow=allow)
    if resolve is not None:
        app.dependency_overrides[get_resolve_plugin_dependencies] = lambda: resolve
    if apply is not None:
        app.dependency_overrides[get_apply_plugin_resolution] = lambda: apply
    return app


def _url(community: uuid.UUID, server: uuid.UUID, suffix: str) -> str:
    return f"/api/communities/{community}/servers/{server}/plugins{suffix}"


def _plan() -> ResolutionPlan:
    return ResolutionPlan(
        entries=[
            ResolutionEntry(
                dep_identifier="fabric-api",
                required_range=">=0.90.0",
                status="needs_import",
                will_import=WillImport(
                    project_id="FABRICAPI",
                    version_id="VER1",
                    slug="fabric-api",
                    version_number="0.92.0",
                ),
            )
        ]
    )


def _plugin() -> ServerPlugin:
    return ServerPlugin(
        id=PluginId(uuid.uuid4()),
        server_id=ServerId(uuid.uuid4()),
        rel_path="mods/fabric-api.jar",
        filename="fabric-api.jar",
        display_name="Fabric API",
        description=None,
        loader_type=LoaderType.MOD,
        source=PluginSource.MODRINTH,
        source_project_id="FABRICAPI",
        source_version_id="VER1",
        version_number="0.92.0",
        checksum_sha512="abc",
        sha256="def",
        size_bytes=1024,
        enabled=True,
        installed_by=None,
        created_at=_NOW,
        updated_at=_NOW,
        mod_identifier="fabric-api",
    )


# --- two-layer gate --------------------------------------------------------


def test_non_member_gets_404_on_plan() -> None:
    app = _app(member=False, allow=True, resolve=_FakeUseCase(result=_plan()))
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "/resolve"))
    assert resp.status_code == 404


def test_member_without_permission_gets_403_on_apply() -> None:
    app = _app(member=True, allow=False, apply=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "/resolve/apply"))
    assert resp.status_code == 403


# --- plan ------------------------------------------------------------------


def test_plan_returns_200() -> None:
    app = _app(member=True, allow=True, resolve=_FakeUseCase(result=_plan()))
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "/resolve"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["entries"][0]["dep_identifier"] == "fabric-api"
    assert body["entries"][0]["status"] == "needs_import"
    assert body["entries"][0]["will_import"]["project_id"] == "FABRICAPI"
    assert "validation" in body


def test_plan_catalog_unavailable_is_502() -> None:
    app = _app(
        member=True,
        allow=True,
        resolve=_FakeUseCase(error=CatalogUnavailableError("down")),
    )
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "/resolve"))
    assert resp.status_code == 502
    assert resp.json()["reason"] == "catalog_unavailable"


# --- apply -----------------------------------------------------------------


def test_apply_returns_200() -> None:
    app = _app(
        member=True,
        allow=True,
        apply=_FakeUseCase(result=(_plan(), [_plugin()], ["broken-lib"])),
    )
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "/resolve/apply"))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["installed"]) == 1
    assert body["installed"][0]["source_project_id"] == "FABRICAPI"
    assert body["failed"] == ["broken-lib"]
    assert body["plan"]["entries"][0]["dep_identifier"] == "fabric-api"


def test_apply_server_unsettled_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        apply=_FakeUseCase(error=ServerFilesUnsettledError("sid")),
    )
    client = next(_client(app))
    resp = client.post(_url(uuid.uuid4(), uuid.uuid4(), "/resolve/apply"))
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_unsettled"
