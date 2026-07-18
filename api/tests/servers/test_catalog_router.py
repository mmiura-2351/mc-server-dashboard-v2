"""Tests for :class:`RoutingCatalog` (issue #1905, #1961).

The router must keep every non-Floodgate project on the default (Modrinth)
catalog, route ``geysermc-floodgate`` metadata and GeyserMC-host downloads to
the GeyserMC adapter, and merge the Floodgate search hit ahead of the default
hits. The bare ``floodgate`` slug must fall through to Modrinth (#1961).
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


class _PaginatingCatalog(_RecordingCatalog):
    """Default-catalog stand-in with a fixed, paginated result set.

    Unlike :class:`_RecordingCatalog` (one hit regardless of the window), this
    serves ``total`` synthetic hits and honours ``limit``/``offset`` so the
    router's page math can be pinned at the page boundary.
    """

    def __init__(self, total: int) -> None:
        super().__init__()
        self._results = [
            CatalogSearchResult(
                project_id=f"mod-{i}",
                slug=f"mod-{i}",
                title=f"Mod {i}",
                description="",
                author="",
                icon_url=None,
                downloads=0,
                categories=[],
                latest_game_versions=[],
            )
            for i in range(total)
        ]

    async def search(
        self,
        *,
        query: str,
        loader: str,
        game_versions: list[str],
        limit: int = 20,
        offset: int = 0,
    ) -> CatalogSearchResponse:
        # Modrinth's live /v2/search clamps ``limit=0`` up to ``1`` rather than
        # returning no hits, so the router must never rely on a zero-limit
        # sub-query to omit a Modrinth hit.
        effective_limit = max(limit, 1)
        window = self._results[offset : offset + effective_limit]
        return CatalogSearchResponse(
            hits=window,
            total_hits=len(self._results),
            offset=offset,
            limit=limit,
        )


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


def _paginating_router(total: int) -> RoutingCatalog:
    return RoutingCatalog(
        default=_PaginatingCatalog(total), geyser=_geyser_with_build()
    )


async def test_search_merges_floodgate_ahead_of_default() -> None:
    router, _ = _router()
    resp = await router.search(query="", loader="paper", game_versions=["1.21.1"])
    assert [h.project_id for h in resp.hits] == ["geysermc-floodgate", "sodium"]
    assert resp.total_hits == 2


async def test_search_without_floodgate_match_returns_default_only() -> None:
    router, _ = _router()
    resp = await router.search(query="sodium", loader="paper", game_versions=[])
    assert [h.project_id for h in resp.hits] == ["sodium"]
    assert resp.total_hits == 1


async def test_search_page_zero_respects_limit_with_floodgate_first() -> None:
    # 5 Modrinth hits plus the Floodgate hit; a page of 3 must return 3, not 4,
    # with Floodgate first and only the first two Modrinth hits behind it.
    router = _paginating_router(total=5)
    resp = await router.search(
        query="", loader="paper", game_versions=[], limit=3, offset=0
    )
    assert [h.project_id for h in resp.hits] == ["geysermc-floodgate", "mod-0", "mod-1"]


async def test_search_total_hits_consistent_across_pages() -> None:
    # The Floodgate hit counts toward the total on every page's view of it.
    router = _paginating_router(total=5)
    page0 = await router.search(
        query="", loader="paper", game_versions=[], limit=3, offset=0
    )
    page1 = await router.search(
        query="", loader="paper", game_versions=[], limit=3, offset=3
    )
    assert page0.total_hits == page1.total_hits == 6


async def test_search_limit_one_page_zero_is_floodgate_only() -> None:
    # limit=1 with a Floodgate match: page 0 is Floodgate alone (1 hit, not 2),
    # and page 1 resumes at mod-0 with no Modrinth hit duplicated across the
    # boundary. Because Modrinth clamps a zero limit to 1, the router must not
    # sub-query Modrinth with ``limit - 1 == 0`` on page 0.
    router = _paginating_router(total=5)
    page0 = await router.search(
        query="", loader="paper", game_versions=[], limit=1, offset=0
    )
    page1 = await router.search(
        query="", loader="paper", game_versions=[], limit=1, offset=1
    )
    assert [h.project_id for h in page0.hits] == ["geysermc-floodgate"]
    assert [h.project_id for h in page1.hits] == ["mod-0"]


async def test_search_pagination_boundary_has_no_gap_or_duplicate() -> None:
    # Page 0 shows Floodgate + mod-0..mod-1; page 1 resumes at mod-2 with no
    # Floodgate hit and no dropped/duplicated Modrinth hit at the boundary.
    router = _paginating_router(total=5)
    page1 = await router.search(
        query="", loader="paper", game_versions=[], limit=3, offset=3
    )
    assert [h.project_id for h in page1.hits] == ["mod-2", "mod-3", "mod-4"]


async def test_get_project_synthetic_id_routes_to_geyser() -> None:
    router, default = _router()
    project = await router.get_project("geysermc-floodgate")
    assert project.source == "geyser"
    assert default.project_calls == []  # default catalog never consulted


async def test_get_project_bare_floodgate_slug_routes_to_default() -> None:
    """The bare ``floodgate`` slug must reach Modrinth, not GeyserMC (#1961)."""
    router, default = _router()
    await router.get_project("floodgate")
    assert default.project_calls == ["floodgate"]


async def test_get_project_other_routes_to_default() -> None:
    router, default = _router()
    await router.get_project("sodium")
    assert default.project_calls == ["sodium"]


async def test_list_versions_synthetic_id_routes_to_geyser() -> None:
    router, default = _router()
    versions = await router.list_versions("geysermc-floodgate", loader="paper")
    assert [v.version_id for v in versions] == ["2.2.5-138"]
    assert default.version_calls == []


async def test_list_versions_bare_floodgate_slug_routes_to_default() -> None:
    """The bare ``floodgate`` slug must reach Modrinth, not GeyserMC (#1961)."""
    router, default = _router()
    await router.list_versions("floodgate", loader="fabric")
    assert default.version_calls == ["floodgate"]


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
