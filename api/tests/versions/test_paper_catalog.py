"""Paper catalog adapter against recorded PaperMC v2 API fixtures (offline)."""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.versions.adapters.paper import _BASE, PaperCatalog
from mc_server_dashboard_api.versions.domain.errors import UnknownVersionError
from mc_server_dashboard_api.versions.domain.value_objects import (
    HashAlgorithm,
    ServerType,
)
from tests.versions.fakes import FakeJsonFetcher

_PROJECT = {"versions": ["1.20.4", "1.21.1"]}
_VERSION = {"builds": [100, 196, 42]}
_BUILD = {
    "downloads": {
        "application": {
            "name": "paper-1.21.1-196.jar",
            "sha256": "b" * 64,
        }
    }
}


def _catalog() -> tuple[PaperCatalog, FakeJsonFetcher]:
    fetcher = FakeJsonFetcher(
        {
            _BASE: _PROJECT,
            f"{_BASE}/versions/1.21.1": _VERSION,
            f"{_BASE}/versions/1.21.1/builds/196": _BUILD,
        }
    )
    return PaperCatalog(fetcher=fetcher), fetcher


@pytest.mark.asyncio
async def test_lists_versions_newest_first() -> None:
    catalog, _ = _catalog()
    versions = await catalog.list_versions(ServerType.PAPER)
    assert [v.version for v in versions] == ["1.21.1", "1.20.4"]


@pytest.mark.asyncio
async def test_resolves_newest_build_with_sha256() -> None:
    catalog, _ = _catalog()
    source = await catalog.resolve(ServerType.PAPER, "1.21.1")
    assert source.url == (
        f"{_BASE}/versions/1.21.1/builds/196/downloads/paper-1.21.1-196.jar"
    )
    assert source.expected_hash == "b" * 64
    assert source.hash_algorithm is HashAlgorithm.SHA256


@pytest.mark.asyncio
async def test_resolve_url_encodes_version_path_segment() -> None:
    """Version strings with path-traversal or query chars must be percent-encoded."""
    malicious = "1.21.1/../admin?x=1"
    encoded = "1.21.1%2F..%2Fadmin%3Fx%3D1"
    build_detail = {
        "downloads": {"application": {"name": "paper.jar", "sha256": "c" * 64}}
    }
    fetcher = FakeJsonFetcher(
        {
            f"{_BASE}/versions/{encoded}": {"builds": [1]},
            f"{_BASE}/versions/{encoded}/builds/1": build_detail,
        }
    )
    catalog = PaperCatalog(fetcher=fetcher)
    source = await catalog.resolve(ServerType.PAPER, malicious)
    # The fetcher must have received percent-encoded URLs, not raw path segments.
    assert fetcher.calls == [
        f"{_BASE}/versions/{encoded}",
        f"{_BASE}/versions/{encoded}/builds/1",
    ]
    assert source.url == f"{_BASE}/versions/{encoded}/builds/1/downloads/paper.jar"


@pytest.mark.asyncio
async def test_vanilla_request_rejected() -> None:
    catalog, _ = _catalog()
    with pytest.raises(UnknownVersionError):
        await catalog.resolve(ServerType.VANILLA, "1.21.1")
