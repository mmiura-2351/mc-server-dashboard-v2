"""Catalog-backed create-path version validation (FR-VER-1).

The servers ``VersionValidator`` maps the persisted ``server_type`` onto the
version catalog. Types the catalog cannot resolve are rejected at create-time:
``forge`` as unsupported (worker installer step needed), ``spigot`` with a
distinct error recommending Paper (no official distribution API). A catalogued
type whose version is not offered is the unknown-version case.
"""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.servers.adapters.version_validator import (
    CatalogVersionValidator,
)
from mc_server_dashboard_api.servers.domain.version_validator import (
    SpigotUnsupportedError,
    UnknownVersionError,
    UnsupportedServerTypeError,
)
from mc_server_dashboard_api.versions.adapters.composite import CompositeCatalog
from mc_server_dashboard_api.versions.adapters.vanilla import (
    _MANIFEST_URL,
    VanillaCatalog,
)
from mc_server_dashboard_api.versions.domain.value_objects import ServerType
from tests.versions.fakes import FakeJsonFetcher

_VERSION_URL = "https://example.test/1.21.1.json"
_MANIFEST = {
    "versions": [{"id": "1.21.1", "type": "release", "url": _VERSION_URL}],
}


def _validator() -> CatalogVersionValidator:
    fetcher = FakeJsonFetcher({_MANIFEST_URL: _MANIFEST})
    catalog = CompositeCatalog(
        by_type={ServerType.VANILLA: VanillaCatalog(fetcher=fetcher)}
    )
    return CatalogVersionValidator(catalog=catalog)


@pytest.mark.asyncio
async def test_accepts_offered_version() -> None:
    await _validator().validate(server_type="vanilla", version="1.21.1")


@pytest.mark.asyncio
async def test_unknown_version_rejected() -> None:
    with pytest.raises(UnknownVersionError):
        await _validator().validate(server_type="vanilla", version="9.9.9")


@pytest.mark.asyncio
async def test_forge_rejected_as_unsupported() -> None:
    with pytest.raises(UnsupportedServerTypeError):
        await _validator().validate(server_type="forge", version="1.21.1")


@pytest.mark.asyncio
async def test_spigot_rejected_recommending_paper() -> None:
    with pytest.raises(SpigotUnsupportedError) as exc:
        await _validator().validate(server_type="spigot", version="1.21.1")
    assert "paper" in str(exc.value).lower()
