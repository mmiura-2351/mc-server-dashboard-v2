"""Endpoint tests for the global version catalog (FR-VER-1).

Exercised in-process via TestClient with the catalog overridden by a fake (no
network). Covers: auth requirement, the types index, a version listing, the
unknown-type 404 (spigot), and the source-down 503.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.dependencies import (
    get_current_user,
    get_version_catalog,
)
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    UserId,
    Username,
)
from mc_server_dashboard_api.versions.domain.catalog import VersionCatalog
from mc_server_dashboard_api.versions.domain.errors import CatalogUnavailableError
from mc_server_dashboard_api.versions.domain.value_objects import (
    JarSource,
    ServerType,
    VersionRef,
)


class _FakeCatalog(VersionCatalog):
    def __init__(self, *, down: bool = False) -> None:
        self.down = down

    async def list_versions(self, server_type: ServerType) -> list[VersionRef]:
        if self.down:
            raise CatalogUnavailableError("source down")
        return [VersionRef(server_type=server_type, version="1.21.1")]

    async def resolve(self, server_type: ServerType, version: str) -> JarSource:
        raise NotImplementedError


def _fake_user() -> User:
    import datetime as dt
    import uuid

    return User(
        id=UserId(uuid.uuid4()),
        username=Username("tester"),
        email=EmailAddress("tester@example.test"),
        password_hash="x",
        is_platform_admin=False,
        created_at=dt.datetime.now(dt.UTC),
        updated_at=dt.datetime.now(dt.UTC),
    )


def _client(catalog: VersionCatalog, *, authed: bool = True) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_version_catalog] = lambda: catalog
    if authed:
        app.dependency_overrides[get_current_user] = _fake_user
    return TestClient(app)


def test_requires_authentication() -> None:
    client = _client(_FakeCatalog(), authed=False)
    with client:
        resp = client.get("/versions/vanilla")
    assert resp.status_code == 401


def test_lists_server_types() -> None:
    client = _client(_FakeCatalog())
    with client:
        resp = client.get("/versions")
    assert resp.status_code == 200
    assert resp.json() == {"server_types": ["vanilla", "paper", "fabric", "forge"]}


def test_lists_versions_for_type() -> None:
    client = _client(_FakeCatalog())
    with client:
        resp = client.get("/versions/vanilla")
    assert resp.status_code == 200
    assert resp.json() == {"versions": ["1.21.1"]}


def test_lists_versions_for_forge() -> None:
    client = _client(_FakeCatalog())
    with client:
        resp = client.get("/versions/forge")
    assert resp.status_code == 200
    assert resp.json() == {"versions": ["1.21.1"]}


def test_unknown_type_spigot_is_404() -> None:
    client = _client(_FakeCatalog())
    with client:
        resp = client.get("/versions/spigot")
    assert resp.status_code == 404
    assert resp.json()["reason"] == "unknown_server_type"


def test_source_down_is_503() -> None:
    client = _client(_FakeCatalog(down=True))
    with client:
        resp = client.get("/versions/vanilla")
    assert resp.status_code == 503
    assert resp.json()["reason"] == "catalog_unavailable"
