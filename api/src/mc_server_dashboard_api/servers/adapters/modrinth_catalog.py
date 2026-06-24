"""Modrinth API v2 adapter for :class:`CatalogProvider` (issue #1151).

Uses httpx with a per-request client (the same pattern as
:class:`HttpxJsonFetcher` in the versions adapters). Modrinth ToS requires a
descriptive User-Agent.
"""

from __future__ import annotations

import ipaddress
import json
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx

from mc_server_dashboard_api.servers.domain.catalog_provider import (
    CatalogDependency,
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
)

_BASE_URL = "https://api.modrinth.com/v2"
_USER_AGENT = "mc-server-dashboard/2.0 (mmiura2351@gmail.com)"
_METADATA_TIMEOUT = httpx.Timeout(15.0)
_DOWNLOAD_TIMEOUT = httpx.Timeout(120.0)
_MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024  # 512 MiB
_MAX_JSON_BYTES = 10 * 1024 * 1024  # 10 MiB
_MAX_REDIRECTS = 5

_ALLOWED_DOWNLOAD_HOSTS = frozenset(
    {
        "cdn.modrinth.com",
        "github.com",
        "objects.githubusercontent.com",
    }
)


def _default_resolve_host(hostname: str) -> list[str]:
    """Resolve *hostname* to a list of IP address strings via DNS."""
    results = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    return list({str(addr[4][0]) for addr in results})


_resolve_host: Callable[[str], list[str]] = _default_resolve_host


def _assert_no_private_ips(
    hostname: str,
    *,
    _resolver: Callable[[str], list[str]] | None = None,
) -> None:
    """Raise :class:`CatalogUnavailableError` if *hostname* resolves to a private IP.

    Guards against DNS-rebinding attacks where an attacker-controlled hostname
    initially passes the allowlist check but resolves to a private/loopback
    address.
    """
    resolver = _resolver or _resolve_host
    try:
        addrs = resolver(hostname)
    except (socket.gaierror, OSError) as exc:
        raise CatalogUnavailableError(f"DNS resolution failed for {hostname}") from exc
    for addr in addrs:
        ip = ipaddress.ip_address(addr)
        if not ip.is_global:
            raise CatalogUnavailableError(
                f"hostname {hostname} resolved to private/reserved IP: {addr}"
            )


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
        data = await self._get_json(f"/project/{quote(project_id_or_slug, safe='')}")
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
            client_side=data.get("client_side", "unknown"),
            server_side=data.get("server_side", "unknown"),
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
            f"/project/{quote(project_id_or_slug, safe='')}/version", params=params
        )
        return [self._parse_version(v) for v in data]

    async def download_file(self, url: str) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise CatalogUnavailableError(f"download URL must use HTTPS: {url}")
        if parsed.hostname not in _ALLOWED_DOWNLOAD_HOSTS:
            raise CatalogUnavailableError(
                f"download URL host not allowed: {parsed.hostname}"
            )
        _assert_no_private_ips(parsed.hostname)
        try:
            async with httpx.AsyncClient(
                timeout=_DOWNLOAD_TIMEOUT,
                headers=self._headers(),
            ) as client:
                current_url = url
                for _ in range(_MAX_REDIRECTS):
                    async with client.stream(
                        "GET", current_url, follow_redirects=False
                    ) as response:
                        if response.is_redirect:
                            location = response.headers.get("location", "")
                            redirect_parsed = urlparse(location)
                            if not redirect_parsed.scheme:
                                location = urljoin(current_url, location)
                                redirect_parsed = urlparse(location)
                            if redirect_parsed.scheme != "https":
                                raise CatalogUnavailableError(
                                    f"redirect to non-HTTPS: {location}"
                                )
                            if redirect_parsed.hostname not in _ALLOWED_DOWNLOAD_HOSTS:
                                raise CatalogUnavailableError(
                                    f"redirect to disallowed host: "
                                    f"{redirect_parsed.hostname}"
                                )
                            _assert_no_private_ips(redirect_parsed.hostname)
                            current_url = location
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
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise CatalogProjectNotFoundError(url) from exc
            raise CatalogUnavailableError(str(exc)) from exc
        except httpx.TransportError as exc:
            raise CatalogUnavailableError(str(exc)) from exc

    # -- internal helpers --

    async def _get_json(
        self, path: str, *, params: dict[str, str | int] | None = None
    ) -> Any:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=_METADATA_TIMEOUT,
                headers=self._headers(),
            ) as client:
                async with client.stream("GET", path, params=params) as response:
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
        except (CatalogProjectNotFoundError, CatalogUnavailableError):
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
        dependencies = [
            CatalogDependency(
                version_id=d.get("version_id"),
                project_id=d.get("project_id", ""),
                dependency_type=d.get("dependency_type", "required"),
            )
            for d in v.get("dependencies", [])
        ]
        return CatalogVersion(
            version_id=v["id"],
            version_number=v.get("version_number", ""),
            name=v.get("name", ""),
            game_versions=v.get("game_versions", []),
            loaders=v.get("loaders", []),
            files=files,
            date_published=v.get("date_published", ""),
            dependencies=dependencies,
        )
