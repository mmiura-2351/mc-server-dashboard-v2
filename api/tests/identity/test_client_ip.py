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


def test_leftmost_entry_is_the_client() -> None:
    # X-Forwarded-For lists the original client first, then each proxy.
    assert _resolve(forwarded_for="203.0.113.7, 10.0.0.9") == _FORWARDED


def test_exact_ip_in_trusted_list() -> None:
    assert _resolve(trusted=("10.0.0.5",)) == _FORWARDED


def test_unknown_peer_returns_none() -> None:
    assert _resolve(peer_ip=None) is None


def test_blank_forwarded_falls_back_to_peer() -> None:
    assert _resolve(forwarded_for="  ") == _PEER


def test_malformed_trusted_entry_is_skipped() -> None:
    # A bad CIDR in the list is ignored, not fatal; peer stays untrusted here.
    assert _resolve(peer_ip="192.0.2.1", trusted=("not-a-cidr",)) == "192.0.2.1"
