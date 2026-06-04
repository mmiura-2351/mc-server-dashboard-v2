"""httpx-backed :class:`JarFetcher` (FR-VER-3).

Downloads a JAR's full bytes (the verify-then-store path buffers them; see
:mod:`...domain.jar_fetcher`). Confined to httpx at the transport edge alongside
:class:`HttpxJsonFetcher`. A non-2xx response raises so the start that triggered
the download fails before placement.
"""

from __future__ import annotations

import httpx

from mc_server_dashboard_api.versions.domain.jar_fetcher import JarFetcher

# JAR downloads are larger than manifest JSON; a generous read timeout.
_TIMEOUT = httpx.Timeout(60.0)


class HttpxJarFetcher(JarFetcher):
    """Download a JAR's bytes over HTTP with httpx."""

    async def fetch(self, url: str) -> bytes:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.content
