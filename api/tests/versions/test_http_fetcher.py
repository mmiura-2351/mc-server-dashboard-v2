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
    monkeypatch.setattr(ssrf_guard, "_resolve_host", lambda _host: ["93.184.216.34"])


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

    monkeypatch.setattr(ssrf_guard, "_resolve_host", lambda _host: ["169.254.169.254"])
    with pytest.raises(FetchError, match="private"):
        await HttpxJsonFetcher().get_json(_URL)


@pytest.mark.asyncio
async def test_get_text_private_ip_is_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ssrf_guard, "_resolve_host", lambda _host: ["127.0.0.1"])
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
    monkeypatch.setattr(ssrf_guard, "_resolve_host", lambda _host: ["93.184.216.34"])
    with pytest.raises(FetchError, match="HTTPS"):
        await HttpxJsonFetcher().get_json("http://example.test/manifest.json")
