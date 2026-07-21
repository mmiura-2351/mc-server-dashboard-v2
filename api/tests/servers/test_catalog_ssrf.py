"""Tests for the shared catalog SSRF helper (issue #2155).

Thin coverage for the catalog-specific wrapping logic — allowlist rejection
and redirect-vs-initial error messages. The underlying IP validation is
exercised in ``tests/versions/test_ssrf_guard.py``.
"""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.servers.adapters.catalog_ssrf import (
    next_logical_url,
    pin_download_url,
)
from mc_server_dashboard_api.servers.domain.errors import CatalogUnavailableError

_ALLOWED = frozenset({"cdn.example.com"})


# -- pin_download_url: allowlist + error-message tests --


async def test_pin_rejects_non_https() -> None:
    with pytest.raises(CatalogUnavailableError, match="HTTPS"):
        await pin_download_url("http://cdn.example.com/f.jar", _ALLOWED)


async def test_pin_rejects_disallowed_host() -> None:
    with pytest.raises(CatalogUnavailableError, match="host not allowed"):
        await pin_download_url("https://evil.example.com/f.jar", _ALLOWED)


async def test_pin_redirect_non_https_message() -> None:
    """redirect=True uses the redirect-specific error prefix."""
    with pytest.raises(CatalogUnavailableError, match="redirect to non-HTTPS"):
        await pin_download_url("http://cdn.example.com/f.jar", _ALLOWED, redirect=True)


async def test_pin_redirect_disallowed_host_message() -> None:
    """redirect=True uses the redirect-specific error prefix."""
    with pytest.raises(CatalogUnavailableError, match="redirect to disallowed host"):
        await pin_download_url(
            "https://evil.example.com/f.jar", _ALLOWED, redirect=True
        )


# -- next_logical_url --


def test_next_logical_url_absolute() -> None:
    result = next_logical_url(
        "https://other.example.com/b", "https://cdn.example.com/a"
    )
    assert result == "https://other.example.com/b"


def test_next_logical_url_relative() -> None:
    result = next_logical_url("/new/path", "https://cdn.example.com/old/path")
    assert result == "https://cdn.example.com/new/path"
