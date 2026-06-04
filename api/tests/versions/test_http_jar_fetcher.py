"""HTTP JAR fetcher: streamed download, size cap, typed transport errors (FR-VER-3).

The real :class:`HttpxJarFetcher` is driven over an httpx ``MockTransport`` (no live
network, the issue's NO-live-network rule). Verifies the happy path returns the
bytes, an over-cap body is aborted with :class:`JarTooLargeError`, and a non-2xx
response surfaces as :class:`JarDownloadError` (both map to ``jar_unavailable``).
"""

from __future__ import annotations

import functools

import httpx
import pytest

from mc_server_dashboard_api.versions.adapters import http_jar_fetcher
from mc_server_dashboard_api.versions.adapters.http_jar_fetcher import HttpxJarFetcher
from mc_server_dashboard_api.versions.domain.errors import (
    JarDownloadError,
    JarTooLargeError,
)

_URL = "https://example.test/server.jar"


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    """Make the adapter's httpx client route through a MockTransport handler."""

    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    patched = functools.partial(httpx.AsyncClient, transport=transport)
    monkeypatch.setattr(httpx, "AsyncClient", patched)


@pytest.mark.asyncio
async def test_returns_downloaded_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"PK\x03\x04 a small jar"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    _install_transport(monkeypatch, handler)
    assert await HttpxJarFetcher().fetch(_URL) == body


@pytest.mark.asyncio
async def test_aborts_over_cap_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http_jar_fetcher, "MAX_JAR_BYTES", 8)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"way too many bytes for the cap")

    _install_transport(monkeypatch, handler)
    with pytest.raises(JarTooLargeError):
        await HttpxJarFetcher().fetch(_URL)


@pytest.mark.asyncio
async def test_non_2xx_is_download_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"upstream down")

    _install_transport(monkeypatch, handler)
    with pytest.raises(JarDownloadError):
        await HttpxJarFetcher().fetch(_URL)
