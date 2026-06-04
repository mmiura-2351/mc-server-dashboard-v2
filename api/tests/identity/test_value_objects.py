"""Unit tests for the identity value objects (pure, no I/O)."""

import uuid

import pytest

from mc_server_dashboard_api.identity.domain.errors import (
    InvalidEmailError,
    InvalidUsernameError,
)
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    RefreshTokenId,
    UserId,
    Username,
)


def test_user_id_new_is_unique() -> None:
    assert UserId.new() != UserId.new()


def test_user_id_wraps_a_uuid() -> None:
    raw = uuid.uuid4()
    assert UserId(raw).value == raw


def test_refresh_token_id_new_is_unique() -> None:
    assert RefreshTokenId.new() != RefreshTokenId.new()


def test_username_must_not_be_blank() -> None:
    with pytest.raises(InvalidUsernameError):
        Username("   ")


def test_username_trims_surrounding_whitespace() -> None:
    assert Username("  alice  ").value == "alice"


def test_username_equality_is_case_insensitive() -> None:
    # Uniqueness on username is case-insensitive (DATABASE.md Section 4); the
    # value object normalizes the comparison key while preserving display case.
    assert Username("Alice") == Username("alice")
    assert Username("Alice").value == "Alice"


def test_username_key_is_case_folded() -> None:
    # Brute-force state keys on this folded form so spelling variants aggregate.
    assert Username("Alice").key == "alice"
    assert Username("Alice").key == Username("ALICE").key


def test_email_must_contain_an_at_sign() -> None:
    with pytest.raises(InvalidEmailError):
        EmailAddress("not-an-email")


def test_email_is_normalized_to_lowercase() -> None:
    assert EmailAddress("Alice@Example.COM").value == "alice@example.com"


def test_email_equality_ignores_case() -> None:
    assert EmailAddress("Alice@Example.com") == EmailAddress("alice@example.com")
