"""HTTP JSON/text fetcher: SSRF guard + typed transport errors (FR-VER-2, #1598).

The real :class:`HttpxJsonFetcher` is driven over an httpx2 ``MockTransport`` (no
live network). Verifies the happy paths pass the SSRF guard and return the
document, and that the guard refuses a URL resolving to a private IP or a
non-HTTPS URL — surfaced as :class:`FetchError` for the retry/cache wrapper.
"""

from __future__ import annotations

import functools

import httpx2
import pytest

from mc_server_dashboard_api.versions.adapters import ssrf_guard
from mc_server_dashboard_api.versions.adapters.http_fetcher import HttpxJsonFetcher
from mc_server_dashboard_api.versions.domain.fetcher import (
    FetchError,
    FetchNotFoundError,
)

_URL = "https://example.test/manifest.json"


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    """Route the adapter's httpx2 client through a MockTransport and stub DNS.

    The SSRF guard's resolver is stubbed to a public IP so it is a pass-through
    for the mocked host (the tests never touch real DNS).
    """

    transport = httpx2.MockTransport(handler)  # type: ignore[arg-type]
    patched = functools.partial(httpx2.AsyncClient, transport=transport)
    monkeypatch.setattr(httpx2, "AsyncClient", patched)

    async def _public_resolver(_host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf_guard, "_async_resolve_host", _public_resolver)


@pytest.mark.asyncio
async def test_get_json_returns_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"latest": "1.21.1"})

    _install_transport(monkeypatch, handler)
    assert await HttpxJsonFetcher().get_json(_URL) == {"latest": "1.21.1"}


@pytest.mark.asyncio
async def test_get_text_returns_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="<metadata/>")

    _install_transport(monkeypatch, handler)
    assert await HttpxJsonFetcher().get_text(_URL) == "<metadata/>"


@pytest.mark.asyncio
async def test_get_json_non_2xx_is_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(503, content=b"upstream down")

    _install_transport(monkeypatch, handler)
    with pytest.raises(FetchError):
        await HttpxJsonFetcher().get_json(_URL)


@pytest.mark.asyncio
async def test_get_json_private_ip_is_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A URL resolving to a private IP is refused before any request (#1598)."""

    async def _private_resolver(_host: str) -> list[str]:
        return ["169.254.169.254"]

    monkeypatch.setattr(ssrf_guard, "_async_resolve_host", _private_resolver)
    with pytest.raises(FetchError, match="private"):
        await HttpxJsonFetcher().get_json(_URL)


@pytest.mark.asyncio
async def test_get_text_private_ip_is_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _private_resolver(_host: str) -> list[str]:
        return ["127.0.0.1"]

    monkeypatch.setattr(ssrf_guard, "_async_resolve_host", _private_resolver)
    with pytest.raises(FetchError, match="private"):
        await HttpxJsonFetcher().get_text(_URL)


@pytest.mark.asyncio
async def test_get_json_404_is_fetch_not_found_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 is a definitive 'not found', distinct from a transient error (#1539)."""

    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, content=b"not found")

    _install_transport(monkeypatch, handler)
    with pytest.raises(FetchNotFoundError):
        await HttpxJsonFetcher().get_json(_URL)


@pytest.mark.asyncio
async def test_get_text_404_is_fetch_not_found_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, content=b"not found")

    _install_transport(monkeypatch, handler)
    with pytest.raises(FetchNotFoundError):
        await HttpxJsonFetcher().get_text(_URL)


@pytest.mark.asyncio
async def test_get_json_non_https_is_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _public_resolver(_host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf_guard, "_async_resolve_host", _public_resolver)
    with pytest.raises(FetchError, match="HTTPS"):
        await HttpxJsonFetcher().get_json("http://example.test/manifest.json")


@pytest.mark.asyncio
async def test_get_json_does_not_follow_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 3xx redirect is surfaced, never transparently followed (#1903).

    Pins the SSRF no-redirect invariant independently of the httpx2 default: the
    redirect target must never be requested. A future httpx2 default flip or an
    accidental ``follow_redirects=True`` would issue a second request to the
    ``Location`` target through the same transport, which the handler would see.

    The request URL is the pinned-IP form (SSRF guard pins the resolved address
    into the URL), so the handler matches on that (#1989).
    """

    requested: list[str] = []
    redirect_target = "https://redirect-target.test/internal"
    pinned_url = "https://93.184.216.34/manifest.json"

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append(str(request.url))
        if str(request.url) == pinned_url:
            return httpx2.Response(302, headers={"Location": redirect_target})
        return httpx2.Response(200, json={"followed": True})

    _install_transport(monkeypatch, handler)
    with pytest.raises(FetchError):
        await HttpxJsonFetcher().get_json(_URL)
    assert requested == [pinned_url]


@pytest.mark.asyncio
async def test_get_json_connects_to_pinned_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP request goes to the resolver-stubbed IP, not the hostname (#1989).

    Anti-rebinding regression: the SSRF guard resolves DNS and pins the IP into
    the request URL. A second DNS lookup (which could return a different, internal
    address) must never happen.
    """

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "93.184.216.34"
        return httpx2.Response(200, json={"ok": True})

    _install_transport(monkeypatch, handler)
    await HttpxJsonFetcher().get_json(_URL)
