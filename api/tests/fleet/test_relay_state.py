"""Unit tests for the relay in-memory state (issue #956).

The relay registration is last-writer-wins (one relay per deployment); the join
token table mints single-use 128-bit tokens with a 10 s TTL, consumed on first
use, expired by the clock, and cleaned up so it never grows unbounded
(RELAY.md Sections 5, 6).
"""

from __future__ import annotations

import datetime as dt

from mc_server_dashboard_api.fleet.adapters.relay_state import (
    JoinTokenTable,
    RelayRegistration,
)
from tests.fleet.fakes import FakeClock

_T0 = dt.datetime(2026, 6, 12, 12, 0, tzinfo=dt.timezone.utc)


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
    table = JoinTokenTable(clock=FakeClock(_T0), ttl=dt.timedelta(seconds=10))
    token = table.mint(server_id="s-1")
    # 128 bits == 32 hex chars.
    assert len(token) == 32
    int(token, 16)  # parses as hex


def test_mint_tokens_are_unique() -> None:
    table = JoinTokenTable(clock=FakeClock(_T0), ttl=dt.timedelta(seconds=10))
    tokens = {table.mint(server_id="s-1") for _ in range(100)}
    assert len(tokens) == 100


def test_consume_returns_server_id_then_invalidates() -> None:
    table = JoinTokenTable(clock=FakeClock(_T0), ttl=dt.timedelta(seconds=10))
    token = table.mint(server_id="s-42")
    assert table.consume(token) == "s-42"
    # Single-use: a second consume of the same token fails.
    assert table.consume(token) is None


def test_consume_unknown_token_returns_none() -> None:
    table = JoinTokenTable(clock=FakeClock(_T0), ttl=dt.timedelta(seconds=10))
    assert table.consume("deadbeef") is None


def test_expired_token_cannot_be_consumed() -> None:
    clock = FakeClock(_T0)
    table = JoinTokenTable(clock=clock, ttl=dt.timedelta(seconds=10))
    token = table.mint(server_id="s-1")
    clock.set(_T0 + dt.timedelta(seconds=11))
    assert table.consume(token) is None


def test_token_still_valid_at_ttl_boundary() -> None:
    clock = FakeClock(_T0)
    table = JoinTokenTable(clock=clock, ttl=dt.timedelta(seconds=10))
    token = table.mint(server_id="s-1")
    clock.set(_T0 + dt.timedelta(seconds=10))
    assert table.consume(token) == "s-1"


def test_minting_prunes_expired_entries() -> None:
    clock = FakeClock(_T0)
    table = JoinTokenTable(clock=clock, ttl=dt.timedelta(seconds=10))
    stale = table.mint(server_id="s-old")
    clock.set(_T0 + dt.timedelta(seconds=11))
    # Minting a fresh token sweeps expired entries so the table never grows
    # unbounded under a flood of never-consumed tokens.
    table.mint(server_id="s-new")
    assert table.size() == 1
    # The pruned token is gone regardless of the clock.
    clock.set(_T0)
    assert table.consume(stale) is None
