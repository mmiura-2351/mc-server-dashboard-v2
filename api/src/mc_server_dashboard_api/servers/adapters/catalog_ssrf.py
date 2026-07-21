"""Shared SSRF helpers for catalog download adapters (issue #2155).

Wraps the :mod:`versions.adapters.ssrf_guard` pinning primitives with
catalog-specific policy: host-allowlist enforcement and redirect-location
resolution. Both :class:`ModrinthCatalog` and :class:`GeyserMcCatalog` delegate
their per-hop validation here instead of maintaining independent copies.
"""

from __future__ import annotations

from urllib.parse import urljoin, urlparse

from mc_server_dashboard_api.servers.domain.errors import CatalogUnavailableError
from mc_server_dashboard_api.versions.adapters.ssrf_guard import (
    BlockedHostError,
    PinnedRequest,
    assert_url_allowed,
)


async def pin_download_url(
    url: str,
    allowed_hosts: frozenset[str],
    *,
    redirect: bool = False,
) -> PinnedRequest:
    """Validate *url* against *allowed_hosts* and return a pinned request.

    Checks HTTPS scheme and hostname allowlist before delegating to
    :func:`assert_url_allowed` for DNS resolution and IP pinning. Maps
    :class:`BlockedHostError` to :class:`CatalogUnavailableError`.

    *redirect* controls the error message prefix (``"redirect to ..."`` vs
    ``"download URL ..."``).
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        prefix = "redirect to non-HTTPS" if redirect else "download URL must use HTTPS"
        raise CatalogUnavailableError(f"{prefix}: {url}")
    if parsed.hostname not in allowed_hosts:
        prefix = (
            "redirect to disallowed host"
            if redirect
            else "download URL host not allowed"
        )
        raise CatalogUnavailableError(f"{prefix}: {parsed.hostname}")
    try:
        return await assert_url_allowed(url)
    except BlockedHostError as exc:
        raise CatalogUnavailableError(str(exc)) from exc


def next_logical_url(location: str, current_logical_url: str) -> str:
    """Resolve a redirect *location* against *current_logical_url*.

    Relative ``Location`` headers are resolved against the current URL's
    origin; absolute locations are used as-is. The result is the next
    *logical* (hostname-bearing) URL for allowlist + pinning validation.
    """
    parsed = urlparse(location)
    if not parsed.scheme:
        return urljoin(current_logical_url, location)
    return location
