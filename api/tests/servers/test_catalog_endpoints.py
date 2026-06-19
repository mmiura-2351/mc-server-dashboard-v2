"""Endpoint tests for the Modrinth catalog + import routes (issue #1264).

The HTTP boundary is exercised in-process via FastAPI's TestClient with the
catalog provider / import use case faked (NFR-TEST-1, no database, no network).
Verifies:

- catalog search 200 + facet/pagination forwarding;
- project detail 200, not-found 404, source-down 502;
- import 201 (+ audit), auth gate, error mapping (not found 404, invalid jar 422,
  too large 413, integrity 502).
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.audit.domain import operations as ops
from mc_server_dashboard_api.audit.domain.events import Outcome
from mc_server_dashboard_api.dependencies import (
    get_audit_recorder,
    get_catalog_provider,
    get_current_user,
    get_import_mod,
    require_server_update_in_any_community,
)
from mc_server_dashboard_api.servers.domain.catalog_provider import (
    CatalogDependency,
    CatalogProject,
    CatalogProjectNotFoundError,
    CatalogSearchHit,
    CatalogSearchResult,
    CatalogUnavailableError,
    CatalogVersion,
)
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    InvalidModJarError,
    ModIntegrityError,
)
from mc_server_dashboard_api.servers.domain.mod import Mod, ModId
from tests.audit.fakes import RecordingAuditRecorder
from tests.identity.fakes import make_user
from tests.servers.fakes import FakeCatalogProvider

_NOW = dt.datetime(2026, 6, 16, 12, 0, 0, tzinfo=dt.timezone.utc)
_MOD_ID = ModId(uuid.UUID("22222222-2222-2222-2222-222222222222"))


def _mod() -> Mod:
    return Mod(
        id=_MOD_ID,
        filename="sodium.jar",
        display_name="Sodium",
        description=None,
        loader_type="fabric",
        mod_identifier="sodium",
        provides=[],
        version_number="0.5.3",
        mc_versions=["1.20.4"],
        side="client",
        dependencies=[],
        sha256_hash="abc",
        sha512_hash="def",
        size_bytes=10,
        source="modrinth",
        source_project_id="AABBCCDD",
        source_version_id="VER111",
        uploaded_by=uuid.uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _version() -> CatalogVersion:
    return CatalogVersion(
        version_id="VER111",
        project_id="AABBCCDD",
        name="Sodium 0.5.3",
        version_number="0.5.3",
        filename="sodium.jar",
        download_url="https://cdn/sodium.jar",
        sha512="deadbeef",
        loaders=["fabric"],
        game_versions=["1.20.4"],
        dependencies=[
            CatalogDependency(
                project_id="FABRICAPI", version_id=None, dependency_type="required"
            )
        ],
    )


def _project() -> CatalogProject:
    return CatalogProject(
        project_id="AABBCCDD",
        slug="sodium",
        title="Sodium",
        description="A rendering engine",
        project_type="mod",
        side="client",
        loaders=["fabric"],
        game_versions=["1.20.4"],
        versions=[_version()],
    )


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


class _ProviderStub(FakeCatalogProvider):
    """Catalog provider whose ``search`` / ``get_project`` defer to a recorded stub.

    Lets a route test assert what the endpoint passed into the provider and force
    an error, without standing up full fixture payloads.
    """

    def __init__(
        self,
        *,
        search: _FakeUseCase | None = None,
        get_project: _FakeUseCase | None = None,
    ) -> None:
        super().__init__()
        self._search = search
        self._get_project = get_project

    async def search(self, **kwargs: object) -> CatalogSearchResult:
        assert self._search is not None
        return await self._search(**kwargs)  # type: ignore[return-value]

    async def get_project(self, project_id: str) -> CatalogProject:
        assert self._get_project is not None
        return await self._get_project(project_id=project_id)  # type: ignore[return-value]


def _app(
    *,
    provider: object | None = None,
    import_: _FakeUseCase | None = None,
    recorder: RecordingAuditRecorder | None = None,
    require_upload_perm: bool = True,
) -> object:
    app = create_app()
    user = make_user()
    app.dependency_overrides[get_current_user] = lambda: user
    if require_upload_perm:
        app.dependency_overrides[require_server_update_in_any_community] = lambda: user
    else:
        from fastapi import status

        from mc_server_dashboard_api.http_problem import problem

        async def _deny() -> None:
            raise problem(status.HTTP_403_FORBIDDEN, "forbidden")

        app.dependency_overrides[require_server_update_in_any_community] = _deny
    if provider is not None:
        app.dependency_overrides[get_catalog_provider] = lambda: provider
    if import_ is not None:
        app.dependency_overrides[get_import_mod] = lambda: import_
    if recorder is not None:
        app.dependency_overrides[get_audit_recorder] = lambda: recorder
    return app


class TestCatalogSearchEndpoint:
    def test_search_200(self) -> None:
        result = CatalogSearchResult(
            hits=[
                CatalogSearchHit(
                    project_id="AABBCCDD",
                    slug="sodium",
                    title="Sodium",
                    description="A rendering engine",
                    project_type="mod",
                    side="client",
                    loaders=["fabric"],
                    game_versions=["1.20.4"],
                    downloads=100,
                    icon_url=None,
                )
            ],
            total=1,
        )
        provider = FakeCatalogProvider(results={"sodium": result})
        app = _app(provider=provider)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get("/api/catalog/search", params={"query": "sodium"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["hits"][0]["project_id"] == "AABBCCDD"
        assert body["hits"][0]["side"] == "client"

    def test_search_forwards_facets_and_pagination(self) -> None:
        uc = _FakeUseCase(result=CatalogSearchResult(hits=[], total=0))
        app = _app(provider=_ProviderStub(search=uc))
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get(
                "/api/catalog/search",
                params={
                    "query": "map",
                    "loader": "fabric",
                    "game_version": "1.20.4",
                    "limit": 5,
                    "offset": 10,
                },
            )
        assert resp.status_code == 200
        assert uc.calls[0] == {
            "query": "map",
            "loader": "fabric",
            "game_version": "1.20.4",
            "limit": 5,
            "offset": 10,
        }

    def test_search_source_down_502(self) -> None:
        uc = _FakeUseCase(error=CatalogUnavailableError("down"))
        app = _app(provider=_ProviderStub(search=uc))
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get("/api/catalog/search", params={"query": "x"})
        assert resp.status_code == 502

    def test_search_requires_query(self) -> None:
        app = _app(provider=FakeCatalogProvider())
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get("/api/catalog/search")
        assert resp.status_code == 422


class TestCatalogProjectEndpoint:
    def test_project_200(self) -> None:
        provider = FakeCatalogProvider(projects={"sodium": _project()})
        app = _app(provider=provider)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get("/api/catalog/projects/sodium")
        assert resp.status_code == 200
        body = resp.json()
        assert body["project_id"] == "AABBCCDD"
        assert body["side"] == "client"
        assert len(body["versions"]) == 1
        ver = body["versions"][0]
        assert ver["version_id"] == "VER111"
        assert ver["sha512"] == "deadbeef"
        assert ver["dependencies"][0]["project_id"] == "FABRICAPI"

    def test_project_not_found_404(self) -> None:
        uc = _FakeUseCase(error=CatalogProjectNotFoundError("missing"))
        app = _app(provider=_ProviderStub(get_project=uc))
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get("/api/catalog/projects/missing")
        assert resp.status_code == 404

    def test_project_source_down_502(self) -> None:
        uc = _FakeUseCase(error=CatalogUnavailableError("down"))
        app = _app(provider=_ProviderStub(get_project=uc))
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.get("/api/catalog/projects/x")
        assert resp.status_code == 502


class TestImportEndpoint:
    def test_import_201(self) -> None:
        m = _mod()
        uc = _FakeUseCase(result=m)
        recorder = RecordingAuditRecorder()
        app = _app(import_=uc, recorder=recorder)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(
                "/api/mods/import",
                json={"project_id": "AABBCCDD", "version_id": "VER111"},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["id"] == str(m.id.value)
        assert body["source"] == "modrinth"
        assert len(recorder.events) == 1
        assert recorder.events[0].operation == ops.MOD_IMPORT
        assert recorder.events[0].outcome == Outcome.SUCCESS

    def test_import_forwards_side_override(self) -> None:
        uc = _FakeUseCase(result=_mod())
        app = _app(import_=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(
                "/api/mods/import",
                json={
                    "project_id": "AABBCCDD",
                    "version_id": "VER111",
                    "side": "both",
                },
            )
        assert resp.status_code == 201
        assert uc.calls[0]["side"] == "both"

    def test_import_requires_upload_permission_403(self) -> None:
        uc = _FakeUseCase(result=_mod())
        app = _app(import_=uc, require_upload_perm=False)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(
                "/api/mods/import",
                json={"project_id": "AABBCCDD", "version_id": "VER111"},
            )
        assert resp.status_code == 403

    def test_import_version_not_found_404(self) -> None:
        uc = _FakeUseCase(error=CatalogProjectNotFoundError("missing"))
        app = _app(import_=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(
                "/api/mods/import",
                json={"project_id": "AABBCCDD", "version_id": "MISSING"},
            )
        assert resp.status_code == 404

    def test_import_invalid_jar_422(self) -> None:
        uc = _FakeUseCase(error=InvalidModJarError("bad"))
        app = _app(import_=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(
                "/api/mods/import",
                json={"project_id": "AABBCCDD", "version_id": "VER111"},
            )
        assert resp.status_code == 422

    def test_import_too_large_413(self) -> None:
        uc = _FakeUseCase(error=FileTooLargeError("big"))
        app = _app(import_=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(
                "/api/mods/import",
                json={"project_id": "AABBCCDD", "version_id": "VER111"},
            )
        assert resp.status_code == 413

    def test_import_integrity_mismatch_502(self) -> None:
        uc = _FakeUseCase(error=ModIntegrityError("VER111"))
        app = _app(import_=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(
                "/api/mods/import",
                json={"project_id": "AABBCCDD", "version_id": "VER111"},
            )
        assert resp.status_code == 502

    def test_import_source_down_502(self) -> None:
        uc = _FakeUseCase(error=CatalogUnavailableError("down"))
        app = _app(import_=uc)
        with TestClient(app) as client:  # type: ignore[arg-type]
            resp = client.post(
                "/api/mods/import",
                json={"project_id": "AABBCCDD", "version_id": "VER111"},
            )
        assert resp.status_code == 502
