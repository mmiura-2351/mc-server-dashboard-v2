"""Unit tests for the relay in-memory state (issue #956).

The relay registration is last-writer-wins (one relay per deployment); the join
token table mints single-use 128-bit tokens. Single-use/TTL enforcement lives
relay-side (tokens.go) — the API only mints a fresh, unguessable value to carry
into ``TunnelDial`` and never validates it back (RELAY.md Sections 5, 6).
"""

from __future__ import annotations

from mc_server_dashboard_api.fleet.adapters.relay_state import (
    JoinTokenTable,
    RelayRegistration,
)


def test_registration_starts_empty() -> None:
    registration = RelayRegistration()
    assert registration.current() is None


def test_register_stores_endpoint_and_ca() -> None:
    registration = RelayRegistration()
    registration.set(endpoint="relay.example.com:25665", ca_pem="CA-PEM")
    stored = registration.current()
    assert stored is not None
    assert stored.endpoint == "relay.example.com:25665"
    assert stored.ca_pem == "CA-PEM"


def test_register_is_last_writer_wins() -> None:
    registration = RelayRegistration()
    registration.set(endpoint="old:1", ca_pem="old-ca")
    registration.set(endpoint="new:2", ca_pem="new-ca")
    stored = registration.current()
    assert stored is not None
    assert stored.endpoint == "new:2"
    assert stored.ca_pem == "new-ca"


def test_mint_token_is_128_bit_hex() -> None:
    token = JoinTokenTable().mint()
    # 128 bits == 32 hex chars.
    assert len(token) == 32
    int(token, 16)  # parses as hex


def test_mint_tokens_are_unique() -> None:
    table = JoinTokenTable()
    tokens = {table.mint() for _ in range(100)}
    assert len(tokens) == 100
