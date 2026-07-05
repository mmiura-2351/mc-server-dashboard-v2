"""SSRF guard for the versions catalog fetchers (issue #1598).

Mirrors the private-address guard the Modrinth download path enforces
(``servers/adapters/modrinth_catalog.py``) so every outbound catalog fetch
shares one policy. Applied to the shared JAR/JSON fetchers, a fetch is refused
unless the URL uses HTTPS and its host resolves only to global (public)
addresses — blocking a compromised or future user-influenced catalog URL from
reaching a private/loopback/link-local target (and DNS-rebinding to one).

No host allowlist: the upstream bases (Mojang / PaperMC / Fabric / Forge) and
their download CDNs are public but not a closed, enumerable set, so an allowlist
would break version installs. Scheme + private-IP is the whole policy.

These fetchers do not follow redirects (httpx defaults ``follow_redirects`` to
``False`` and neither fetcher overrides it), so validating the single requested
URL is sufficient — there are no redirect hops to re-validate the way the
Modrinth download path does.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlparse


class BlockedHostError(Exception):
    """A URL was refused by the SSRF guard (non-HTTPS or a private/reserved IP)."""


def _default_resolve_host(hostname: str) -> list[str]:
    """Resolve *hostname* to a list of IP address strings via DNS."""
    results = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    return list({str(addr[4][0]) for addr in results})


_resolve_host: Callable[[str], list[str]] = _default_resolve_host


def assert_url_allowed(
    url: str,
    *,
    _resolver: Callable[[str], list[str]] | None = None,
) -> None:
    """Raise :class:`BlockedHostError` unless *url* is HTTPS to a public host.

    The URL must use HTTPS and its hostname must not resolve to a private,
    loopback, link-local, or otherwise non-global address (``ip.is_global``),
    which also blocks a hostname that rebinds to an internal target.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise BlockedHostError(f"URL must use HTTPS: {url}")
    hostname = parsed.hostname
    if not hostname:
        raise BlockedHostError(f"URL has no host: {url}")
    resolver = _resolver or _resolve_host
    try:
        addrs = resolver(hostname)
    except (socket.gaierror, OSError) as exc:
        raise BlockedHostError(f"DNS resolution failed for {hostname}") from exc
    if not addrs:
        raise BlockedHostError(f"DNS resolution for {hostname!r} returned no addresses")
    for addr in addrs:
        ip = ipaddress.ip_address(addr)
        if not ip.is_global:
            raise BlockedHostError(
                f"host {hostname} resolved to private/reserved IP: {addr}"
            )
