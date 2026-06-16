"""Modrinth API v2 adapter for :class:`CatalogProvider` (issue #1151).

Uses httpx with a per-request client (the same pattern as
:class:`HttpxJsonFetcher` in the versions adapters). Modrinth ToS requires a
descriptive User-Agent.
"""

from __future__ import annotations

import json

import httpx

from mc_server_dashboard_api.servers.domain.catalog_provider import (
    CatalogFile,
    CatalogProject,
    CatalogProvider,
    CatalogSearchResponse,
    CatalogSearchResult,
    CatalogVersion,
)
from mc_server_dashboard_api.servers.domain.errors import (
    CatalogProjectNotFoundError,
    CatalogUnavailableError,
)

_BASE_URL = "https://api.modrinth.com/v2"
_USER_AGENT = "mc-server-dashboard/2.0 (mmiura2351@gmail.com)"
_METADATA_TIMEOUT = httpx.Timeout(15.0)
_DOWNLOAD_TIMEOUT = httpx.Timeout(120.0)
_MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024  # 512 MiB


class ModrinthCatalog(CatalogProvider):
    """Modrinth API v2 implementation of :class:`CatalogProvider`."""

    def __init__(self, *, base_url: str = _BASE_URL) -> None:
        self._base_url = base_url

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": _USER_AGENT}

    async def search(
        self,
        *,
        query: str,
        loader: str,
        game_versions: list[str],
        limit: int = 20,
        offset: int = 0,
    ) -> CatalogSearchResponse:
        # Build Modrinth facets: outer list = AND, inner list = OR.
        facets: list[list[str]] = [[f"categories:{loader}"]]
        if game_versions:
            facets.append([f"versions:{v}" for v in game_versions])
        params: dict[str, str | int] = {
            "query": query,
            "facets": json.dumps(facets),
            "limit": limit,
            "offset": offset,
        }
        data = await self._get_json("/search", params=params)
        hits = [
            CatalogSearchResult(
                project_id=h["project_id"],
                slug=h.get("slug", ""),
                title=h.get("title", ""),
                description=h.get("description", ""),
                author=h.get("author", ""),
                icon_url=h.get("icon_url"),
                downloads=h.get("downloads", 0),
                categories=h.get("categories", []),
                latest_game_versions=h.get("versions", []),
            )
            for h in data.get("hits", [])
        ]
        return CatalogSearchResponse(
            hits=hits,
            total_hits=data.get("total_hits", 0),
            offset=data.get("offset", offset),
            limit=data.get("limit", limit),
        )

    async def get_project(self, project_id_or_slug: str) -> CatalogProject:
        data = await self._get_json(f"/project/{project_id_or_slug}")
        return CatalogProject(
            project_id=data["id"],
            slug=data.get("slug", ""),
            title=data.get("title", ""),
            description=data.get("description", ""),
            body=data.get("body", ""),
            author=data.get("team", None),
            icon_url=data.get("icon_url"),
            downloads=data.get("downloads", 0),
            categories=data.get("categories", []),
            game_versions=data.get("game_versions", []),
            loaders=data.get("loaders", []),
        )

    async def list_versions(
        self,
        project_id_or_slug: str,
        *,
        loader: str | None = None,
        game_versions: list[str] | None = None,
    ) -> list[CatalogVersion]:
        params: dict[str, str | int] = {}
        if loader is not None:
            params["loaders"] = json.dumps([loader])
        if game_versions:
            params["game_versions"] = json.dumps(game_versions)
        data = await self._get_json(
            f"/project/{project_id_or_slug}/version", params=params
        )
        return [self._parse_version(v) for v in data]

    async def download_file(self, url: str) -> bytes:
        try:
            async with httpx.AsyncClient(
                timeout=_DOWNLOAD_TIMEOUT,
                headers=self._headers(),
                follow_redirects=True,
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                content = response.content
                if len(content) > _MAX_DOWNLOAD_BYTES:
                    raise CatalogUnavailableError(
                        f"download exceeds {_MAX_DOWNLOAD_BYTES} bytes"
                    )
                return content
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise CatalogProjectNotFoundError(url) from exc
            raise CatalogUnavailableError(str(exc)) from exc
        except httpx.TransportError as exc:
            raise CatalogUnavailableError(str(exc)) from exc

    # -- internal helpers --

    async def _get_json(
        self, path: str, *, params: dict[str, str | int] | None = None
    ) -> dict:  # type: ignore[type-arg]
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=_METADATA_TIMEOUT,
                headers=self._headers(),
            ) as client:
                response = await client.get(path, params=params)
                if response.status_code == 404:
                    raise CatalogProjectNotFoundError(path)
                response.raise_for_status()
                return response.json()  # type: ignore[no-any-return]
        except CatalogProjectNotFoundError:
            raise
        except httpx.HTTPStatusError as exc:
            raise CatalogUnavailableError(str(exc)) from exc
        except httpx.TransportError as exc:
            raise CatalogUnavailableError(str(exc)) from exc

    @staticmethod
    def _parse_version(v: dict) -> CatalogVersion:  # type: ignore[type-arg]
        files = [
            CatalogFile(
                url=f.get("url", ""),
                filename=f.get("filename", ""),
                size=f.get("size", 0),
                sha512=f.get("hashes", {}).get("sha512", ""),
                primary=f.get("primary", False),
            )
            for f in v.get("files", [])
        ]
        return CatalogVersion(
            version_id=v["id"],
            version_number=v.get("version_number", ""),
            name=v.get("name", ""),
            game_versions=v.get("game_versions", []),
            loaders=v.get("loaders", []),
            files=files,
            date_published=v.get("date_published", ""),
        )
