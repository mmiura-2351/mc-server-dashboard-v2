"""Trusted-proxy client-IP resolution (SECURITY.md Section 4, CONFIG 7.3).

A forwarded-for header is attacker-controlled unless it arrives from a proxy the
operator runs, so the per-IP brute-force counter must not trust it blindly. This
resolver returns the client IP the counter keys on:

- with ``trust_forwarded_headers`` off, always the immediate peer;
- with it on, and *only* when the immediate peer is on the ``trusted_proxies``
  allow-list (IPs/CIDRs), the right-most ``X-Forwarded-For`` entry that is not
  itself a trusted proxy (the real hop just before our trusted edge); otherwise
  the peer.

The right-most-untrusted hop is the spoof-resistant choice: an attacker can
prepend arbitrary entries to ``X-Forwarded-For``, but our trusted proxy appends
the real peer on the right, so walking from the right and skipping our own
trusted proxies lands on the address the attacker could not forge.

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
    forwarded = _rightmost_untrusted(forwarded_for, trusted_proxies)
    return forwarded if forwarded is not None else peer_ip


def forwarded_for_header(headers: object) -> str | None:
    """Read the ``X-Forwarded-For`` value from a mapping-like headers object."""

    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    value = getter(_FORWARDED_FOR_HEADER)
    return value if isinstance(value, str) else None


def _rightmost_untrusted(
    forwarded_for: str | None, trusted_proxies: Sequence[str]
) -> str | None:
    """The first ``X-Forwarded-For`` hop, from the right, that is not our proxy.

    Trusted proxies append the genuine peer on the right, so walking right-to-left
    past our own trusted proxies yields the closest hop we did not append — the
    address an attacker cannot spoof by prepending entries.
    """

    if not forwarded_for:
        return None
    for entry in reversed(forwarded_for.split(",")):
        hop = entry.strip()
        if not hop:
            continue
        if not _peer_is_trusted(hop, trusted_proxies):
            return hop
    return None


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
