"""Fabric catalog adapter against recorded meta.fabricmc.net fixtures (offline).

The fabric server launcher JAR is generated dynamically by the meta API at
``/v2/versions/loader/{game}/{loader}/{installer}/server/jar`` (verified
empirically: 200, ``content-type: application/java-archive``). The meta API does
*not* publish a checksum for that generated JAR, so the resolved
:class:`JarSource` carries no expected hash — distinct from vanilla/paper, which
both publish one.
"""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.versions.adapters.fabric import (
    _GAME_URL,
    _INSTALLER_URL,
    _LOADER_URL,
    FabricCatalog,
    _loader_for_game_url,
    _server_jar_url,
)
from mc_server_dashboard_api.versions.domain.errors import UnknownVersionError
from mc_server_dashboard_api.versions.domain.value_objects import ServerType
from tests.versions.fakes import FakeJsonFetcher

# meta.fabricmc.net lists game versions newest-first, with a stability flag.
_GAME = [
    {"version": "1.21.1", "stable": True},
    {"version": "1.21", "stable": True},
    {"version": "24w14a", "stable": False},
]
# Loader list newest-first, with a stability flag.
_LOADER = [
    {"version": "0.16.5", "stable": True},
    {"version": "0.16.4", "stable": False},
]
# Per-game loader list (the resolve hop) confirms the game version is offered.
_LOADER_FOR_GAME = [
    {"loader": {"version": "0.16.5", "stable": True}},
]
# Installer list newest-first, with a stability flag.
_INSTALLER = [
    {"version": "1.0.1", "stable": True},
    {"version": "1.0.0", "stable": False},
]


def _catalog(fail: bool = False) -> tuple[FabricCatalog, FakeJsonFetcher]:
    fetcher = FakeJsonFetcher(
        {
            _GAME_URL: _GAME,
            _LOADER_URL: _LOADER,
            _INSTALLER_URL: _INSTALLER,
            _loader_for_game_url("1.21.1"): _LOADER_FOR_GAME,
        },
        fail=fail,
    )
    return FabricCatalog(fetcher=fetcher), fetcher


@pytest.mark.asyncio
async def test_lists_only_stable_game_versions_newest_first() -> None:
    catalog, _ = _catalog()
    versions = await catalog.list_versions(ServerType.FABRIC)
    assert [v.version for v in versions] == ["1.21.1", "1.21"]


@pytest.mark.asyncio
async def test_resolves_server_jar_with_newest_stable_loader_and_installer() -> None:
    catalog, _ = _catalog()
    source = await catalog.resolve(ServerType.FABRIC, "1.21.1")
    assert source.url == _server_jar_url("1.21.1", "0.16.5", "1.0.1")
    # The fabric meta API publishes no checksum for the generated launcher JAR.
    assert source.expected_hash is None
    assert source.hash_algorithm is None


@pytest.mark.asyncio
async def test_resolve_unknown_game_version_raises() -> None:
    catalog, fetcher = _catalog()
    fetcher._payloads[_loader_for_game_url("9.9.9")] = []
    with pytest.raises(UnknownVersionError):
        await catalog.resolve(ServerType.FABRIC, "9.9.9")


@pytest.mark.asyncio
async def test_resolve_raises_unknown_when_loader_for_game_404s() -> None:
    """FetchNotFoundError should surface as UnknownVersionError (#1941)."""
    fetcher = FakeJsonFetcher(
        {
            _GAME_URL: _GAME,
            _LOADER_URL: _LOADER,
            _INSTALLER_URL: _INSTALLER,
        },
        not_found_urls={_loader_for_game_url("1.21.1")},
    )
    catalog = FabricCatalog(fetcher=fetcher)
    with pytest.raises(UnknownVersionError):
        await catalog.resolve(ServerType.FABRIC, "1.21.1")


@pytest.mark.asyncio
async def test_non_fabric_request_rejected() -> None:
    catalog, _ = _catalog()
    with pytest.raises(UnknownVersionError):
        await catalog.list_versions(ServerType.VANILLA)
