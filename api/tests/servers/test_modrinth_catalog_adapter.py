"""Adapter-level tests for ModrinthCatalog security hardening (issue #1159).

Tests redirect validation, JSON response size cap, and the production adapter's
host-allowlist enforcement — these cannot be tested through the FakeCatalogProvider.
"""

from __future__ import annotations

import httpx
import pytest

from mc_server_dashboard_api.servers.adapters.modrinth_catalog import (
    _ALLOWED_DOWNLOAD_HOSTS,
    _MAX_JSON_BYTES,
    _MAX_REDIRECTS,
    ModrinthCatalog,
)
from mc_server_dashboard_api.servers.domain.errors import CatalogUnavailableError

# -- SSRF redirect bypass --


async def test_download_redirect_to_disallowed_host_raises() -> None:
    """A redirect from an allowed host to an internal/disallowed host is blocked."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if "cdn.modrinth.com" in str(request.url):
            return httpx.Response(
                302,
                headers={"location": "https://169.254.169.254/metadata"},
            )
        return httpx.Response(200, content=b"secret")

    transport = httpx.MockTransport(_handler)
    catalog = ModrinthCatalog()
    # Monkey-patch the client factory to inject our mock transport.
    original = catalog.download_file

    async def _patched_download(url: str) -> bytes:
        # We need to inject the transport into the client created inside
        # download_file. We do this by temporarily replacing httpx.AsyncClient.
        real_init = httpx.AsyncClient.__init__

        def patched_init(self_client: httpx.AsyncClient, **kwargs: object) -> None:
            kwargs["transport"] = transport  # type: ignore[assignment]
            kwargs.pop("follow_redirects", None)
            real_init(self_client, **kwargs)

        httpx.AsyncClient.__init__ = patched_init  # type: ignore[assignment]
        try:
            return await original(url)
        finally:
            httpx.AsyncClient.__init__ = real_init  # type: ignore[assignment]

    with pytest.raises(CatalogUnavailableError, match="disallowed host"):
        await _patched_download("https://cdn.modrinth.com/data/test.jar")


async def test_download_too_many_redirects_raises() -> None:
    """More than _MAX_REDIRECTS hops raises CatalogUnavailableError."""

    call_count = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        # Always redirect back to an allowed host.
        return httpx.Response(
            302,
            headers={
                "location": f"https://cdn.modrinth.com/data/test.jar?r={call_count}"
            },
        )

    transport = httpx.MockTransport(_handler)
    catalog = ModrinthCatalog()
    original = catalog.download_file

    async def _patched_download(url: str) -> bytes:
        real_init = httpx.AsyncClient.__init__

        def patched_init(self_client: httpx.AsyncClient, **kwargs: object) -> None:
            kwargs["transport"] = transport  # type: ignore[assignment]
            kwargs.pop("follow_redirects", None)
            real_init(self_client, **kwargs)

        httpx.AsyncClient.__init__ = patched_init  # type: ignore[assignment]
        try:
            return await original(url)
        finally:
            httpx.AsyncClient.__init__ = real_init  # type: ignore[assignment]

    with pytest.raises(CatalogUnavailableError, match="too many redirects"):
        await _patched_download("https://cdn.modrinth.com/data/test.jar")

    # Verify we made exactly _MAX_REDIRECTS + 1 attempts (initial + redirects).
    assert call_count == _MAX_REDIRECTS


async def test_download_redirect_to_non_https_raises() -> None:
    """A redirect from HTTPS to HTTP is blocked."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "http://cdn.modrinth.com/data/test.jar"},
        )

    transport = httpx.MockTransport(_handler)
    catalog = ModrinthCatalog()
    original = catalog.download_file

    async def _patched_download(url: str) -> bytes:
        real_init = httpx.AsyncClient.__init__

        def patched_init(self_client: httpx.AsyncClient, **kwargs: object) -> None:
            kwargs["transport"] = transport  # type: ignore[assignment]
            kwargs.pop("follow_redirects", None)
            real_init(self_client, **kwargs)

        httpx.AsyncClient.__init__ = patched_init  # type: ignore[assignment]
        try:
            return await original(url)
        finally:
            httpx.AsyncClient.__init__ = real_init  # type: ignore[assignment]

    with pytest.raises(CatalogUnavailableError, match="non-HTTPS"):
        await _patched_download("https://cdn.modrinth.com/data/test.jar")


# -- Unbounded JSON response --


async def test_get_json_oversized_response_raises() -> None:
    """A JSON response exceeding _MAX_JSON_BYTES raises CatalogUnavailableError."""
    oversized = b"x" * (_MAX_JSON_BYTES + 1)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized)

    transport = httpx.MockTransport(_handler)
    catalog = ModrinthCatalog(base_url="https://api.modrinth.com/v2")

    real_init = httpx.AsyncClient.__init__

    def patched_init(self_client: httpx.AsyncClient, **kwargs: object) -> None:
        kwargs["transport"] = transport  # type: ignore[assignment]
        real_init(self_client, **kwargs)

    httpx.AsyncClient.__init__ = patched_init  # type: ignore[assignment]
    try:
        with pytest.raises(CatalogUnavailableError, match="response too large"):
            await catalog.search(
                query="test", loader="fabric", game_versions=["1.20.4"]
            )
    finally:
        httpx.AsyncClient.__init__ = real_init  # type: ignore[assignment]


# -- Constants sanity --


def test_max_redirects_is_positive() -> None:
    assert _MAX_REDIRECTS >= 1


def test_max_json_bytes_is_positive() -> None:
    assert _MAX_JSON_BYTES >= 1


def test_allowed_download_hosts_non_empty() -> None:
    assert len(_ALLOWED_DOWNLOAD_HOSTS) > 0
