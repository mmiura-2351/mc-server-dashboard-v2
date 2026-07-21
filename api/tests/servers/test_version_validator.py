"""Catalog-backed create-path version validation (FR-VER-1).

The servers ``VersionValidator`` maps the persisted ``server_type`` onto the
version catalog. ``vanilla`` / ``paper`` / ``fabric`` / ``forge`` are catalogued
and validated against the catalog. A catalogued type whose version is not
offered is the unknown-version case.
"""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.servers.adapters.version_validator import (
    CatalogVersionValidator,
)
from mc_server_dashboard_api.servers.domain.version_validator import (
    CatalogUnavailableError,
    UnknownVersionError,
)
from mc_server_dashboard_api.versions.adapters.composite import CompositeCatalog
from mc_server_dashboard_api.versions.adapters.forge import (
    _METADATA_URL,
    ForgeCatalog,
)
from mc_server_dashboard_api.versions.adapters.vanilla import (
    _MANIFEST_URL,
    VanillaCatalog,
)
from mc_server_dashboard_api.versions.domain.value_objects import ServerType
from tests.versions.fakes import FakeDocumentFetcher, FakeJsonFetcher

_VERSION_URL = "https://example.test/1.21.1.json"
_MANIFEST = {
    "versions": [{"id": "1.21.1", "type": "release", "url": _VERSION_URL}],
}
_FORGE_METADATA = """<?xml version="1.0" encoding="UTF-8"?>
<metadata><versioning><versions>
  <version>1.21.8-58.1.0</version>
</versions></versioning></metadata>
"""


def _validator() -> CatalogVersionValidator:
    catalog = CompositeCatalog(
        by_type={
            ServerType.VANILLA: VanillaCatalog(
                fetcher=FakeJsonFetcher({_MANIFEST_URL: _MANIFEST})
            ),
            ServerType.FORGE: ForgeCatalog(
                fetcher=FakeDocumentFetcher(
                    texts={_METADATA_URL: _FORGE_METADATA}, payloads={}
                )
            ),
        }
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
async def test_accepts_offered_forge_version() -> None:
    await _validator().validate(server_type="forge", version="1.21.8")


@pytest.mark.asyncio
async def test_unknown_forge_version_rejected() -> None:
    with pytest.raises(UnknownVersionError):
        await _validator().validate(server_type="forge", version="9.9.9")


@pytest.mark.asyncio
async def test_malformed_catalog_payload_raises_catalog_unavailable() -> None:
    """Versions-domain UnknownVersionError from a malformed upstream body
    translates to the servers-domain CatalogUnavailableError (issue #1991)."""
    catalog = CompositeCatalog(
        by_type={
            ServerType.FORGE: ForgeCatalog(
                fetcher=FakeDocumentFetcher(
                    texts={_METADATA_URL: "not xml at all"},
                    payloads={},
                )
            ),
        }
    )
    validator = CatalogVersionValidator(catalog=catalog)
    with pytest.raises(CatalogUnavailableError):
        await validator.validate(server_type="forge", version="1.21.8")
