"""Tests for :class:`RoutingCatalog` (issue #1905).

The router must keep every non-Floodgate project on the default (Modrinth)
catalog, route ``floodgate`` metadata and GeyserMC-host downloads to the GeyserMC
adapter, and merge the Floodgate search hit ahead of the default hits.
"""

from __future__ import annotations

from typing import Any

from mc_server_dashboard_api.servers.adapters.catalog_router import RoutingCatalog
from mc_server_dashboard_api.servers.adapters.geysermc_catalog import GeyserMcCatalog
from mc_server_dashboard_api.servers.domain.catalog_provider import (
    CatalogProject,
    CatalogProvider,
    CatalogSearchResponse,
    CatalogSearchResult,
    CatalogVersion,
)


class _RecordingCatalog(CatalogProvider):
    """Default-catalog stand-in that records which calls it received."""

    def __init__(self) -> None:
        self.project_calls: list[str] = []
        self.version_calls: list[str] = []
        self.download_calls: list[str] = []

    async def search(
        self,
        *,
        query: str,
        loader: str,
        game_versions: list[str],
        limit: int = 20,
        offset: int = 0,
    ) -> CatalogSearchResponse:
        hit = CatalogSearchResult(
            project_id="sodium",
            slug="sodium",
            title="Sodium",
            description="",
            author="",
            icon_url=None,
            downloads=5,
            categories=[],
            latest_game_versions=[],
        )
        return CatalogSearchResponse(
            hits=[hit], total_hits=1, offset=offset, limit=limit
        )

    async def get_project(self, project_id_or_slug: str) -> CatalogProject:
        self.project_calls.append(project_id_or_slug)
        return CatalogProject(
            project_id=project_id_or_slug,
            slug=project_id_or_slug,
            title="",
            description="",
            body="",
            author=None,
            icon_url=None,
            downloads=0,
            categories=[],
            game_versions=[],
            loaders=[],
        )

    async def list_versions(
        self,
        project_id_or_slug: str,
        *,
        loader: str | None = None,
        game_versions: list[str] | None = None,
    ) -> list[CatalogVersion]:
        self.version_calls.append(project_id_or_slug)
        return []

    async def download_file(self, url: str) -> bytes:
        self.download_calls.append(url)
        return b"default-bytes"


def _geyser_with_build() -> GeyserMcCatalog:
    catalog = GeyserMcCatalog()

    async def _fake_get_json(path: str) -> Any:
        return {
            "version": "2.2.5",
            "build": 138,
            "downloads": {"spigot": {"name": "floodgate-spigot.jar", "sha256": "ab"}},
        }

    catalog._get_json = _fake_get_json  # type: ignore[method-assign]

    async def _fake_download(url: str) -> bytes:
        return b"geyser-bytes"

    catalog.download_file = _fake_download  # type: ignore[method-assign]
    return catalog


def _router() -> tuple[RoutingCatalog, _RecordingCatalog]:
    default = _RecordingCatalog()
    return RoutingCatalog(default=default, geyser=_geyser_with_build()), default


async def test_search_merges_floodgate_ahead_of_default() -> None:
    router, _ = _router()
    resp = await router.search(query="", loader="paper", game_versions=["1.21.1"])
    assert [h.project_id for h in resp.hits] == ["floodgate", "sodium"]
    assert resp.total_hits == 2


async def test_search_without_floodgate_match_returns_default_only() -> None:
    router, _ = _router()
    resp = await router.search(query="sodium", loader="paper", game_versions=[])
    assert [h.project_id for h in resp.hits] == ["sodium"]
    assert resp.total_hits == 1


async def test_get_project_floodgate_routes_to_geyser() -> None:
    router, default = _router()
    project = await router.get_project("floodgate")
    assert project.source == "geyser"
    assert default.project_calls == []  # default catalog never consulted


async def test_get_project_other_routes_to_default() -> None:
    router, default = _router()
    await router.get_project("sodium")
    assert default.project_calls == ["sodium"]


async def test_list_versions_floodgate_routes_to_geyser() -> None:
    router, default = _router()
    versions = await router.list_versions("floodgate", loader="paper")
    assert [v.version_id for v in versions] == ["2.2.5-138"]
    assert default.version_calls == []


async def test_list_versions_other_routes_to_default() -> None:
    router, default = _router()
    await router.list_versions("sodium", loader="paper")
    assert default.version_calls == ["sodium"]


async def test_download_routes_by_host() -> None:
    router, default = _router()
    geyser_bytes = await router.download_file(
        "https://download.geysermc.org/v2/projects/floodgate/x/downloads/spigot"
    )
    assert geyser_bytes == b"geyser-bytes"
    assert default.download_calls == []

    default_bytes = await router.download_file("https://cdn.modrinth.com/data/x.jar")
    assert default_bytes == b"default-bytes"
    assert default.download_calls == ["https://cdn.modrinth.com/data/x.jar"]
