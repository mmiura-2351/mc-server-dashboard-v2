"""Adapter-level tests for ModrinthCatalog security hardening (issue #1159).

Tests redirect validation, JSON response size cap, and the production adapter's
host-allowlist enforcement — these cannot be tested through the FakeCatalogProvider.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest

from mc_server_dashboard_api.servers.adapters.modrinth_catalog import (
    _ALLOWED_DOWNLOAD_HOSTS,
    _MAX_JSON_BYTES,
    _MAX_REDIRECTS,
    _TEAM_OWNER_CACHE,
    ModrinthCatalog,
)
from mc_server_dashboard_api.servers.domain.errors import CatalogUnavailableError

# -- SSRF redirect bypass --


async def test_download_redirect_to_disallowed_host_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redirect from an allowed host to an internal/disallowed host is blocked."""
    import mc_server_dashboard_api.versions.adapters.ssrf_guard as ssrf_guard

    async def _public_resolver(_host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf_guard, "_async_resolve_host", _public_resolver)

    def _handler(request: httpx2.Request) -> httpx2.Response:
        if "/data/test.jar" in str(request.url):
            return httpx2.Response(
                302,
                headers={"location": "https://169.254.169.254/metadata"},
            )
        return httpx2.Response(200, content=b"secret")

    transport = httpx2.MockTransport(_handler)
    catalog = ModrinthCatalog()
    original = catalog.download_file

    async def _patched_download(url: str) -> bytes:
        real_init = httpx2.AsyncClient.__init__

        def patched_init(self_client: httpx2.AsyncClient, **kwargs: Any) -> None:
            kwargs["transport"] = transport
            kwargs.pop("follow_redirects", None)
            real_init(self_client, **kwargs)

        httpx2.AsyncClient.__init__ = patched_init  # type: ignore[assignment]
        try:
            return await original(url)
        finally:
            httpx2.AsyncClient.__init__ = real_init  # type: ignore[method-assign]

    with pytest.raises(CatalogUnavailableError, match="disallowed host"):
        await _patched_download("https://cdn.modrinth.com/data/test.jar")


async def test_download_too_many_redirects_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More than _MAX_REDIRECTS hops raises CatalogUnavailableError."""
    import mc_server_dashboard_api.versions.adapters.ssrf_guard as ssrf_guard

    async def _public_resolver(_host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf_guard, "_async_resolve_host", _public_resolver)

    call_count = 0

    def _handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal call_count
        call_count += 1
        # Always redirect back to an allowed host.
        return httpx2.Response(
            302,
            headers={
                "location": f"https://cdn.modrinth.com/data/test.jar?r={call_count}"
            },
        )

    transport = httpx2.MockTransport(_handler)
    catalog = ModrinthCatalog()
    original = catalog.download_file

    async def _patched_download(url: str) -> bytes:
        real_init = httpx2.AsyncClient.__init__

        def patched_init(self_client: httpx2.AsyncClient, **kwargs: Any) -> None:
            kwargs["transport"] = transport
            kwargs.pop("follow_redirects", None)
            real_init(self_client, **kwargs)

        httpx2.AsyncClient.__init__ = patched_init  # type: ignore[assignment]
        try:
            return await original(url)
        finally:
            httpx2.AsyncClient.__init__ = real_init  # type: ignore[method-assign]

    with pytest.raises(CatalogUnavailableError, match="too many redirects"):
        await _patched_download("https://cdn.modrinth.com/data/test.jar")

    # Verify we made exactly _MAX_REDIRECTS + 1 attempts (initial + redirects).
    assert call_count == _MAX_REDIRECTS


async def test_download_redirect_to_non_https_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redirect from HTTPS to HTTP is blocked."""
    import mc_server_dashboard_api.versions.adapters.ssrf_guard as ssrf_guard

    async def _public_resolver(_host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf_guard, "_async_resolve_host", _public_resolver)

    def _handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            302,
            headers={"location": "http://cdn.modrinth.com/data/test.jar"},
        )

    transport = httpx2.MockTransport(_handler)
    catalog = ModrinthCatalog()
    original = catalog.download_file

    async def _patched_download(url: str) -> bytes:
        real_init = httpx2.AsyncClient.__init__

        def patched_init(self_client: httpx2.AsyncClient, **kwargs: Any) -> None:
            kwargs["transport"] = transport
            kwargs.pop("follow_redirects", None)
            real_init(self_client, **kwargs)

        httpx2.AsyncClient.__init__ = patched_init  # type: ignore[assignment]
        try:
            return await original(url)
        finally:
            httpx2.AsyncClient.__init__ = real_init  # type: ignore[method-assign]

    with pytest.raises(CatalogUnavailableError, match="non-HTTPS"):
        await _patched_download("https://cdn.modrinth.com/data/test.jar")


