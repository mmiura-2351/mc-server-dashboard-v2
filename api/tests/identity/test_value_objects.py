"""Unit tests for the identity value objects (pure, no I/O)."""

import pytest

from mc_server_dashboard_api.identity.domain.errors import (
    InvalidEmailError,
    InvalidUsernameError,
)
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    Username,
)


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


# --- Write caps (issue #3689) ------------------------------------------------
#
# The caps apply when a value is written (created or changed), not when one is
# constructed: the constructor also builds lookups and rows stored before the
# caps existed, which may be longer.


def _email(length: int) -> str:
    return "a" * (length - len("@example.com")) + "@example.com"


def test_username_at_64_characters_is_writable() -> None:
    Username("a" * 64).ensure_writable()


def test_username_past_64_characters_is_not_writable() -> None:
    with pytest.raises(InvalidUsernameError):
        Username("a" * 65).ensure_writable()


def test_username_cap_counts_the_trimmed_name() -> None:
    Username("  " + "a" * 64 + "  ").ensure_writable()


def test_username_past_the_cap_still_constructs() -> None:
    assert Username("a" * 65).value == "a" * 65


def test_email_at_254_characters_is_writable() -> None:
    EmailAddress(_email(254)).ensure_writable()


def test_email_past_254_characters_is_not_writable() -> None:
    with pytest.raises(InvalidEmailError):
        EmailAddress(_email(255)).ensure_writable()


def test_email_cap_counts_the_trimmed_address() -> None:
    EmailAddress("  " + _email(254) + "  ").ensure_writable()


def test_email_past_the_cap_still_constructs() -> None:
    assert EmailAddress(_email(255)).value == _email(255)
