"""HTTP JAR fetcher: streamed download, size cap, typed transport errors (FR-VER-3).

The real :class:`HttpxJarFetcher` is driven over an httpx ``MockTransport`` (no live
network, the issue's NO-live-network rule). Verifies the happy path returns the
bytes, an over-cap body is aborted with :class:`JarTooLargeError`, a non-2xx
response surfaces as :class:`JarDownloadError` (both map to ``jar_unavailable``),
and the SSRF guard refuses a URL resolving to a private IP (#1598).
"""

from __future__ import annotations

import functools

import httpx
import pytest

from mc_server_dashboard_api.versions.adapters import http_jar_fetcher, ssrf_guard
from mc_server_dashboard_api.versions.adapters.http_jar_fetcher import HttpxJarFetcher
from mc_server_dashboard_api.versions.domain.errors import (
    JarDownloadError,
    JarTooLargeError,
)

_URL = "https://example.test/server.jar"


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    """Make the adapter's httpx client route through a MockTransport handler.

    Also stubs the SSRF guard's resolver to a public IP so the guard is a
    pass-through for the mocked host (the tests never touch real DNS).
    """

    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    patched = functools.partial(httpx.AsyncClient, transport=transport)
    monkeypatch.setattr(httpx, "AsyncClient", patched)
    monkeypatch.setattr(ssrf_guard, "_resolve_host", lambda _host: ["93.184.216.34"])


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


@pytest.mark.asyncio
async def test_private_ip_is_download_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A URL resolving to a private IP is refused before any request (#1598)."""

    monkeypatch.setattr(ssrf_guard, "_resolve_host", lambda _host: ["10.0.0.1"])
    with pytest.raises(JarDownloadError, match="private"):
        await HttpxJarFetcher().fetch(_URL)
