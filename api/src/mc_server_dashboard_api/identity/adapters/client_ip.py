"""Trusted-proxy client-IP resolution (SECURITY.md Section 4, CONFIG 7.3).

A forwarded-for header is attacker-controlled unless it arrives from a proxy the
operator runs, so the per-IP brute-force counter must not trust it blindly. This
resolver returns the client IP the counter keys on:

- with ``trust_forwarded_headers`` off, always the immediate peer;
- with it on, the left-most ``X-Forwarded-For`` entry *only* when the immediate
  peer is on the ``trusted_proxies`` allow-list (IPs/CIDRs); otherwise the peer.

This is an edge/adapter concern (it reads the transport peer and an HTTP header),
so it lives in the adapters layer and is invoked from the wiring dependency.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence

_FORWARDED_FOR_HEADER = "x-forwarded-for"


def resolve_client_ip(
    *,
    peer_ip: str | None,
    forwarded_for: str | None,
    trust_forwarded_headers: bool,
    trusted_proxies: Sequence[str],
) -> str | None:
    """Return the trustworthy client IP, or ``None`` if the peer is unknown."""

    if peer_ip is None:
        return None
    if not trust_forwarded_headers:
        return peer_ip
    if not _peer_is_trusted(peer_ip, trusted_proxies):
        return peer_ip
    forwarded = _leftmost_forwarded(forwarded_for)
    return forwarded if forwarded is not None else peer_ip


def forwarded_for_header(headers: object) -> str | None:
    """Read the ``X-Forwarded-For`` value from a mapping-like headers object."""

    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    value = getter(_FORWARDED_FOR_HEADER)
    return value if isinstance(value, str) else None


def _leftmost_forwarded(forwarded_for: str | None) -> str | None:
    """The original client is the first entry of ``X-Forwarded-For`` (RFC 7239)."""

    if not forwarded_for:
        return None
    first = forwarded_for.split(",", 1)[0].strip()
    return first or None


def _peer_is_trusted(peer_ip: str, trusted_proxies: Sequence[str]) -> bool:
    """Whether ``peer_ip`` falls in any configured trusted IP/CIDR."""

    try:
        peer = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    for entry in trusted_proxies:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if peer in network:
            return True
    return False
