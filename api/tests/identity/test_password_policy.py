"""Unit tests for the pure password policy (SECURITY.md Section 1).

Each rule boundary is exercised against a policy built with explicit knobs and a
small in-test blocklist; the policy depends on no I/O or persistent state.
"""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.identity.domain.errors import PasswordPolicyError
from mc_server_dashboard_api.identity.domain.password_policy import PasswordPolicy
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    Username,
)

_USER = Username("alice")
_EMAIL = EmailAddress("alice@example.com")


def _policy(
    *,
    min_length: int = 12,
    max_length: int = 128,
    require_complexity: bool = True,
    check_common_list: bool = True,
    forbid_user_info: bool = True,
    forbid_simple_patterns: bool = True,
    common: frozenset[str] = frozenset({"password", "letmein123456"}),
) -> PasswordPolicy:
    return PasswordPolicy(
        min_length=min_length,
        max_length=max_length,
        require_complexity=require_complexity,
        check_common_list=check_common_list,
        forbid_user_info=forbid_user_info,
        forbid_simple_patterns=forbid_simple_patterns,
        common_passwords=common,
    )


def _validate(policy: PasswordPolicy, password: str) -> None:
    policy.validate(password, username=_USER, email=_EMAIL)


def _reason(policy: PasswordPolicy, password: str) -> str:
    with pytest.raises(PasswordPolicyError) as exc:
        _validate(policy, password)
    return exc.value.reason


def test_accepts_a_strong_password() -> None:
    _validate(_policy(), "Wm7!qz#Lp2vT")


def test_rejects_below_min_length() -> None:
    # 11 chars, one below the default minimum of 12.
    assert _reason(_policy(), "Wm7!qz#Lp2v") == "too_short"


def test_accepts_exactly_min_length() -> None:
    _validate(_policy(min_length=12), "Wm7!qz#Lp2vT")


def test_rejects_above_max_length() -> None:
    assert _reason(_policy(max_length=12), "Wm7!qz#Lp2vTX") == "too_long"


def test_accepts_exactly_max_length() -> None:
    _validate(_policy(max_length=12), "Wm7!qz#Lp2vT")


def test_rejects_insufficient_complexity_when_short() -> None:
    # 13 lowercase letters: only one class, under 16 chars -> fails the rule.
    assert _reason(_policy(), "abcdefghijklm") == "insufficient_complexity"


def test_accepts_three_classes() -> None:
    # upper + lower + digit (no symbol) at 12 chars satisfies "3 of 4".
    _validate(_policy(), "Kp9mZt4qWb7d")


def test_accepts_long_passphrase_without_complexity() -> None:
    # Single class but >= 16 chars satisfies the complexity-or-length rule.
    _validate(_policy(), "korvblixmpungthaz")


def test_complexity_rule_disabled() -> None:
    _validate(_policy(require_complexity=False), "korvblixmpun")


def test_rejects_common_password() -> None:
    # Long enough and multi-class but on the blocklist (stored case-folded).
    assert _reason(_policy(common=frozenset({"trustno1mkp!"})), "Trustno1MKp!") == (
        "common_password"
    )


def test_common_check_is_case_insensitive() -> None:
    assert _reason(_policy(common=frozenset({"trustno1mkp!"})), "TRUSTNO1mkp!") == (
        "common_password"
    )


def test_common_check_disabled() -> None:
    _validate(
        _policy(check_common_list=False, common=frozenset({"korvblixmpungthaz"})),
        "korvblixmpungthaz",
    )


def test_rejects_password_containing_username() -> None:
    assert _reason(_policy(), "xxAlice9mkp!") == "contains_user_info"


def test_username_check_is_case_insensitive() -> None:
    assert _reason(_policy(), "xxALICE9mkp!") == "contains_user_info"


def test_rejects_password_containing_email_local_part() -> None:
    # Email local-part is "carol"; caught even when length/complexity pass.
    user = Username("bob")
    email = EmailAddress("carol@example.com")
    policy = _policy()
    with pytest.raises(PasswordPolicyError) as exc:
        policy.validate("zzCarol9mkp!", username=user, email=email)
    assert exc.value.reason == "contains_user_info"


def test_user_info_check_disabled() -> None:
    _validate(_policy(forbid_user_info=False), "xxAlice9mkp!")


def test_rejects_repeated_characters() -> None:
    assert _reason(_policy(), "Aaaaa1!xkqwp") == "simple_pattern"


def test_rejects_sequential_run() -> None:
    assert _reason(_policy(), "Xabcd1!kqwpz") == "simple_pattern"


def test_rejects_numeric_sequential_run() -> None:
    assert _reason(_policy(), "X1234!kqwpzL") == "simple_pattern"


def test_simple_pattern_check_disabled() -> None:
    _validate(_policy(forbid_simple_patterns=False), "Aaaaa1!xkqwp")