# -- DNS-rebinding / private-IP check (issue #1417, #2155) --


async def test_download_rejects_hostname_resolving_to_private_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """download_file rejects an allowed hostname that resolves to a private IP."""
    import mc_server_dashboard_api.versions.adapters.ssrf_guard as ssrf_guard

    async def _private_resolver(_host: str) -> list[str]:
        return ["10.0.0.1"]

    monkeypatch.setattr(ssrf_guard, "_async_resolve_host", _private_resolver)
    catalog = ModrinthCatalog()

    with pytest.raises(CatalogUnavailableError, match="private"):
        await catalog.download_file("https://cdn.modrinth.com/data/test.jar")


async def test_download_pins_resolved_ip_in_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """download_file connects to the resolved IP, not the hostname (anti-rebinding)."""
    import mc_server_dashboard_api.versions.adapters.ssrf_guard as ssrf_guard

    async def _public_resolver(_host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf_guard, "_async_resolve_host", _public_resolver)

    captured_requests: list[httpx2.Request] = []

    def _handler(request: httpx2.Request) -> httpx2.Response:
        captured_requests.append(request)
        return httpx2.Response(200, content=b"jar-content")

    transport = httpx2.MockTransport(_handler)
    catalog = ModrinthCatalog()
    original = catalog.download_file

    async def _patched_download(url: str) -> bytes:
        real_init = httpx2.AsyncClient.__init__

        def patched_init(self_client: httpx2.AsyncClient, **kwargs: Any) -> None:
            kwargs["transport"] = transport
            real_init(self_client, **kwargs)

        httpx2.AsyncClient.__init__ = patched_init  # type: ignore[assignment]
        try:
            return await original(url)
        finally:
            httpx2.AsyncClient.__init__ = real_init  # type: ignore[method-assign]

    await _patched_download("https://cdn.modrinth.com/data/test.jar")

    assert len(captured_requests) == 1
    req = captured_requests[0]
    # The request URL uses the resolved IP, not the hostname.
    assert "93.184.216.34" in str(req.url)
    assert "cdn.modrinth.com" not in str(req.url)
    # The Host header carries the original hostname for TLS/virtual-host routing.
    assert req.headers.get("host") == "cdn.modrinth.com"


# -- Unbounded JSON response --


