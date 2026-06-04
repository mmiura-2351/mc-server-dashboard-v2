"""httpx-backed :class:`JsonFetcher` (FR-VER-2).

The single module touching httpx at the transport edge (the grpcio/aioboto3
precedent: the network library is confined to one adapter). A non-2xx response or
a transport error becomes a :class:`FetchError` so the retry/cache wrapper can
decide between a retry, a cached fallback, or surfacing catalog-unavailable.
"""

from __future__ import annotations

import httpx

from mc_server_dashboard_api.versions.domain.fetcher import FetchError, JsonFetcher

# A bounded per-request timeout so a hung source cannot stall a request thread.
_TIMEOUT = httpx.Timeout(10.0)


class HttpxJsonFetcher(JsonFetcher):
    """Fetch JSON over HTTP with httpx, mapping failures to :class:`FetchError`."""

    async def get_json(self, url: str) -> object:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise FetchError(str(exc)) from exc
