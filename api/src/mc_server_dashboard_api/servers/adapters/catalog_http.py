"""httpx-backed :class:`CatalogHttpClient` (issue #1264).

The single servers-context module touching httpx at the catalog transport edge
(the versions-context ``HttpxJsonFetcher`` precedent: the network library is
confined to one adapter). A non-2xx response or a transport error becomes a
:class:`CatalogHttpError` — carrying the status code when there is one — so the
catalog adapter can map a 404 to not-found and everything else to unavailable.

Two SSRF/OOM guards live here so a future CurseForge adapter (#1269) inherits
them:

* **Host pinning.** The JSON base and the download URL both come from the
  third-party API response, so a crafted/compromised payload — or a redirect —
  could point at an internal address (``169.254.169.254``, loopback, RFC-1918).
  Every request URL's host is checked against the caller-supplied allowlist
  *before* the request, and redirects are disabled so a 3xx to a non-allowlisted
  host cannot smuggle the fetch past the check.
* **Streamed, bounded download.** ``get_bytes`` streams the body (mirroring
  ``versions/adapters/http_jar_fetcher.py``) and aborts the moment it crosses
  the caller-supplied cap, so an oversized/runaway upstream file is rejected
  before it can buffer the whole body into memory.
"""

from __future__ import annotations

import httpx

from mc_server_dashboard_api.servers.domain.catalog_http import (
    CatalogHostNotAllowedError,
    CatalogHttpClient,
    CatalogHttpError,
    CatalogTooLargeError,
)

# A bounded per-request timeout so a hung source cannot stall a request thread.
_TIMEOUT = httpx.Timeout(10.0)

# A descriptive User-Agent is requested by the Modrinth API guidelines.
_USER_AGENT = "mc-server-dashboard/2 (+https://github.com/mc-server-dashboard)"


class HttpxCatalogHttpClient(CatalogHttpClient):
    """Fetch catalog documents over HTTP with httpx, host-pinned and bounded.

    ``allowed_hosts`` is the set of hostnames the client may fetch from (the
    catalog API host for JSON and the CDN host(s) for downloads). Any URL whose
    host is outside it is rejected before the request, and redirects are disabled
    so a 3xx cannot redirect to a non-allowlisted host.
    """

    def __init__(self, *, allowed_hosts: frozenset[str]) -> None:
        self._allowed_hosts = allowed_hosts

    async def get_json(
        self, url: str, *, params: dict[str, str] | None = None
    ) -> object:
        self._require_allowed_host(url)
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT,
                follow_redirects=False,
                headers={"User-Agent": _USER_AGENT},
            ) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            raise CatalogHttpError(str(exc), status=exc.response.status_code) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise CatalogHttpError(str(exc)) from exc

    async def get_bytes(self, url: str, *, max_bytes: int) -> bytes:
        self._require_allowed_host(url)
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT,
                follow_redirects=False,
                headers={"User-Agent": _USER_AGENT},
            ) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    return await _read_capped(response, max_bytes)
        except httpx.HTTPStatusError as exc:
            raise CatalogHttpError(str(exc), status=exc.response.status_code) from exc
        except httpx.HTTPError as exc:
            raise CatalogHttpError(str(exc)) from exc

    def _require_allowed_host(self, url: str) -> None:
        """Reject a URL whose host is not on the allowlist, before any request."""
        host = httpx.URL(url).host
        if host not in self._allowed_hosts:
            raise CatalogHostNotAllowedError(f"host not allowed: {host!r}")


async def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    """Buffer the streamed body, aborting the moment it crosses ``max_bytes``."""

    chunks = bytearray()
    async for chunk in response.aiter_bytes():
        chunks += chunk
        if len(chunks) > max_bytes:
            raise CatalogTooLargeError(
                f"catalog download exceeded {max_bytes} bytes; aborted"
            )
    return bytes(chunks)
