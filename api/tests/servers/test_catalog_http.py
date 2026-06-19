"""HTTP-edge tests for the httpx-backed catalog client (issue #1264).

The real :class:`HttpxCatalogHttpClient` is driven over an httpx
``MockTransport`` (no live network, the issue's NO-live-network rule). Verifies
JSON + query params, a binary download, and that a 404 carries its status while
a 5xx / transport error does too (so the catalog adapter can map them).
"""

from __future__ import annotations

import functools

import httpx
import pytest

from mc_server_dashboard_api.servers.adapters.catalog_http import (
    HttpxCatalogHttpClient,
)
from mc_server_dashboard_api.servers.domain.catalog_http import CatalogHttpError


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
    result = await HttpxCatalogHttpClient().get_json(
        "https://api.modrinth.com/v2/search", params={"query": "sodium", "limit": "5"}
    )
    assert result == {"hits": [], "total_hits": 0}
    assert seen["query"] == "sodium"
    assert seen["limit"] == "5"


async def test_get_bytes_returns_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"jar-bytes")

    _install_transport(monkeypatch, handler)
    assert await HttpxCatalogHttpClient().get_bytes("https://cdn/x.jar") == b"jar-bytes"


async def test_404_carries_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not_found"})

    _install_transport(monkeypatch, handler)
    with pytest.raises(CatalogHttpError) as exc:
        await HttpxCatalogHttpClient().get_json("https://api.modrinth.com/v2/project/x")
    assert exc.value.status == 404


async def test_5xx_carries_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"down")

    _install_transport(monkeypatch, handler)
    with pytest.raises(CatalogHttpError) as exc:
        await HttpxCatalogHttpClient().get_bytes("https://cdn/x.jar")
    assert exc.value.status == 503


async def test_transport_error_has_no_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    _install_transport(monkeypatch, handler)
    with pytest.raises(CatalogHttpError) as exc:
        await HttpxCatalogHttpClient().get_json("https://api.modrinth.com/v2/search")
    assert exc.value.status is None
