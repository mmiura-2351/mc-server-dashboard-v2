"""HTTP JAR fetcher: streamed download, size cap, typed transport errors (FR-VER-3).

The real :class:`HttpxJarFetcher` is driven over an httpx2 ``MockTransport`` (no live
network, the issue's NO-live-network rule). Verifies the happy path returns the
bytes, an over-cap body is aborted with :class:`JarTooLargeError`, a non-2xx
response surfaces as :class:`JarDownloadError` (both map to ``jar_unavailable``),
and the SSRF guard refuses a URL resolving to a private IP (#1598).
"""

from __future__ import annotations

import functools

import httpx2
import pytest

from mc_server_dashboard_api.versions.adapters import http_jar_fetcher, ssrf_guard
from mc_server_dashboard_api.versions.adapters.http_jar_fetcher import HttpxJarFetcher
from mc_server_dashboard_api.versions.domain.errors import (
    JarDownloadError,
    JarTooLargeError,
)

_URL = "https://example.test/server.jar"


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    """Make the adapter's httpx2 client route through a MockTransport handler.

    Also stubs the SSRF guard's resolver to a public IP so the guard is a
    pass-through for the mocked host (the tests never touch real DNS).
    """

    transport = httpx2.MockTransport(handler)  # type: ignore[arg-type]
    patched = functools.partial(httpx2.AsyncClient, transport=transport)
    monkeypatch.setattr(httpx2, "AsyncClient", patched)

    async def _public_resolver(_host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf_guard, "_async_resolve_host", _public_resolver)


@pytest.mark.asyncio
async def test_returns_downloaded_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"PK\x03\x04 a small jar"

    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=body)

    _install_transport(monkeypatch, handler)
    assert await HttpxJarFetcher().fetch(_URL) == body


@pytest.mark.asyncio
async def test_aborts_over_cap_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http_jar_fetcher, "MAX_JAR_BYTES", 8)

    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"way too many bytes for the cap")

    _install_transport(monkeypatch, handler)
    with pytest.raises(JarTooLargeError):
        await HttpxJarFetcher().fetch(_URL)


@pytest.mark.asyncio
async def test_non_2xx_is_download_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503, content=b"upstream down")

    _install_transport(monkeypatch, handler)
    with pytest.raises(JarDownloadError):
        await HttpxJarFetcher().fetch(_URL)


@pytest.mark.asyncio
async def test_private_ip_is_download_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A URL resolving to a private IP is refused before any request (#1598)."""

    async def _private_resolver(_host: str) -> list[str]:
        return ["10.0.0.1"]

    monkeypatch.setattr(ssrf_guard, "_async_resolve_host", _private_resolver)
    with pytest.raises(JarDownloadError, match="private"):
        await HttpxJarFetcher().fetch(_URL)


@pytest.mark.asyncio
async def test_does_not_follow_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 3xx redirect is surfaced, never transparently followed (#1903).

    Pins the SSRF no-redirect invariant independently of the httpx2 default: the
    redirect target must never be streamed. A future httpx2 default flip or an
    accidental ``follow_redirects=True`` would issue a second request to the
    ``Location`` target through the same transport, which the handler would see.

    The request URL is the pinned-IP form (SSRF guard pins the resolved address
    into the URL), so the handler matches on that (#1989).
    """

    requested: list[str] = []
    redirect_target = "https://redirect-target.test/internal"
    pinned_url = "https://93.184.216.34/server.jar"

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append(str(request.url))
        if str(request.url) == pinned_url:
            return httpx2.Response(302, headers={"Location": redirect_target})
        return httpx2.Response(200, content=b"PK\x03\x04 redirect target jar")

    _install_transport(monkeypatch, handler)
    with pytest.raises(JarDownloadError):
        await HttpxJarFetcher().fetch(_URL)
    assert requested == [pinned_url]


@pytest.mark.asyncio
async def test_connects_to_pinned_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """The HTTP request goes to the resolver-stubbed IP, not the hostname (#1989).

    Anti-rebinding regression: the SSRF guard resolves DNS and pins the IP into
    the request URL. A second DNS lookup (which could return a different, internal
    address) must never happen.
    """
    body = b"PK\x03\x04 pinned jar"

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "93.184.216.34"
        return httpx2.Response(200, content=body)

    _install_transport(monkeypatch, handler)
    assert await HttpxJarFetcher().fetch(_URL) == body
