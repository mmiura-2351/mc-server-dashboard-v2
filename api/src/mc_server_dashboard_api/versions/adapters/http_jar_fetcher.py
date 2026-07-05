"""httpx-backed :class:`JarFetcher` (FR-VER-3).

Confined to httpx at the transport edge alongside :class:`HttpxJsonFetcher`. The
body is **streamed** (``aiter_bytes``) rather than read whole via
``response.content`` so a runaway or hostile response cannot buffer unboundedly
before verification: the download is aborted the moment it crosses
:data:`MAX_JAR_BYTES`. The capped bytes are then returned for the ensure-on-start
verify-then-store path, which hashes them against the source's published digest
(SHA-1 for vanilla, SHA-256 for Paper) before a single byte reaches the store. A
non-2xx response or a transport error becomes :class:`JarDownloadError`, and an
over-cap body becomes :class:`JarTooLargeError`, so the start fails cleanly (the
edge maps both to the ``jar_unavailable`` 503 surface) before placement.
"""

from __future__ import annotations

import httpx

from mc_server_dashboard_api.versions.adapters.ssrf_guard import (
    BlockedHostError,
    assert_url_allowed,
)
from mc_server_dashboard_api.versions.domain.errors import (
    JarDownloadError,
    JarTooLargeError,
)
from mc_server_dashboard_api.versions.domain.jar_fetcher import JarFetcher

# JAR downloads are larger than manifest JSON; a generous read timeout.
_TIMEOUT = httpx.Timeout(60.0)

# Hard ceiling on a single JAR download. 512 MiB is generous for any Minecraft
# server JAR (vanilla/Paper server JARs are tens of MB) while bounding the memory a
# single download can consume before its hash is verified. A body that crosses this
# is aborted mid-stream.
MAX_JAR_BYTES = 512 * 1024 * 1024


class HttpxJarFetcher(JarFetcher):
    """Stream a JAR's bytes over HTTP with httpx, capped at :data:`MAX_JAR_BYTES`."""

    async def fetch(self, url: str) -> bytes:
        try:
            assert_url_allowed(url)
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    return await _read_capped(response)
        except BlockedHostError as exc:
            raise JarDownloadError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise JarDownloadError(str(exc)) from exc


async def _read_capped(response: httpx.Response) -> bytes:
    """Buffer the streamed body, aborting the moment it crosses the cap."""

    chunks = bytearray()
    async for chunk in response.aiter_bytes():
        chunks += chunk
        if len(chunks) > MAX_JAR_BYTES:
            raise JarTooLargeError(
                f"JAR download exceeded {MAX_JAR_BYTES} bytes; aborted"
            )
    return bytes(chunks)
