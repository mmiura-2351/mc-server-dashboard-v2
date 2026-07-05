"""SSRF guard for the versions fetchers: HTTPS-only + private-IP refusal (#1598).

Mirrors the Modrinth guard tests (``tests/servers/test_modrinth_catalog_adapter``):
a public address passes, private/loopback/link-local/CGNAT/IPv6-loopback are
refused, a non-HTTPS URL is refused, and DNS failure surfaces as a blocked host.
The resolver is injected so the tests never touch the network.
"""

from __future__ import annotations

import socket
from collections.abc import Callable

import pytest

from mc_server_dashboard_api.versions.adapters.ssrf_guard import (
    BlockedHostError,
    assert_url_allowed,
)

_PUBLIC = "https://example.com/server.jar"


def _resolver_for(*addrs: str) -> Callable[[str], list[str]]:
    """Return a resolver callback that yields the given addresses."""

    def _resolve(_: str) -> list[str]:
        return list(addrs)

    return _resolve


def test_allows_public_host() -> None:
    """An HTTPS URL whose host resolves to a public IP passes."""
    assert_url_allowed(_PUBLIC, _resolver=_resolver_for("93.184.216.34"))


def test_rejects_loopback() -> None:
    with pytest.raises(BlockedHostError, match="private"):
        assert_url_allowed(_PUBLIC, _resolver=_resolver_for("127.0.0.1"))


def test_rejects_rfc1918() -> None:
    with pytest.raises(BlockedHostError, match="private"):
        assert_url_allowed(_PUBLIC, _resolver=_resolver_for("192.168.1.1"))


def test_rejects_link_local() -> None:
    with pytest.raises(BlockedHostError, match="private"):
        assert_url_allowed(_PUBLIC, _resolver=_resolver_for("169.254.169.254"))


def test_rejects_ipv6_loopback() -> None:
    with pytest.raises(BlockedHostError, match="private"):
        assert_url_allowed(_PUBLIC, _resolver=_resolver_for("::1"))


def test_rejects_cgnat() -> None:
    with pytest.raises(BlockedHostError, match="private"):
        assert_url_allowed(_PUBLIC, _resolver=_resolver_for("100.64.0.1"))


def test_rejects_if_any_addr_private() -> None:
    """If any resolved address is private, the check fails."""
    with pytest.raises(BlockedHostError, match="private"):
        assert_url_allowed(
            _PUBLIC, _resolver=_resolver_for("93.184.216.34", "10.0.0.1")
        )


def test_rejects_non_https() -> None:
    with pytest.raises(BlockedHostError, match="HTTPS"):
        assert_url_allowed(
            "http://example.com/server.jar", _resolver=_resolver_for("93.184.216.34")
        )


def test_rejects_dns_failure() -> None:
    def _boom(_: str) -> list[str]:
        raise socket.gaierror("no such host")

    with pytest.raises(BlockedHostError, match="DNS resolution failed"):
        assert_url_allowed(_PUBLIC, _resolver=_boom)


def test_rejects_empty_resolver_result() -> None:
    """An empty resolver result must fail closed, not silently pass."""
    with pytest.raises(BlockedHostError, match="returned no addresses"):
        assert_url_allowed(_PUBLIC, _resolver=_resolver_for())
