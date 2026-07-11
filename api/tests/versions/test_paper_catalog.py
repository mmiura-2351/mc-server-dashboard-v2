"""Paper catalog adapter against recorded PaperMC v3 Fill API fixtures (offline)."""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.versions.adapters.paper import _BASE, PaperCatalog
from mc_server_dashboard_api.versions.domain.errors import UnknownVersionError
from mc_server_dashboard_api.versions.domain.value_objects import (
    HashAlgorithm,
    ServerType,
)
from tests.versions.fakes import FakeJsonFetcher

_VERSIONS = {
    "versions": [
        {"version": {"id": "1.21.4"}},
        {"version": {"id": "1.20.4"}},
    ]
}
_DOWNLOAD_URL = (
    "https://fill-data.papermc.io/v1/objects/" + "b" * 64 + "/paper-1.21.4-232.jar"
)
_BUILD = {
    "id": 232,
    "channel": "STABLE",
    "downloads": {
        "server:default": {
            "name": "paper-1.21.4-232.jar",
            "checksums": {"sha256": "b" * 64},
            "size": 51437498,
            "url": _DOWNLOAD_URL,
        }
    },
}


def _catalog() -> tuple[PaperCatalog, FakeJsonFetcher]:
    fetcher = FakeJsonFetcher(
        {
            f"{_BASE}/versions": _VERSIONS,
            f"{_BASE}/versions/1.21.4/builds/latest": _BUILD,
        }
    )
    return PaperCatalog(fetcher=fetcher), fetcher


@pytest.mark.asyncio
async def test_lists_versions_newest_first() -> None:
    catalog, _ = _catalog()
    versions = await catalog.list_versions(ServerType.PAPER)
    # The Fill /versions array is newest-first already, so it is presented as-is.
    assert [v.version for v in versions] == ["1.21.4", "1.20.4"]


@pytest.mark.asyncio
async def test_resolves_latest_build_with_sha256() -> None:
    catalog, _ = _catalog()
    source = await catalog.resolve(ServerType.PAPER, "1.21.4")
    # The download URL is served verbatim from fill-data.papermc.io, not rebuilt.
    assert source.url == _DOWNLOAD_URL
    assert source.expected_hash == "b" * 64
    assert source.hash_algorithm is HashAlgorithm.SHA256


@pytest.mark.asyncio
async def test_resolve_url_encodes_version_path_segment() -> None:
    """Version strings with path-traversal or query chars must be percent-encoded."""
    malicious = "1.21.1/../admin?x=1"
    encoded = "1.21.1%2F..%2Fadmin%3Fx%3D1"
    build = {
        "downloads": {
            "server:default": {
                "name": "paper.jar",
                "checksums": {"sha256": "c" * 64},
                "url": "https://fill-data.papermc.io/v1/objects/c/paper.jar",
            }
        }
    }
    fetcher = FakeJsonFetcher({f"{_BASE}/versions/{encoded}/builds/latest": build})
    catalog = PaperCatalog(fetcher=fetcher)
    source = await catalog.resolve(ServerType.PAPER, malicious)
    # The fetcher must have received a percent-encoded URL, not a raw path segment.
    assert fetcher.calls == [f"{_BASE}/versions/{encoded}/builds/latest"]
    assert source.url == "https://fill-data.papermc.io/v1/objects/c/paper.jar"


@pytest.mark.asyncio
async def test_resolve_raises_when_no_server_default_download() -> None:
    build = {"id": 1, "channel": "STABLE", "downloads": {}}
    fetcher = FakeJsonFetcher({f"{_BASE}/versions/1.21.4/builds/latest": build})
    catalog = PaperCatalog(fetcher=fetcher)
    with pytest.raises(UnknownVersionError):
        await catalog.resolve(ServerType.PAPER, "1.21.4")


@pytest.mark.asyncio
async def test_resolve_raises_when_server_default_missing_url() -> None:
    """Guard: server:default present but without a 'url' key."""
    build = {
        "downloads": {
            "server:default": {
                "name": "paper.jar",
                "checksums": {"sha256": "a" * 64},
            }
        }
    }
    fetcher = FakeJsonFetcher({f"{_BASE}/versions/1.21.4/builds/latest": build})
    catalog = PaperCatalog(fetcher=fetcher)
    with pytest.raises(UnknownVersionError):
        await catalog.resolve(ServerType.PAPER, "1.21.4")


@pytest.mark.asyncio
async def test_resolve_raises_when_checksums_missing() -> None:
    """Guard: server:default has url but no 'checksums' key."""
    build = {
        "downloads": {
            "server:default": {
                "name": "paper.jar",
                "url": "https://fill-data.papermc.io/v1/objects/a/paper.jar",
            }
        }
    }
    fetcher = FakeJsonFetcher({f"{_BASE}/versions/1.21.4/builds/latest": build})
    catalog = PaperCatalog(fetcher=fetcher)
    with pytest.raises(UnknownVersionError):
        await catalog.resolve(ServerType.PAPER, "1.21.4")


@pytest.mark.asyncio
async def test_resolve_raises_when_checksums_missing_sha256() -> None:
    """Guard: checksums is a dict but lacks 'sha256'."""
    build = {
        "downloads": {
            "server:default": {
                "name": "paper.jar",
                "checksums": {"md5": "d" * 32},
                "url": "https://fill-data.papermc.io/v1/objects/a/paper.jar",
            }
        }
    }
    fetcher = FakeJsonFetcher({f"{_BASE}/versions/1.21.4/builds/latest": build})
    catalog = PaperCatalog(fetcher=fetcher)
    with pytest.raises(UnknownVersionError):
        await catalog.resolve(ServerType.PAPER, "1.21.4")


@pytest.mark.asyncio
async def test_resolve_unknown_version_raises_on_upstream_404() -> None:
    """An upstream 404 for a nonexistent version is UnknownVersionError, not
    CatalogUnavailableError (#1539)."""

    not_found_url = f"{_BASE}/versions/9.9.9/builds/latest"
    fetcher = FakeJsonFetcher({}, not_found_urls={not_found_url})
    catalog = PaperCatalog(fetcher=fetcher)
    with pytest.raises(UnknownVersionError):
        await catalog.resolve(ServerType.PAPER, "9.9.9")


@pytest.mark.asyncio
async def test_vanilla_request_rejected() -> None:
    catalog, _ = _catalog()
    with pytest.raises(UnknownVersionError):
        await catalog.resolve(ServerType.VANILLA, "1.21.1")
