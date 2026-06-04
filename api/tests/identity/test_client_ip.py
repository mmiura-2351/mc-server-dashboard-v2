"""Tests for trusted-proxy client-IP resolution (SECURITY.md Section 4)."""

from __future__ import annotations

from mc_server_dashboard_api.identity.adapters.client_ip import resolve_client_ip

_PEER = "10.0.0.5"
_FORWARDED = "203.0.113.7"


def _resolve(
    *,
    peer_ip: str | None = _PEER,
    forwarded_for: str | None = _FORWARDED,
    trust: bool = True,
    trusted: tuple[str, ...] = ("10.0.0.0/8",),
) -> str | None:
    return resolve_client_ip(
        peer_ip=peer_ip,
        forwarded_for=forwarded_for,
        trust_forwarded_headers=trust,
        trusted_proxies=trusted,
    )


def test_forwarded_ignored_when_trust_disabled() -> None:
    assert _resolve(trust=False) == _PEER


def test_forwarded_honored_from_trusted_peer() -> None:
    assert _resolve() == _FORWARDED


def test_forwarded_ignored_from_untrusted_peer() -> None:
    # The peer is not in the allow-list, so its forwarded header is not trusted.
    assert _resolve(peer_ip="192.0.2.1") == "192.0.2.1"


def test_trusted_peer_without_forwarded_falls_back_to_peer() -> None:
    assert _resolve(forwarded_for=None) == _PEER


def test_rightmost_untrusted_hop_is_the_client() -> None:
    # X-Forwarded-For appends each hop on the right; the last entry here (a
    # trusted proxy) is skipped, leaving the real client to its left.
    assert _resolve(forwarded_for="203.0.113.7, 10.0.0.9") == _FORWARDED


def test_spoofed_left_entry_is_ignored() -> None:
    # An attacker prepends a forged entry; our trusted proxy then appends the
    # attacker's real address. Walking from the right past trusted proxies must
    # resolve to the attacker's real hop, not the forged left-most one.
    assert _resolve(forwarded_for="1.2.3.4, 203.0.113.7, 10.0.0.9") == _FORWARDED


def test_all_trusted_hops_falls_back_to_peer() -> None:
    # If every forwarded hop is a trusted proxy, there is no untrusted client to
    # trust; fall back to the immediate peer rather than a proxy address.
    assert _resolve(forwarded_for="10.0.0.9, 10.0.0.8") == _PEER


def test_exact_ip_in_trusted_list() -> None:
    assert _resolve(trusted=("10.0.0.5",)) == _FORWARDED


def test_unknown_peer_returns_none() -> None:
    assert _resolve(peer_ip=None) is None


def test_blank_forwarded_falls_back_to_peer() -> None:
    assert _resolve(forwarded_for="  ") == _PEER


def test_malformed_trusted_entry_is_skipped() -> None:
    # A bad CIDR in the list is ignored, not fatal; peer stays untrusted here.
    assert _resolve(peer_ip="192.0.2.1", trusted=("not-a-cidr",)) == "192.0.2.1"
