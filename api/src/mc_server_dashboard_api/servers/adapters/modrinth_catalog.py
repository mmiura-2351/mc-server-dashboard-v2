"""Keyless Modrinth catalog adapter (issue #1264).

Implements :class:`CatalogProvider` against the public Modrinth API v2
(``https://api.modrinth.com/v2``). Modrinth needs no API key for search,
project detail, or CDN download, so this adapter is unauthenticated. All
transport rides the injected :class:`CatalogHttpClient` so the adapter is
fakeable and tests never hit the network.

Side mapping (the epic's ``server`` / ``client`` / ``both`` axis): Modrinth
reports ``client_side`` and ``server_side`` independently, each
``required`` / ``optional`` / ``unsupported`` / ``unknown``. We collapse the two
axes onto one :data:`ModSide`:

* supported on one side only -> that side (``client`` / ``server``);
* supported on both (or ambiguous: both unknown / both unsupported) -> ``both``,
  the safe default (a ``both`` mod is present everywhere).

This is the most-accurate side source per the epic; the jar manifest stays the
uniform source for loader / mod id / deps at import time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from mc_server_dashboard_api.servers.domain.catalog_http import (
    CatalogHttpClient,
    CatalogHttpError,
)
from mc_server_dashboard_api.servers.domain.catalog_provider import (
    CatalogDependency,
    CatalogProject,
    CatalogProjectNotFoundError,
    CatalogProvider,
    CatalogSearchHit,
    CatalogSearchResult,
    CatalogUnavailableError,
    CatalogVersion,
)
from mc_server_dashboard_api.servers.domain.mod import ModSide

_BASE_URL = "https://api.modrinth.com/v2"

# Modrinth side values that mean "this mod runs on / is needed by this side".
_SUPPORTED = frozenset({"required", "optional"})


@dataclass(frozen=True)
class ModrinthCatalogProvider(CatalogProvider):
    """:class:`CatalogProvider` backed by the keyless Modrinth API v2."""

    http: CatalogHttpClient

    async def search(
        self,
        *,
        query: str,
        loader: str | None = None,
        game_version: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> CatalogSearchResult:
        params: dict[str, str] = {
            "query": query,
            "limit": str(limit),
            "offset": str(offset),
        }
        facets = _build_facets(loader, game_version)
        if facets is not None:
            params["facets"] = facets

        data = _as_dict(await self._get_json(f"{_BASE_URL}/search", params=params))
        hits = [
            _map_hit(hit) for hit in _as_list(data.get("hits")) if isinstance(hit, dict)
        ]
        total = data.get("total_hits")
        return CatalogSearchResult(
            hits=hits, total=total if isinstance(total, int) else len(hits)
        )

    async def get_project(self, project_id: str) -> CatalogProject:
        project = _as_dict(await self._get_json(f"{_BASE_URL}/project/{project_id}"))
        version_list = _as_list(
            await self._get_json(f"{_BASE_URL}/project/{project_id}/version")
        )
        versions = [_map_version(v) for v in version_list if isinstance(v, dict)]
        return CatalogProject(
            project_id=_str(project.get("id")),
            slug=_str(project.get("slug")),
            title=_str(project.get("title")),
            description=_str(project.get("description")),
            project_type=_str(project.get("project_type")),
            side=_map_side(project.get("client_side"), project.get("server_side")),
            loaders=_str_list(project.get("loaders")),
            game_versions=_str_list(project.get("game_versions")),
            versions=versions,
        )

    async def get_version(self, version_id: str) -> CatalogVersion:
        data = _as_dict(await self._get_json(f"{_BASE_URL}/version/{version_id}"))
        return _map_version(data)

    async def download(self, url: str) -> bytes:
        try:
            return await self.http.get_bytes(url)
        except CatalogHttpError as exc:
            raise CatalogUnavailableError(str(exc)) from exc

    async def _get_json(
        self, url: str, *, params: dict[str, str] | None = None
    ) -> object:
        """Fetch JSON, mapping a 404 to not-found and anything else to unavailable."""
        try:
            return await self.http.get_json(url, params=params)
        except CatalogHttpError as exc:
            if exc.status == 404:
                raise CatalogProjectNotFoundError(url) from exc
            raise CatalogUnavailableError(str(exc)) from exc


def _build_facets(loader: str | None, game_version: str | None) -> str | None:
    """Build Modrinth's nested-array facet string, or ``None`` when no facets.

    Modrinth facets are ANDed across the outer array; each inner array is ORed.
    A loader maps to ``categories:<loader>`` and a game version to
    ``versions:<v>``. ``project_type:mod`` is always added so search returns
    mods (not modpacks/resource packs/shaders). When neither facet is given we
    still constrain to mods.
    """
    facets: list[list[str]] = []
    if loader:
        facets.append([f"categories:{loader}"])
    if game_version:
        facets.append([f"versions:{game_version}"])
    facets.append(["project_type:mod"])
    return json.dumps(facets)


def _map_hit(hit: dict[str, Any]) -> CatalogSearchHit:
    downloads = hit.get("downloads")
    return CatalogSearchHit(
        project_id=_str(hit.get("project_id")),
        slug=_str(hit.get("slug")),
        title=_str(hit.get("title")),
        description=_str(hit.get("description")),
        project_type=_str(hit.get("project_type")),
        side=_map_side(hit.get("client_side"), hit.get("server_side")),
        loaders=_str_list(hit.get("loaders")),
        game_versions=_str_list(hit.get("versions")),
        downloads=downloads if isinstance(downloads, int) else 0,
        icon_url=hit.get("icon_url") if isinstance(hit.get("icon_url"), str) else None,
    )


def _map_version(data: dict[str, Any]) -> CatalogVersion:
    file = _primary_file(data.get("files"))
    hashes = _as_dict(file.get("hashes"))
    sha512 = hashes.get("sha512")
    return CatalogVersion(
        version_id=_str(data.get("id")),
        project_id=_str(data.get("project_id")),
        name=_str(data.get("name")),
        version_number=_str(data.get("version_number")),
        filename=_str(file.get("filename")),
        download_url=_str(file.get("url")),
        sha512=sha512 if isinstance(sha512, str) and sha512 else None,
        loaders=_str_list(data.get("loaders")),
        game_versions=_str_list(data.get("game_versions")),
        dependencies=[
            _map_dependency(dep)
            for dep in _as_list(data.get("dependencies"))
            if isinstance(dep, dict)
        ],
    )


def _map_dependency(dep: dict[str, Any]) -> CatalogDependency:
    project_id = dep.get("project_id")
    version_id = dep.get("version_id")
    return CatalogDependency(
        project_id=project_id if isinstance(project_id, str) else None,
        version_id=version_id if isinstance(version_id, str) else None,
        dependency_type=_str(dep.get("dependency_type")),
    )


def _primary_file(files: object) -> dict[str, Any]:
    """Pick the version's primary downloadable file.

    Modrinth marks one file ``primary``; that is the jar to install. When none
    is flagged primary (rare), fall back to the first file. An empty file list
    yields an empty dict, leaving ``download_url`` blank — the import use case
    rejects a version with no download.
    """
    entries = [f for f in _as_list(files) if isinstance(f, dict)]
    if not entries:
        return {}
    for entry in entries:
        if entry.get("primary") is True:
            return entry
    return entries[0]


def _map_side(client_side: object, server_side: object) -> ModSide:
    """Collapse Modrinth's two side axes onto our single :data:`ModSide`."""
    client_ok = client_side in _SUPPORTED
    server_ok = server_side in _SUPPORTED
    if client_ok and not server_ok:
        return "client"
    if server_ok and not client_ok:
        return "server"
    # Both supported, or ambiguous (unknown/unsupported on both) -> safe default.
    return "both"


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _str_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v]
    return []
