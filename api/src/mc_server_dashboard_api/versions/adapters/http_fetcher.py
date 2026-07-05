"""httpx-backed :class:`JsonFetcher` (FR-VER-2).

The single module touching httpx at the transport edge (the grpcio/aioboto3
precedent: the network library is confined to one adapter). A non-2xx response or
a transport error becomes a :class:`FetchError` so the retry/cache wrapper can
decide between a retry, a cached fallback, or surfacing catalog-unavailable.
"""

from __future__ import annotations

import httpx

from mc_server_dashboard_api.versions.adapters.ssrf_guard import (
    BlockedHostError,
    assert_url_allowed,
)
from mc_server_dashboard_api.versions.domain.fetcher import (
    FetchError,
    FetchNotFoundError,
    JsonFetcher,
)

# A bounded per-request timeout so a hung source cannot stall a request thread.
_TIMEOUT = httpx.Timeout(10.0)


class HttpxJsonFetcher(JsonFetcher):
    """Fetch a document over HTTP with httpx, mapping failures to FetchError."""

    async def get_json(self, url: str) -> object:
        try:
            assert_url_allowed(url)
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.get(url)
                _check_not_found(response)
                response.raise_for_status()
                return response.json()
        except BlockedHostError as exc:
            raise FetchError(str(exc)) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise FetchError(str(exc)) from exc

    async def get_text(self, url: str) -> str:
        try:
            assert_url_allowed(url)
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.get(url)
                _check_not_found(response)
                response.raise_for_status()
                return response.text
        except BlockedHostError as exc:
            raise FetchError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise FetchError(str(exc)) from exc


def _check_not_found(response: httpx.Response) -> None:
    """Raise :class:`FetchNotFoundError` if the response is 404."""

    if response.status_code == 404:
        raise FetchNotFoundError(f"404 Not Found: {response.url}")