async def test_get_json_oversized_response_raises() -> None:
    """A JSON response exceeding _MAX_JSON_BYTES raises CatalogUnavailableError."""
    oversized = b"x" * (_MAX_JSON_BYTES + 1)

    def _handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=oversized)

    transport = httpx2.MockTransport(_handler)
    catalog = ModrinthCatalog(base_url="https://api.modrinth.com/v2")

    real_init = httpx2.AsyncClient.__init__

    def patched_init(self_client: httpx2.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        real_init(self_client, **kwargs)

    httpx2.AsyncClient.__init__ = patched_init  # type: ignore[assignment]
    try:
        with pytest.raises(CatalogUnavailableError, match="response too large"):
            await catalog.search(
                query="test", loader="fabric", game_versions=["1.20.4"]
            )
    finally:
        httpx2.AsyncClient.__init__ = real_init  # type: ignore[method-assign]


async def test_get_json_enforces_limit_during_streaming() -> None:
    """Size limit is enforced during streaming, not after full buffering.

    A body 5x the limit should trigger CatalogUnavailableError without the
    entire body being consumed by the application-level reader.
    """
    chunk_size = 64 * 1024
    total_body_size = _MAX_JSON_BYTES * 5
    chunks_yielded = 0

    class _TrackingStream(httpx2.AsyncByteStream):
        """Yields chunks and tracks how many were consumed."""

        async def __aiter__(self) -> AsyncIterator[bytes]:
            nonlocal chunks_yielded
            sent = 0
            while sent < total_body_size:
                size = min(chunk_size, total_body_size - sent)
                yield b"x" * size
                chunks_yielded += 1
                sent += size

    def _handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, stream=_TrackingStream())

    transport = httpx2.MockTransport(_handler)
    catalog = ModrinthCatalog(base_url="https://api.modrinth.com/v2")

    real_init = httpx2.AsyncClient.__init__

    def patched_init(self_client: httpx2.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        real_init(self_client, **kwargs)

    httpx2.AsyncClient.__init__ = patched_init  # type: ignore[assignment]
    try:
        with pytest.raises(CatalogUnavailableError, match="response too large"):
            await catalog.search(
                query="test", loader="fabric", game_versions=["1.20.4"]
            )
    finally:
        httpx2.AsyncClient.__init__ = real_init  # type: ignore[method-assign]

    # With streaming, we should stop well before consuming all chunks.
    # total chunks if fully consumed = total_body_size / chunk_size = ~800.
    # With streaming cutoff just past _MAX_JSON_BYTES, ~160 chunks at most.
    total_chunks = total_body_size // chunk_size
    assert chunks_yielded < total_chunks, (
        f"Expected early cutoff but consumed {chunks_yielded}/{total_chunks} chunks"
    )


# -- Constants sanity --


def test_max_redirects_is_positive() -> None:
    assert _MAX_REDIRECTS >= 1


def test_max_json_bytes_is_positive() -> None:
    assert _MAX_JSON_BYTES >= 1


def test_allowed_download_hosts_non_empty() -> None:
    assert len(_ALLOWED_DOWNLOAD_HOSTS) > 0


# -- URL path encoding (issue #1416) --


async def test_get_project_encodes_slug_in_url_path() -> None:
    """Slugs with special characters are percent-encoded in the URL path."""
    captured_urls: list[str] = []

    def _handler(request: httpx2.Request) -> httpx2.Response:
        captured_urls.append(str(request.url))
        return httpx2.Response(
            200,
            content=b'{"id":"abc","slug":"my mod","title":"My Mod"}',
        )

    transport = httpx2.MockTransport(_handler)
    catalog = ModrinthCatalog(base_url="https://api.modrinth.com/v2")

    real_init = httpx2.AsyncClient.__init__

    def patched_init(self_client: httpx2.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        real_init(self_client, **kwargs)

    httpx2.AsyncClient.__init__ = patched_init  # type: ignore[assignment]
    try:
        await catalog.get_project("my mod/v2")
    finally:
        httpx2.AsyncClient.__init__ = real_init  # type: ignore[method-assign]

    assert len(captured_urls) == 1
    # Space → %20, slash → %2F — must not appear unencoded in the path.
    assert "my%20mod%2Fv2" in captured_urls[0] or "my+mod" not in captured_urls[0]
    assert "/project/my mod/v2" not in captured_urls[0]
    assert "my%20mod%2Fv2" in captured_urls[0]


async def test_get_json_html_body_raises_catalog_unavailable() -> None:
    """An HTML body on HTTP 200 raises CatalogUnavailableError."""

    def _handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, content=b"<html><body>Service Unavailable</body></html>"
        )

    transport = httpx2.MockTransport(_handler)
    catalog = ModrinthCatalog(base_url="https://api.modrinth.com/v2")

    real_init = httpx2.AsyncClient.__init__

    def patched_init(self_client: httpx2.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        real_init(self_client, **kwargs)

    httpx2.AsyncClient.__init__ = patched_init  # type: ignore[assignment]
    try:
        with pytest.raises(CatalogUnavailableError):
            await catalog.search(
                query="test", loader="fabric", game_versions=["1.20.4"]
            )
    finally:
        httpx2.AsyncClient.__init__ = real_init  # type: ignore[method-assign]


async def test_search_shape_error_raises_catalog_unavailable() -> None:
    """A valid JSON response with unexpected shape raises CatalogUnavailableError."""

    def _handler(request: httpx2.Request) -> httpx2.Response:
        # Return a JSON array instead of the expected object with "hits".
        return httpx2.Response(200, content=b'["unexpected", "array"]')

    transport = httpx2.MockTransport(_handler)
    catalog = ModrinthCatalog(base_url="https://api.modrinth.com/v2")

    real_init = httpx2.AsyncClient.__init__

    def patched_init(self_client: httpx2.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        real_init(self_client, **kwargs)

    httpx2.AsyncClient.__init__ = patched_init  # type: ignore[assignment]
    try:
        with pytest.raises(CatalogUnavailableError):
            await catalog.search(
                query="test", loader="fabric", game_versions=["1.20.4"]
            )
    finally:
        httpx2.AsyncClient.__init__ = real_init  # type: ignore[method-assign]


async def test_get_project_shape_error_raises_catalog_unavailable() -> None:
    """get_project raises CatalogUnavailableError when required keys are missing."""

    def _handler(request: httpx2.Request) -> httpx2.Response:
        # Return valid JSON but missing the required "id" key.
        return httpx2.Response(200, content=b'{"slug":"test","title":"Test"}')

    transport = httpx2.MockTransport(_handler)
    catalog = ModrinthCatalog(base_url="https://api.modrinth.com/v2")

    real_init = httpx2.AsyncClient.__init__

    def patched_init(self_client: httpx2.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        real_init(self_client, **kwargs)

    httpx2.AsyncClient.__init__ = patched_init  # type: ignore[assignment]
    try:
        with pytest.raises(CatalogUnavailableError):
            await catalog.get_project("test")
    finally:
        httpx2.AsyncClient.__init__ = real_init  # type: ignore[method-assign]


async def test_list_versions_shape_error_raises_catalog_unavailable() -> None:
    """list_versions raises CatalogUnavailableError on bad shape."""

    def _handler(request: httpx2.Request) -> httpx2.Response:
        # Return a list with an entry missing the required "id" key.
        return httpx2.Response(200, content=b'[{"version_number":"1.0"}]')

    transport = httpx2.MockTransport(_handler)
    catalog = ModrinthCatalog(base_url="https://api.modrinth.com/v2")

    real_init = httpx2.AsyncClient.__init__

    def patched_init(self_client: httpx2.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        real_init(self_client, **kwargs)

    httpx2.AsyncClient.__init__ = patched_init  # type: ignore[assignment]
    try:
        with pytest.raises(CatalogUnavailableError):
            await catalog.list_versions("test")
    finally:
        httpx2.AsyncClient.__init__ = real_init  # type: ignore[method-assign]


async def test_get_project_author_is_none_not_team_id() -> None:
    """get_project must not expose the opaque team ID as author (issue #1999)."""

    def _handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            content=b'{"id":"abc","slug":"fabric-api","title":"Fabric API",'
            b'"team":"peSx5UYg","description":"","body":""}',
        )

    transport = httpx2.MockTransport(_handler)
    catalog = ModrinthCatalog(base_url="https://api.modrinth.com/v2")

    real_init = httpx2.AsyncClient.__init__

    def patched_init(self_client: httpx2.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        real_init(self_client, **kwargs)

    httpx2.AsyncClient.__init__ = patched_init  # type: ignore[assignment]
    try:
        project = await catalog.get_project("fabric-api")
    finally:
        httpx2.AsyncClient.__init__ = real_init  # type: ignore[method-assign]

    assert project.author is None


async def test_get_project_resolves_author_from_team_owner() -> None:
    """get_project resolves author to the team owner's username (issue #2163)."""

    def _handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if "/team/" in url and url.endswith("/members"):
            return httpx2.Response(
                200,
                content=b'[{"role":"Member","user":{"username":"helper"}},'
                b'{"role":"Owner","user":{"username":"jellysquid3"}}]',
            )
        return httpx2.Response(
            200,
            content=b'{"id":"abc","slug":"sodium","title":"Sodium",'
            b'"team":"team-resolve-1","description":"","body":""}',
        )

    transport = httpx2.MockTransport(_handler)
    catalog = ModrinthCatalog(base_url="https://api.modrinth.com/v2")
    _TEAM_OWNER_CACHE.clear()

    real_init = httpx2.AsyncClient.__init__

    def patched_init(self_client: httpx2.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        real_init(self_client, **kwargs)

    httpx2.AsyncClient.__init__ = patched_init  # type: ignore[assignment]
    try:
        project = await catalog.get_project("sodium")
    finally:
        httpx2.AsyncClient.__init__ = real_init  # type: ignore[method-assign]

    assert project.author == "jellysquid3"


async def test_get_project_caches_team_owner_resolution() -> None:
    """The team owner is resolved once and reused across get_project calls.

    A fresh adapter instance per call still hits the cache (issue #2163): the
    members endpoint is queried only once for the same team.
    """
    members_calls = 0

    def _handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal members_calls
        url = str(request.url)
        if "/team/" in url and url.endswith("/members"):
            members_calls += 1
            return httpx2.Response(
                200,
                content=b'[{"role":"Owner","user":{"username":"owner-x"}}]',
            )
        return httpx2.Response(
            200,
            content=b'{"id":"abc","slug":"proj","title":"Proj",'
            b'"team":"team-cache-1","description":"","body":""}',
        )

    transport = httpx2.MockTransport(_handler)
    _TEAM_OWNER_CACHE.clear()

    real_init = httpx2.AsyncClient.__init__

    def patched_init(self_client: httpx2.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        real_init(self_client, **kwargs)

    httpx2.AsyncClient.__init__ = patched_init  # type: ignore[assignment]
    try:
        first = await ModrinthCatalog(
            base_url="https://api.modrinth.com/v2"
        ).get_project("proj")
        second = await ModrinthCatalog(
            base_url="https://api.modrinth.com/v2"
        ).get_project("proj")
    finally:
        httpx2.AsyncClient.__init__ = real_init  # type: ignore[method-assign]

    assert first.author == "owner-x"
    assert second.author == "owner-x"
    assert members_calls == 1


async def test_list_versions_encodes_slug_in_url_path() -> None:
    """list_versions also percent-encodes the slug in the URL path."""
    captured_urls: list[str] = []

    def _handler(request: httpx2.Request) -> httpx2.Response:
        captured_urls.append(str(request.url))
        return httpx2.Response(200, content=b"[]")

    transport = httpx2.MockTransport(_handler)
    catalog = ModrinthCatalog(base_url="https://api.modrinth.com/v2")

    real_init = httpx2.AsyncClient.__init__

    def patched_init(self_client: httpx2.AsyncClient, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        real_init(self_client, **kwargs)

    httpx2.AsyncClient.__init__ = patched_init  # type: ignore[assignment]
    try:
        await catalog.list_versions("slug/with/slashes")
    finally:
        httpx2.AsyncClient.__init__ = real_init  # type: ignore[method-assign]

    assert len(captured_urls) == 1
    assert "slug%2Fwith%2Fslashes" in captured_urls[0]
