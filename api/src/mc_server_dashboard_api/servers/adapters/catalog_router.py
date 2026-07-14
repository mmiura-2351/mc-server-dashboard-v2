"""Routing :class:`CatalogProvider` over the default + GeyserMC catalogs (#1905).

The system has a single catalog seam, but Floodgate-Spigot lives only on
GeyserMC's download API while everything else lives on Modrinth. This composite
keeps that split invisible to the use cases: it forwards each call to whichever
backing catalog owns the project (by project id for metadata, by URL host for a
download) and merges search results so Floodgate appears alongside Modrinth hits.

It is intentionally a fixed two-way router, not a general provider registry --
the pluggable-catalog direction (#1269) is future work; this wires exactly the
one extra source.
"""

from __future__ import annotations

from mc_server_dashboard_api.servers.adapters.geysermc_catalog import GeyserMcCatalog
from mc_server_dashboard_api.servers.domain.catalog_provider import (
    CatalogProject,
    CatalogProvider,
    CatalogSearchResponse,
    CatalogVersion,
)


class RoutingCatalog(CatalogProvider):
    """Route catalog calls to the default catalog or the GeyserMC adapter."""

    def __init__(self, *, default: CatalogProvider, geyser: GeyserMcCatalog) -> None:
        self._default = default
        self._geyser = geyser

    async def search(
        self,
        *,
        query: str,
        loader: str,
        game_versions: list[str],
        limit: int = 20,
        offset: int = 0,
    ) -> CatalogSearchResponse:
        # Probe GeyserMC at offset 0 to learn whether its single Floodgate hit
        # matches this query/loader -- it only surfaces the hit on the first
        # page, but the hit belongs to every page's view of the combined total.
        geyser_resp = await self._geyser.search(
            query=query,
            loader=loader,
            game_versions=game_versions,
            limit=limit,
            offset=0,
        )
        if not geyser_resp.hits:
            # No Floodgate hit: forward the default catalog's page unchanged.
            return await self._default.search(
                query=query,
                loader=loader,
                game_versions=game_versions,
                limit=limit,
                offset=offset,
            )
        # Floodgate occupies index 0 of the combined [Floodgate, *Modrinth]
        # list, so the Modrinth window shifts by one and its total gains one on
        # every page. On the first page Floodgate takes one slot; later pages
        # are all Modrinth, read from one offset earlier.
        if offset == 0:
            # Fetch a full page and drop the last Modrinth hit rather than
            # requesting ``limit - 1``: a zero limit (``limit == 1``) is clamped
            # up to 1 by Modrinth, which would re-inflate page 0 to ``limit + 1``
            # and duplicate that hit onto page 1 (issue #1919).
            default_resp = await self._default.search(
                query=query,
                loader=loader,
                game_versions=game_versions,
                limit=limit,
                offset=0,
            )
            hits = geyser_resp.hits + default_resp.hits[: limit - 1]
        else:
            default_resp = await self._default.search(
                query=query,
                loader=loader,
                game_versions=game_versions,
                limit=limit,
                offset=offset - 1,
            )
            hits = default_resp.hits
        return CatalogSearchResponse(
            hits=hits,
            total_hits=default_resp.total_hits + 1,
            offset=offset,
            limit=limit,
        )

    async def get_project(self, project_id_or_slug: str) -> CatalogProject:
        catalog = self._route(project_id_or_slug)
        return await catalog.get_project(project_id_or_slug)

    async def list_versions(
        self,
        project_id_or_slug: str,
        *,
        loader: str | None = None,
        game_versions: list[str] | None = None,
    ) -> list[CatalogVersion]:
        catalog = self._route(project_id_or_slug)
        return await catalog.list_versions(
            project_id_or_slug, loader=loader, game_versions=game_versions
        )

    async def download_file(self, url: str) -> bytes:
        catalog = self._geyser if self._geyser.handles_url(url) else self._default
        return await catalog.download_file(url)

    def _route(self, project_id_or_slug: str) -> CatalogProvider:
        if self._geyser.handles(project_id_or_slug):
            return self._geyser
        return self._default
