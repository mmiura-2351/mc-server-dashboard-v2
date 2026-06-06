"""Real-stack source-down behaviour: typed errors, never a bare 500 (FR-VER-2).

These tests drive the REAL :class:`RetryCachingFetcher` + real catalog adapter over
a failing fetcher (a cold cache plus a down source) — no fake raising
``CatalogUnavailableError`` directly. They assert every consumer of the catalog
boundary sees a typed error rather than a bare ``FetchError`` leaking out as a 500:

- the ``GET /versions/{type}`` endpoint -> 503 ``catalog_unavailable``
- the start path's ``JarProvisioner`` -> ``JarProvisioningError`` (-> 503
  ``jar_unavailable`` at the edge)
- the create path's ``VersionValidator`` -> ``CatalogUnavailableError`` (-> 503
  ``catalog_unavailable`` at the edge)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.dependencies import (
    get_current_user,
    get_version_catalog,
)
from mc_server_dashboard_api.identity.domain.entities import User
from mc_server_dashboard_api.servers.adapters.jar_provisioner import (
    CatalogJarProvisioner,
)
from mc_server_dashboard_api.servers.adapters.version_validator import (
    CatalogVersionValidator,
)
from mc_server_dashboard_api.servers.domain.jar_provisioner import JarProvisioningError
from mc_server_dashboard_api.servers.domain.version_validator import (
    CatalogUnavailableError as ServersCatalogUnavailableError,
)
from mc_server_dashboard_api.versions.adapters.composite import CompositeCatalog
from mc_server_dashboard_api.versions.adapters.retry_cache import RetryCachingFetcher
from mc_server_dashboard_api.versions.adapters.vanilla import VanillaCatalog
from mc_server_dashboard_api.versions.application.ensure_jar import EnsureJar
from mc_server_dashboard_api.versions.domain.value_objects import ServerType
from tests.identity.fakes import make_user
from tests.versions.fakes import FakeJarPool, FakeJsonFetcher


async def _no_sleep(_: float) -> None:
    return None


def _down_catalog() -> CompositeCatalog:
    """A real catalog whose real retry+cache wrapper sits over a down source."""

    # ``fail=True`` makes every upstream fetch raise FetchError, so with an empty
    # cache the real RetryCachingFetcher exhausts its budget and must translate the
    # failure at its choke point.
    inner = FakeJsonFetcher({}, fail=True)
    fetcher = RetryCachingFetcher(inner=inner, attempts=2, sleep=_no_sleep)
    return CompositeCatalog(
        by_type={ServerType.VANILLA: VanillaCatalog(fetcher=fetcher)}
    )


def _fake_user() -> User:
    return make_user()


def _client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_version_catalog] = _down_catalog
    app.dependency_overrides[get_current_user] = _fake_user
    return TestClient(app)


def test_versions_endpoint_is_503_when_source_down_cold_cache() -> None:
    client = _client()
    with client:
        resp = client.get("/versions/vanilla")
    assert resp.status_code == 503
    assert resp.json()["reason"] == "catalog_unavailable"


@pytest.mark.asyncio
async def test_start_jar_provisioner_raises_typed_error_when_source_down() -> None:
    from mc_server_dashboard_api.versions.adapters.http_jar_fetcher import (
        HttpxJarFetcher,
    )

    ensure = EnsureJar(
        catalog=_down_catalog(), fetcher=HttpxJarFetcher(), pool=FakeJarPool()
    )
    provisioner = CatalogJarProvisioner(ensure_jar=ensure)
    with pytest.raises(JarProvisioningError):
        await provisioner.ensure(
            server_type="vanilla", version="1.21.1", known_key=None
        )


@pytest.mark.asyncio
async def test_create_validator_raises_typed_error_when_source_down() -> None:
    validator = CatalogVersionValidator(catalog=_down_catalog())
    with pytest.raises(ServersCatalogUnavailableError):
        await validator.validate(server_type="vanilla", version="1.21.1")
