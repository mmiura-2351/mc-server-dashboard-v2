"""Endpoint tests for the catalog routes (issue #1163).

The HTTP boundary is exercised in-process via FastAPI's TestClient with the use
cases and authorization Ports faked (NFR-TEST-1, no database). Verifies:

- the two-layer gate per route (non-member -> 404, member-without-permission ->
  403, authorized member -> 2xx);
- domain-error -> HTTP-code mapping (unsupported_server_type 422,
  catalog_unavailable 502, server_unsettled 409, checksum_mismatch 502, etc.);
- correct status codes for each catalog endpoint.
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
    get_current_user,
    get_get_catalog_project,
    get_install_from_catalog,
    get_membership_visibility,
    get_permission_checker,
    get_search_catalog,
)
from mc_server_dashboard_api.servers.domain.catalog_provider import (
    CatalogProject,
    CatalogSearchResponse,
    CatalogSearchResult,
    CatalogVersion,
)
from mc_server_dashboard_api.servers.domain.errors import (
    CatalogChecksumMismatchError,
    CatalogProjectNotFoundError,
    CatalogUnavailableError,
    PortAlreadyTakenError,
    PortRangeExhaustedError,
    ServerFilesUnsettledError,
    UnsupportedPluginServerTypeError,
)
from mc_server_dashboard_api.servers.domain.plugin import (
    LoaderType,
    PluginId,
    PluginSource,
    ServerPlugin,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId
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


_shared_app: FastAPI


@pytest.fixture(autouse=True)
def _bind_shared_app(shared_app: FastAPI) -> None:
    global _shared_app
    _shared_app = shared_app


def _app(
    *,
    member: bool,
    allow: bool,
    search: _FakeUseCase | None = None,
    get_project: _FakeUseCase | None = None,
    install: _FakeUseCase | None = None,
) -> object:
    app = _shared_app
    app.dependency_overrides.clear()
    app.dependency_overrides[get_current_user] = lambda: make_user()
    app.dependency_overrides[get_membership_visibility] = lambda: _FakeVisibility(
        member=member
    )
    app.dependency_overrides[get_permission_checker] = lambda: _FakeChecker(allow=allow)
    if search is not None:
        app.dependency_overrides[get_search_catalog] = lambda: search
    if get_project is not None:
        app.dependency_overrides[get_get_catalog_project] = lambda: get_project
    if install is not None:
        app.dependency_overrides[get_install_from_catalog] = lambda: install
    return app


def _url(community: uuid.UUID, server: uuid.UUID, suffix: str = "") -> str:
    return f"/api/communities/{community}/servers/{server}/catalog{suffix}"


def _search_result() -> CatalogSearchResponse:
    return CatalogSearchResponse(
        hits=[
            CatalogSearchResult(
                project_id="proj-1",
                slug="test-plugin",
                title="Test Plugin",
                description="A test plugin",
                author="author",
                icon_url=None,
                downloads=100,
                categories=["utility"],
                latest_game_versions=["1.21.1"],
            )
        ],
        total_hits=1,
        offset=0,
        limit=20,
    )


def _project_detail() -> tuple[CatalogProject, list[CatalogVersion]]:
    project = CatalogProject(
        project_id="proj-1",
        slug="test-plugin",
        title="Test Plugin",
        description="A test plugin",
        body="Full description",
        author="author",
        icon_url=None,
        downloads=100,
        categories=["utility"],
        game_versions=["1.21.1"],
        loaders=["paper"],
    )
    versions = [
        CatalogVersion(
            version_id="ver-1",
            version_number="1.0.0",
            name="Release 1.0.0",
            game_versions=["1.21.1"],
            loaders=["paper"],
            files=[],
            date_published="2026-01-01T00:00:00Z",
        )
    ]
    return project, versions


def _plugin() -> ServerPlugin:
    return ServerPlugin(
        id=PluginId(uuid.uuid4()),
        server_id=ServerId(uuid.uuid4()),
        rel_path="plugins/test.jar",
        filename="test.jar",
        display_name="Test Plugin",
        description="A test plugin",
        loader_type=LoaderType.PLUGIN,
        source=PluginSource.MODRINTH,
        source_project_id="proj-1",
        source_version_id="ver-1",
        version_number="1.0.0",
        checksum_sha512="abc123",
        sha256="def456",
        size_bytes=1024,
        enabled=True,
        installed_by=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


# --- two-layer gate --------------------------------------------------------


def test_non_member_gets_404_on_search() -> None:
    app = _app(member=False, allow=True, search=_FakeUseCase(result=_search_result()))
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4(), "/search"), params={"q": "test"})
    assert resp.status_code == 404


def test_member_without_permission_gets_403_on_install() -> None:
    app = _app(member=True, allow=False, install=_FakeUseCase())
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/install"),
        json={"project_id": "proj-1", "version_id": "ver-1"},
    )
    assert resp.status_code == 403


# --- search ----------------------------------------------------------------


def test_search_returns_200() -> None:
    app = _app(member=True, allow=True, search=_FakeUseCase(result=_search_result()))
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4(), "/search"), params={"q": "test"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_hits"] == 1
    assert body["hits"][0]["slug"] == "test-plugin"


def test_search_unsupported_type_is_422() -> None:
    app = _app(
        member=True,
        allow=True,
        search=_FakeUseCase(error=UnsupportedPluginServerTypeError("vanilla")),
    )
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4(), "/search"), params={"q": "test"})
    assert resp.status_code == 422
    assert resp.json()["reason"] == "unsupported_server_type"


def test_search_catalog_unavailable_is_502() -> None:
    app = _app(
        member=True,
        allow=True,
        search=_FakeUseCase(error=CatalogUnavailableError("down")),
    )
    client = next(_client(app))
    resp = client.get(_url(uuid.uuid4(), uuid.uuid4(), "/search"), params={"q": "test"})
    assert resp.status_code == 502
    assert resp.json()["reason"] == "catalog_unavailable"


# --- get project -----------------------------------------------------------


def test_get_project_returns_200() -> None:
    app = _app(
        member=True,
        allow=True,
        get_project=_FakeUseCase(result=_project_detail()),
    )
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/projects/test-plugin"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["project"]["slug"] == "test-plugin"
    assert len(body["versions"]) == 1


def test_get_project_not_found_is_404() -> None:
    app = _app(
        member=True,
        allow=True,
        get_project=_FakeUseCase(error=CatalogProjectNotFoundError("x")),
    )
    client = next(_client(app))
    resp = client.get(
        _url(uuid.uuid4(), uuid.uuid4(), "/projects/nonexistent"),
    )
    assert resp.status_code == 404
    assert resp.json()["reason"] == "catalog_project_not_found"


# --- install from catalog --------------------------------------------------


def test_install_from_catalog_returns_201() -> None:
    p = _plugin()
    app = _app(member=True, allow=True, install=_FakeUseCase(result=p))
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/install"),
        json={"project_id": "proj-1", "version_id": "ver-1"},
    )
    assert resp.status_code == 201
    assert resp.json()["filename"] == "test.jar"


def test_install_from_catalog_unsettled_is_409() -> None:
    app = _app(
        member=True,
        allow=True,
        install=_FakeUseCase(error=ServerFilesUnsettledError("x")),
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/install"),
        json={"project_id": "proj-1", "version_id": "ver-1"},
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "server_unsettled"


def test_install_from_catalog_bedrock_window_exhausted_is_503() -> None:
    # A Geyser install that found no free Bedrock UDP port (issue #1541).
    app = _app(
        member=True,
        allow=True,
        install=_FakeUseCase(error=PortRangeExhaustedError("19132-19231")),
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/install"),
        json={"project_id": "geyser", "version_id": "ver-1"},
    )
    assert resp.status_code == 503
    assert resp.json()["reason"] == "bedrock_port_range_exhausted"


def test_install_from_catalog_bedrock_port_race_is_409() -> None:
    # The UNIQUE(bedrock_port) backstop on a concurrent allocation, translated
    # by the adapter to the typed domain error (issue #1541).
    app = _app(
        member=True,
        allow=True,
        install=_FakeUseCase(error=PortAlreadyTakenError("uq_server_bedrock_port")),
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/install"),
        json={"project_id": "geyser", "version_id": "ver-1"},
    )
    assert resp.status_code == 409
    assert resp.json()["reason"] == "bedrock_port_taken"


def test_install_from_catalog_unavailable_is_502() -> None:
    app = _app(
        member=True,
        allow=True,
        install=_FakeUseCase(error=CatalogUnavailableError("down")),
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/install"),
        json={"project_id": "proj-1", "version_id": "ver-1"},
    )
    assert resp.status_code == 502
    assert resp.json()["reason"] == "catalog_unavailable"


def test_install_from_catalog_checksum_mismatch_is_502() -> None:
    app = _app(
        member=True,
        allow=True,
        install=_FakeUseCase(error=CatalogChecksumMismatchError("x")),
    )
    client = next(_client(app))
    resp = client.post(
        _url(uuid.uuid4(), uuid.uuid4(), "/install"),
        json={"project_id": "proj-1", "version_id": "ver-1"},
    )
    assert resp.status_code == 502
    assert resp.json()["reason"] == "checksum_mismatch"
