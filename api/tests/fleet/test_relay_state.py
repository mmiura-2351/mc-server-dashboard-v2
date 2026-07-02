"""Unit tests for the relay in-memory state (issue #956, #1544).

The relay registration is last-writer-wins (one relay per deployment); the join
token table mints single-use 128-bit tokens. Single-use/TTL enforcement lives
relay-side (tokens.go) — the API only mints a fresh, unguessable value to carry
into ``TunnelDial`` and never validates it back (RELAY.md Sections 5, 6).

The Bedrock tunnel table (issue #1544) is the opposite shape: the API mints
*and keeps* the per-server credential, because the relay asks it to confirm the
token rather than matching it locally (RelayService.ValidateBedrockTunnel).
"""

from __future__ import annotations

from mc_server_dashboard_api.fleet.adapters.relay_state import (
    BedrockTunnelTable,
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


def test_bedrock_tunnel_open_token_is_128_bit_hex() -> None:
    token = BedrockTunnelTable().open(server_id="s1", bedrock_port=19132)
    assert len(token) == 32
    int(token, 16)  # parses as hex


def test_bedrock_tunnel_validate_matches_open_credential() -> None:
    table = BedrockTunnelTable()
    token = table.open(server_id="s1", bedrock_port=19132)
    assert table.validate(server_id="s1", bedrock_port=19132, token=token) is True


def test_bedrock_tunnel_validate_rejects_wrong_port() -> None:
    table = BedrockTunnelTable()
    token = table.open(server_id="s1", bedrock_port=19132)
    assert table.validate(server_id="s1", bedrock_port=19133, token=token) is False


def test_bedrock_tunnel_validate_rejects_wrong_token() -> None:
    table = BedrockTunnelTable()
    table.open(server_id="s1", bedrock_port=19132)
    assert table.validate(server_id="s1", bedrock_port=19132, token="wrong") is False


def test_bedrock_tunnel_validate_rejects_unknown_server() -> None:
    table = BedrockTunnelTable()
    assert table.validate(server_id="s1", bedrock_port=19132, token="anything") is False


def test_bedrock_tunnel_close_invalidates_token() -> None:
    table = BedrockTunnelTable()
    token = table.open(server_id="s1", bedrock_port=19132)
    table.close(server_id="s1")
    assert table.validate(server_id="s1", bedrock_port=19132, token=token) is False


def test_bedrock_tunnel_close_is_idempotent() -> None:
    table = BedrockTunnelTable()
    table.close(server_id="never-opened")  # must not raise


def test_bedrock_tunnel_open_replaces_prior_token() -> None:
    table = BedrockTunnelTable()
    first = table.open(server_id="s1", bedrock_port=19132)
    second = table.open(server_id="s1", bedrock_port=19133)
    assert first != second
    assert table.validate(server_id="s1", bedrock_port=19132, token=first) is False
    assert table.validate(server_id="s1", bedrock_port=19133, token=second) is True
