"""Vanilla catalog adapter against recorded Mojang-manifest fixtures (offline)."""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.versions.adapters.vanilla import (
    _MANIFEST_URL,
    VanillaCatalog,
)
from mc_server_dashboard_api.versions.domain.errors import UnknownVersionError
from mc_server_dashboard_api.versions.domain.value_objects import (
    HashAlgorithm,
    ServerType,
)
from tests.versions.fakes import FakeJsonFetcher

_VERSION_URL = "https://example.test/1.21.1.json"

_MANIFEST = {
    "latest": {"release": "1.21.1"},
    "versions": [
        {"id": "1.21.1", "type": "release", "url": _VERSION_URL},
        {"id": "24w14a", "type": "snapshot", "url": "https://example.test/snap.json"},
    ],
}

_DETAIL = {
    "downloads": {
        "server": {
            "sha1": "a" * 40,
            "url": "https://example.test/server.jar",
        }
    }
}


def _catalog(fail: bool = False) -> tuple[VanillaCatalog, FakeJsonFetcher]:
    fetcher = FakeJsonFetcher(
        {_MANIFEST_URL: _MANIFEST, _VERSION_URL: _DETAIL}, fail=fail
    )
    return VanillaCatalog(fetcher=fetcher), fetcher


@pytest.mark.asyncio
async def test_lists_only_release_versions() -> None:
    catalog, _ = _catalog()
    versions = await catalog.list_versions(ServerType.VANILLA)
    assert [v.version for v in versions] == ["1.21.1"]


@pytest.mark.asyncio
async def test_resolves_server_jar_with_sha1() -> None:
    catalog, _ = _catalog()
    source = await catalog.resolve(ServerType.VANILLA, "1.21.1")
    assert source.url == "https://example.test/server.jar"
    assert source.expected_hash == "a" * 40
    assert source.hash_algorithm is HashAlgorithm.SHA1


@pytest.mark.asyncio
async def test_resolve_unknown_version_raises() -> None:
    catalog, _ = _catalog()
    with pytest.raises(UnknownVersionError):
        await catalog.resolve(ServerType.VANILLA, "9.9.9")


@pytest.mark.asyncio
async def test_paper_request_rejected() -> None:
    catalog, _ = _catalog()
    with pytest.raises(UnknownVersionError):
        await catalog.list_versions(ServerType.PAPER)
