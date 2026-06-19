"""httpx-backed :class:`CatalogHttpClient` (issue #1264).

The single servers-context module touching httpx at the catalog transport edge
(the versions-context ``HttpxJsonFetcher`` precedent: the network library is
confined to one adapter). A non-2xx response or a transport error becomes a
:class:`CatalogHttpError` — carrying the status code when there is one — so the
catalog adapter can map a 404 to not-found and everything else to unavailable.
"""

from __future__ import annotations

import httpx

from mc_server_dashboard_api.servers.domain.catalog_http import (
    CatalogHttpClient,
    CatalogHttpError,
)

# A bounded per-request timeout so a hung source cannot stall a request thread.
_TIMEOUT = httpx.Timeout(10.0)

# A descriptive User-Agent is requested by the Modrinth API guidelines.
_USER_AGENT = "mc-server-dashboard/2 (+https://github.com/mc-server-dashboard)"


class HttpxCatalogHttpClient(CatalogHttpClient):
    """Fetch catalog documents over HTTP with httpx, mapping failures."""

    def __init__(self, *, follow_redirects: bool = True) -> None:
        self._follow_redirects = follow_redirects

    async def get_json(
        self, url: str, *, params: dict[str, str] | None = None
    ) -> object:
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT,
                follow_redirects=self._follow_redirects,
                headers={"User-Agent": _USER_AGENT},
            ) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            raise CatalogHttpError(str(exc), status=exc.response.status_code) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise CatalogHttpError(str(exc)) from exc

    async def get_bytes(self, url: str) -> bytes:
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT,
                follow_redirects=self._follow_redirects,
                headers={"User-Agent": _USER_AGENT},
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.content
        except httpx.HTTPStatusError as exc:
            raise CatalogHttpError(str(exc), status=exc.response.status_code) from exc
        except httpx.HTTPError as exc:
            raise CatalogHttpError(str(exc)) from exc
