"""Forge catalog adapter against recorded files/maven.minecraftforge.net fixtures.

Offline, deterministic (TESTING.md Section 4, the issue's NO-live-network rule):
the fakes serve recorded fixtures keyed by URL. The pinned upstream shapes
(verified empirically with curl, issue #307):

- ``maven.minecraftforge.net/.../forge/maven-metadata.xml`` lists every
  ``<mcversion>-<forgeversion>`` pair under ``<versioning><versions>``.
- ``files.minecraftforge.net/.../forge/promotions_slim.json`` marks the
  ``<mc>-recommended`` / ``<mc>-latest`` build per MC version; the value is the
  *forge-version* segment only (e.g. ``"58.1.0"``), so the full version is
  ``<mc>-<value>``.
- the installer JAR is ``.../forge/<v>/forge-<v>-installer.jar`` with a sibling
  ``.sha1`` whose body is the bare lowercase-hex SHA-1.
"""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.versions.adapters.forge import (
    _METADATA_URL,
    _PROMOTIONS_URL,
    ForgeCatalog,
    _installer_sha1_url,
    _installer_url,
)
from mc_server_dashboard_api.versions.domain.errors import UnknownVersionError
from mc_server_dashboard_api.versions.domain.value_objects import (
    HashAlgorithm,
    ServerType,
)
from tests.versions.fakes import FakeDocumentFetcher

# maven-metadata.xml lists versions oldest-first within each MC line, interleaved
# across MC versions (the real document is not globally sorted). Each entry is
# ``<mcversion>-<forgeversion>``.
_METADATA_XML = """<?xml version="1.0" encoding="UTF-8"?>
<metadata>
  <groupId>net.minecraftforge</groupId>
  <artifactId>forge</artifactId>
  <versioning>
    <latest>1.21.8-58.1.18</latest>
    <release>1.21.8-58.1.18</release>
    <versions>
      <version>1.20.1-47.4.0</version>
      <version>1.20.1-47.4.10</version>
      <version>1.21.1-52.1.0</version>
      <version>1.21.8-58.1.0</version>
      <version>1.21.8-58.1.18</version>
    </versions>
    <lastUpdated>20260527161535</lastUpdated>
  </versioning>
</metadata>
"""

# promotions_slim.json: keys ``<mc>-latest`` / ``<mc>-recommended``; the value is
# the forge-version segment only.
_PROMOTIONS = {
    "homepage": "https://files.minecraftforge.net/net/minecraftforge/forge/",
    "promos": {
        "1.20.1-latest": "47.4.10",
        "1.20.1-recommended": "47.4.0",
        "1.21.1-latest": "52.1.0",
        # 1.21.1 has no recommended build (latest-only line).
        "1.21.8-latest": "58.1.18",
        "1.21.8-recommended": "58.1.0",
    },
}

_RECOMMENDED_SHA1 = "a" * 40  # forge-1.21.8-58.1.0-installer.jar.sha1


def _catalog(fail: bool = False) -> tuple[ForgeCatalog, FakeDocumentFetcher]:
    sha1_url = _installer_sha1_url("1.21.8-58.1.0")
    fetcher = FakeDocumentFetcher(
        texts={_METADATA_URL: _METADATA_XML, sha1_url: f"{_RECOMMENDED_SHA1}\n"},
        payloads={_PROMOTIONS_URL: _PROMOTIONS},
        fail=fail,
    )
    return ForgeCatalog(fetcher=fetcher), fetcher


@pytest.mark.asyncio
async def test_lists_distinct_mc_versions_newest_first() -> None:
    catalog, _ = _catalog()
    versions = await catalog.list_versions(ServerType.FORGE)
    assert [v.version for v in versions] == ["1.21.8", "1.21.1", "1.20.1"]
    assert all(v.server_type is ServerType.FORGE for v in versions)


@pytest.mark.asyncio
async def test_resolves_recommended_build_with_sha1() -> None:
    catalog, _ = _catalog()
    source = await catalog.resolve(ServerType.FORGE, "1.21.8")
    # Recommended (58.1.0) wins over latest (58.1.18).
    assert source.url == _installer_url("1.21.8-58.1.0")
    assert source.expected_hash == _RECOMMENDED_SHA1
    assert source.hash_algorithm is HashAlgorithm.SHA1
    assert source.version == "1.21.8"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_latest_when_no_recommended() -> None:
    catalog, fetcher = _catalog()
    # 1.21.1 is latest-only; the installer sha1 fixture must exist for its latest.
    fetcher.texts[_installer_sha1_url("1.21.1-52.1.0")] = "b" * 40
    source = await catalog.resolve(ServerType.FORGE, "1.21.1")
    assert source.url == _installer_url("1.21.1-52.1.0")
    assert source.expected_hash == "b" * 40


@pytest.mark.asyncio
async def test_resolve_unknown_mc_version_raises() -> None:
    catalog, _ = _catalog()
    with pytest.raises(UnknownVersionError):
        await catalog.resolve(ServerType.FORGE, "9.9.9")


@pytest.mark.asyncio
async def test_non_forge_request_rejected() -> None:
    catalog, _ = _catalog()
    with pytest.raises(UnknownVersionError):
        await catalog.list_versions(ServerType.VANILLA)
