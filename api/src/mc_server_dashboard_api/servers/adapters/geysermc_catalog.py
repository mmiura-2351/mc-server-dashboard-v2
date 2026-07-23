"""GeyserMC download-API adapter for :class:`CatalogProvider` (issue #1905).

Modrinth carries no Spigot/Paper build of Floodgate (its ``floodgate`` project
publishes only ``fabric`` / ``neoforge`` loaders), so a Paper server gets zero
compatible versions from the default catalog. GeyserMC's own build server is the
only publisher of the Floodgate-Spigot jar
(``https://download.geysermc.org/v2/projects/floodgate``), serving a sha256 per
build over HTTPS. This adapter surfaces exactly that one artifact through the
catalog seam so Floodgate installs symmetrically with Geyser, retiring the
jar-upload-only path (issue #1548 option 2).

Scope is deliberately narrow: the sole handled project is Floodgate-Spigot for
a Paper (``paper`` loader) server, always resolving the latest build at install
time (the epic's locked "no pinning, no bundling" rule). It mirrors the SSRF
hardening of :class:`ModrinthCatalog` (host allowlist, private-IP guard,
bounded redirects, download size cap).
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import httpx2

from mc_server_dashboard_api.servers.adapters.catalog_ssrf import (
    next_logical_url,
    pin_download_url,
)
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
    FileTooLargeError,
    wrap_shape_errors,
)

_BASE_URL = "https://download.geysermc.org/v2"
_HOST = "download.geysermc.org"
_USER_AGENT = "mc-server-dashboard/2.0 (mmiura2351@gmail.com)"
_METADATA_TIMEOUT = httpx2.Timeout(15.0)
_DOWNLOAD_TIMEOUT = httpx2.Timeout(120.0)
_MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024  # 512 MiB
_MAX_JSON_BYTES = 1 * 1024 * 1024  # 1 MiB (build metadata is tiny)
_MAX_REDIRECTS = 5

_ALLOWED_DOWNLOAD_HOSTS = frozenset({_HOST})

# The single artifact this adapter serves. ``_GEYSERMC_PROJECT`` is the
# upstream GeyserMC project name (used in API URLs); ``_SYNTHETIC_PROJECT_ID``
# is the client-facing id/slug that avoids shadowing Modrinth's own
# ``floodgate`` slug -- Modrinth publishes Fabric/NeoForge Floodgate builds
# under that slug (issue #1961). ``spigot`` is the Paper-compatible download
# of each build. The source string is the persisted :class:`PluginSource` value.
_GEYSERMC_PROJECT = "floodgate"
_SYNTHETIC_PROJECT_ID = "geysermc-floodgate"
_SPIGOT_DOWNLOAD = "spigot"
_PAPER_LOADER = "paper"
_SOURCE = "geyser"

_FLOODGATE_TITLE = "Floodgate"
_FLOODGATE_DESCRIPTION = (
    "Lets Bedrock players join without a Java (Microsoft) account, the companion "
    "plugin to Geyser."
)
_FLOODGATE_AUTHOR = "GeyserMC"


class GeyserMcCatalog(CatalogProvider):
    """GeyserMC download-API implementation of :class:`CatalogProvider`.

    Handles only the synthetic ``geysermc-floodgate`` project id; a router
    delegates every other project id -- including the bare ``floodgate`` slug
    (which belongs to Modrinth) -- to the default catalog.
    """

    def __init__(self, *, base_url: str = _BASE_URL) -> None:
        self._base_url = base_url

    # -- routing predicates (used by the catalog router) --

    def handles(self, project_id_or_slug: str) -> bool:
        """Whether this adapter owns *project_id_or_slug* (only the synthetic id)."""

        return project_id_or_slug == _SYNTHETIC_PROJECT_ID

    def handles_url(self, url: str) -> bool:
        """Whether ``url`` points at GeyserMC's download host."""

        return urlparse(url).hostname == _HOST

    # -- CatalogProvider --

    async def search(
        self,
        *,
        query: str,
        loader: str,
        game_versions: list[str],
        limit: int = 20,
        offset: int = 0,
    ) -> CatalogSearchResponse:
        # Floodgate-Spigot is Paper-only and lives on a single page; surface it
        # for a blank browse or a query that is a prefix/substring of its name.
        if (
            offset != 0
            or loader != _PAPER_LOADER
            or (query and query.lower() not in _SYNTHETIC_PROJECT_ID)
        ):
            return CatalogSearchResponse(
                hits=[], total_hits=0, offset=offset, limit=limit
            )
        hit = CatalogSearchResult(
            project_id=_SYNTHETIC_PROJECT_ID,
            slug=_SYNTHETIC_PROJECT_ID,
            title=_FLOODGATE_TITLE,
            description=_FLOODGATE_DESCRIPTION,
            author=_FLOODGATE_AUTHOR,
            icon_url=None,
            downloads=0,
            categories=["bedrock"],
            latest_game_versions=[],
        )
        return CatalogSearchResponse(
            hits=[hit], total_hits=1, offset=offset, limit=limit
        )

    async def get_project(self, project_id_or_slug: str) -> CatalogProject:
        if not self.handles(project_id_or_slug):
            raise CatalogProjectNotFoundError(project_id_or_slug)
        return CatalogProject(
            project_id=_SYNTHETIC_PROJECT_ID,
            slug=_SYNTHETIC_PROJECT_ID,
            title=_FLOODGATE_TITLE,
            description=_FLOODGATE_DESCRIPTION,
            body=_FLOODGATE_DESCRIPTION,
            author=_FLOODGATE_AUTHOR,
            icon_url=None,
            downloads=0,
            categories=["bedrock"],
            game_versions=[],
            loaders=[_PAPER_LOADER],
            source=_SOURCE,
        )

    async def list_versions(
        self,
        project_id_or_slug: str,
        *,
        loader: str | None = None,
        game_versions: list[str] | None = None,
    ) -> list[CatalogVersion]:
        if not self.handles(project_id_or_slug):
            raise CatalogProjectNotFoundError(project_id_or_slug)
        # Floodgate-Spigot targets Paper only; a non-Paper server has no build.
        if loader is not None and loader != _PAPER_LOADER:
            return []
        build = await self._get_json(
            f"/projects/{_GEYSERMC_PROJECT}/versions/latest/builds/latest"
        )
        with wrap_shape_errors("geysermc"):
            return [self._parse_latest_build(build)]

    async def download_file(self, url: str) -> bytes:
        logical_url = url
        pinned = await pin_download_url(logical_url, _ALLOWED_DOWNLOAD_HOSTS)
        try:
            async with httpx2.AsyncClient(
                timeout=_DOWNLOAD_TIMEOUT,
                headers=self._headers(),
            ) as client:
                for _ in range(_MAX_REDIRECTS):
                    async with client.stream(
                        "GET",
                        pinned.url,
                        headers=pinned.headers,
                        extensions=pinned.extensions,
                        follow_redirects=False,
                    ) as response:
                        if response.is_redirect:
                            location = response.headers.get("location", "")
                            logical_url = next_logical_url(location, logical_url)
                            pinned = await pin_download_url(
                                logical_url,
                                _ALLOWED_DOWNLOAD_HOSTS,
                                redirect=True,
                            )
                            continue
                        response.raise_for_status()
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > _MAX_DOWNLOAD_BYTES:
                                raise FileTooLargeError(
                                    f"download exceeds {_MAX_DOWNLOAD_BYTES} bytes"
                                )
                            chunks.append(chunk)
                        return b"".join(chunks)
                raise CatalogUnavailableError("too many redirects")
        except (FileTooLargeError, CatalogUnavailableError):
            raise
        except httpx2.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise CatalogProjectNotFoundError(url) from exc
            raise CatalogUnavailableError(str(exc)) from exc
        except httpx2.TransportError as exc:
            raise CatalogUnavailableError(str(exc)) from exc

    # -- internal helpers --

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": _USER_AGENT}

    def _parse_latest_build(self, build: dict[str, Any]) -> CatalogVersion:
        """Map a GeyserMC ``.../builds/latest`` response to a :class:`CatalogVersion`.

        The Spigot download's ``sha256`` is the integrity hash the install path
        verifies; the ``version_id`` combines the released version and monotonic
        build number so a stale selection (a newer build landed mid-install) is
        detected as a missing version rather than silently installed.
        """

        version = str(build.get("version", ""))
        build_no = build.get("build", "")
        downloads = build.get("downloads", {})
        spigot = downloads.get(_SPIGOT_DOWNLOAD)
        if not spigot:
            raise CatalogProjectNotFoundError(
                f"no {_SPIGOT_DOWNLOAD} download in floodgate build {build_no}"
            )
        filename = spigot.get("name", "")
        sha256 = spigot.get("sha256", "")
        download_url = (
            f"{self._base_url}/projects/{_GEYSERMC_PROJECT}"
            f"/versions/{version}/builds/{build_no}/downloads/{_SPIGOT_DOWNLOAD}"
        )
        catalog_file = CatalogFile(
            url=download_url,
            filename=filename,
            size=0,
            sha512="",
            primary=True,
            sha256=sha256,
        )
        return CatalogVersion(
            version_id=f"{version}-{build_no}",
            version_number=version,
            name=f"Floodgate {version} (build {build_no})",
            game_versions=[],
            loaders=[_PAPER_LOADER],
            files=[catalog_file],
            date_published=str(build.get("time", "")),
            dependencies=[],
        )

    async def _get_json(self, path: str) -> Any:
        # Follow redirects manually with the same SSRF guard as download_file:
        # GeyserMC's ``.../builds/latest`` endpoint 302-redirects to the concrete
        # ``.../builds/{build}`` where the JSON lives, so a non-following fetch
        # would fail every Floodgate resolution (issue #1905). An absolute URL is
        # used (no client base_url) so each hop's host is re-validated.
        logical_url = f"{self._base_url}{path}"
        pinned = await pin_download_url(logical_url, _ALLOWED_DOWNLOAD_HOSTS)
        try:
            async with httpx2.AsyncClient(
                timeout=_METADATA_TIMEOUT,
                headers=self._headers(),
            ) as client:
                for _ in range(_MAX_REDIRECTS):
                    async with client.stream(
                        "GET",
                        pinned.url,
                        headers=pinned.headers,
                        extensions=pinned.extensions,
                        follow_redirects=False,
                    ) as response:
                        if response.is_redirect:
                            location = response.headers.get("location", "")
                            logical_url = next_logical_url(location, logical_url)
                            pinned = await pin_download_url(
                                logical_url,
                                _ALLOWED_DOWNLOAD_HOSTS,
                                redirect=True,
                            )
                            continue
                        if response.status_code == 404:
                            raise CatalogProjectNotFoundError(path)
                        response.raise_for_status()
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > _MAX_JSON_BYTES:
                                raise CatalogUnavailableError(
                                    f"response too large: {total} bytes"
                                )
                            chunks.append(chunk)
                        return json.loads(b"".join(chunks))
                raise CatalogUnavailableError("too many redirects")
        except (CatalogProjectNotFoundError, CatalogUnavailableError):
            raise
        except httpx2.HTTPStatusError as exc:
            raise CatalogUnavailableError(str(exc)) from exc
        except (httpx2.TransportError, ValueError) as exc:
            raise CatalogUnavailableError(str(exc)) from exc
