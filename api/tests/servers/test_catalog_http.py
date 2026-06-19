"""HTTP-edge tests for the httpx-backed catalog client (issue #1264).

The real :class:`HttpxCatalogHttpClient` is driven over an httpx
``MockTransport`` (no live network, the issue's NO-live-network rule). Verifies
JSON + query params, a streamed binary download, that a 404 carries its status
while a 5xx / transport error does too (so the catalog adapter can map them), and
the two security guards: host pinning (SSRF) and a bounded streamed download
(OOM).
"""

from __future__ import annotations

import functools
from collections.abc import AsyncIterator

import httpx
import pytest

from mc_server_dashboard_api.servers.adapters.catalog_http import (
    HttpxCatalogHttpClient,
)
from mc_server_dashboard_api.servers.domain.catalog_http import (
    CatalogHostNotAllowedError,
    CatalogHttpError,
    CatalogTooLargeError,
)

_ALLOWED = frozenset({"api.modrinth.com", "cdn.modrinth.com"})
_API = "https://api.modrinth.com/v2"
_CDN = "https://cdn.modrinth.com/data/AABBCCDD/x.jar"
_CAP = 1024


def _client() -> HttpxCatalogHttpClient:
    return HttpxCatalogHttpClient(allowed_hosts=_ALLOWED)


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    patched = functools.partial(httpx.AsyncClient, transport=transport)
    monkeypatch.setattr(httpx, "AsyncClient", patched)


async def test_get_json_returns_payload_and_forwards_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"hits": [], "total_hits": 0})

    _install_transport(monkeypatch, handler)
    result = await _client().get_json(
        f"{_API}/search", params={"query": "sodium", "limit": "5"}
    )
    assert result == {"hits": [], "total_hits": 0}
    assert seen["query"] == "sodium"
    assert seen["limit"] == "5"


async def test_get_bytes_returns_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"jar-bytes")

    _install_transport(monkeypatch, handler)
    assert await _client().get_bytes(_CDN, max_bytes=_CAP) == b"jar-bytes"


class _CountingStream(httpx.AsyncByteStream):
    """An async byte stream that records how many cap-sized chunks were pulled.

    Lets the test prove the download was aborted mid-stream rather than after the
    whole (much larger) body was drained.
    """

    def __init__(self, *, chunk: int, chunks: int) -> None:
        self._chunk = chunk
        self._chunks = chunks
        self.pulled = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self._chunks):
            self.pulled += 1
            yield b"x" * self._chunk

    async def aclose(self) -> None:  # pragma: no cover - nothing to release
        return None


async def test_get_bytes_aborts_over_cap_without_buffering_whole_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A body past the cap is rejected mid-stream, never fully materialized."""

    stream = _CountingStream(chunk=_CAP, chunks=100)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    _install_transport(monkeypatch, handler)
    with pytest.raises(CatalogTooLargeError):
        await _client().get_bytes(_CDN, max_bytes=_CAP)
    # Aborted after crossing the cap, not after draining all 100 chunks.
    assert stream.pulled < 100


async def test_get_bytes_rejects_non_allowlisted_host_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A download URL pointing at an internal address is rejected, not fetched."""

    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, content=b"should-not-be-fetched")

    _install_transport(monkeypatch, handler)
    with pytest.raises(CatalogHostNotAllowedError):
        await _client().get_bytes(
            "http://169.254.169.254/latest/meta-data", max_bytes=_CAP
        )
    assert called is False


async def test_get_json_rejects_non_allowlisted_host_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    _install_transport(monkeypatch, handler)
    with pytest.raises(CatalogHostNotAllowedError):
        await _client().get_json("http://evil.example/v2/search")
    assert called is False


async def test_redirect_to_non_allowlisted_host_is_not_followed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 3xx to an internal host is surfaced as an error, never auto-followed."""

    followed_to: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.modrinth.com":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})
        followed_to.append(request.url.host)
        return httpx.Response(200, content=b"internal")

    _install_transport(monkeypatch, handler)
    # follow_redirects=False -> the 302 is a non-2xx, surfaced as a CatalogHttpError.
    with pytest.raises(CatalogHttpError):
        await _client().get_bytes(_CDN, max_bytes=_CAP)
    assert followed_to == []


async def test_404_carries_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not_found"})

    _install_transport(monkeypatch, handler)
    with pytest.raises(CatalogHttpError) as exc:
        await _client().get_json(f"{_API}/project/x")
    assert exc.value.status == 404


async def test_5xx_carries_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"down")

    _install_transport(monkeypatch, handler)
    with pytest.raises(CatalogHttpError) as exc:
        await _client().get_bytes(_CDN, max_bytes=_CAP)
    assert exc.value.status == 503


async def test_transport_error_has_no_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    _install_transport(monkeypatch, handler)
    with pytest.raises(CatalogHttpError) as exc:
        await _client().get_json(f"{_API}/search")
    assert exc.value.status is None
