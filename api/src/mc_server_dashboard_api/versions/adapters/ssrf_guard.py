"""SSRF guard for the versions catalog fetchers (issue #1598).

Mirrors the private-address guard the Modrinth download path enforces
(``servers/adapters/modrinth_catalog.py``) so every outbound catalog fetch
shares one policy. Applied to the shared JAR/JSON fetchers, a fetch is refused
unless the URL uses HTTPS and its host resolves only to global (public)
addresses — blocking a compromised or future user-influenced catalog URL from
reaching a private/loopback/link-local target.

DNS-rebinding is defeated by pinning the resolved IP into the request URL so
the subsequent HTTP client connection goes to the already-validated address
rather than re-resolving the hostname (issue #1989).

No host allowlist: the upstream bases (Mojang / PaperMC / Fabric / Forge) and
their download CDNs are public but not a closed, enumerable set, so an allowlist
would break version installs. Scheme + private-IP is the whole policy.

These fetchers do not follow redirects (httpx2 defaults ``follow_redirects`` to
``False`` and neither fetcher overrides it), so validating the single requested
URL is sufficient — there are no redirect hops to re-validate the way the
Modrinth download path does.
"""

from __future__ import annotations

import asyncio
import dataclasses
import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlparse, urlunparse


class BlockedHostError(Exception):
    """A URL was refused by the SSRF guard (non-HTTPS or a private/reserved IP)."""


@dataclasses.dataclass(frozen=True, slots=True)
class PinnedRequest:
    """A validated request with the hostname replaced by its resolved IP.

    Callers pass *url*, *headers*, and *extensions* to the HTTP client so the
    connection goes to the already-validated address (no second DNS lookup) while
    TLS SNI and the ``Host`` header still carry the original hostname.
    """

    url: str
    headers: dict[str, str]
    extensions: dict[str, str]


async def _async_resolve_host(hostname: str) -> list[str]:
    """Resolve *hostname* without blocking the event loop.

    Uses ``loop.getaddrinfo``, which delegates to the executor internally.
    """
    loop = asyncio.get_running_loop()
    results = await loop.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    return list({str(addr[4][0]) for addr in results})


async def assert_url_allowed(
    url: str,
    *,
    _resolver: Callable[[str], list[str]] | None = None,
) -> PinnedRequest:
    """Validate *url* and return a :class:`PinnedRequest` pinned to the resolved IP.

    The URL must use HTTPS and its hostname must not resolve to a private,
    loopback, link-local, or otherwise non-global address (``ip.is_global``).

    The returned :class:`PinnedRequest` replaces the hostname with the resolved
    IP so callers connect to the validated address, closing the DNS-rebinding
    TOCTOU window (issue #1989).
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise BlockedHostError(f"URL must use HTTPS: {url}")
    hostname = parsed.hostname
    if not hostname:
        raise BlockedHostError(f"URL has no host: {url}")
    try:
        if _resolver is not None:
            addrs = _resolver(hostname)
        else:
            addrs = await _async_resolve_host(hostname)
    except (socket.gaierror, OSError) as exc:
        raise BlockedHostError(f"DNS resolution failed for {hostname}") from exc
    if not addrs:
        raise BlockedHostError(f"DNS resolution for {hostname} returned no addresses")
    for addr in addrs:
        ip = ipaddress.ip_address(addr)
        if not ip.is_global:
            raise BlockedHostError(
                f"host {hostname} resolved to private/reserved IP: {addr}"
            )

    # Pin to the first IPv4 address; fall back to the first IPv6 if none.
    pinned_addr = next((a for a in addrs if ":" not in a), addrs[0])
    host = f"[{pinned_addr}]" if ":" in pinned_addr else pinned_addr
    netloc = host if parsed.port is None else f"{host}:{parsed.port}"
    return PinnedRequest(
        url=urlunparse(parsed._replace(netloc=netloc)),
        headers={"Host": hostname},
        extensions={"sni_hostname": hostname},
    )
