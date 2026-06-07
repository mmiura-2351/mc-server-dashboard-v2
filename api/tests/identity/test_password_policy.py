"""Unit tests for the pure password policy (SECURITY.md Section 1).

Each rule boundary is exercised against a policy built with explicit knobs and a
small in-test blocklist; the policy depends on no I/O or persistent state.
"""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.identity.domain.errors import PasswordPolicyError
from mc_server_dashboard_api.identity.domain.password_policy import (
    PRESETS,
    PasswordPolicy,
)
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
    max_bytes: int | None = None,
    require_complexity: bool = True,
    complexity_classes: int = 3,
    check_common_list: bool = True,
    forbid_user_info: bool = True,
    forbid_simple_patterns: bool = True,
    common: frozenset[str] = frozenset({"password", "letmein123456"}),
) -> PasswordPolicy:
    return PasswordPolicy(
        min_length=min_length,
        max_length=max_length,
        max_bytes=max_bytes,
        require_complexity=require_complexity,
        complexity_classes=complexity_classes,
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


def test_rejects_over_byte_cap_under_bcrypt() -> None:
    # 73 ASCII bytes exceeds the bcrypt 72-byte cap even though the char count
    # is within max_length.
    password = "Wm7!qz#Lp2vT" + "x" * 61
    assert len(password.encode("utf-8")) == 73
    assert _reason(_policy(max_bytes=72), password) == "too_long_for_bcrypt"


def test_rejects_multibyte_over_byte_cap_under_bcrypt() -> None:
    # 37 three-byte characters: 37 chars (< 72) but 111 bytes (> 72).
    password = "あ" * 37
    assert len(password) < 72
    assert len(password.encode("utf-8")) > 72
    assert _reason(_policy(max_bytes=72), password) == "too_long_for_bcrypt"


def test_accepts_at_byte_cap_under_bcrypt() -> None:
    # Exactly 72 ASCII bytes is allowed; other rules disabled to isolate the cap.
    password = "Wm7!qz#Lp2vT" + "x" * 60
    assert len(password.encode("utf-8")) == 72
    _validate(
        _policy(max_bytes=72, require_complexity=False, forbid_simple_patterns=False),
        password,
    )


def test_byte_cap_not_enforced_for_argon2() -> None:
    # max_bytes is None for argon2: a long password is bounded only by char count.
    _validate(
        _policy(require_complexity=False, forbid_simple_patterns=False),
        "Wm7!qz#Lp2vT" + "x" * 61,
    )


def test_rejects_insufficient_complexity_when_short() -> None:
    # 13 lowercase letters: only one class, under 16 chars -> fails the rule.
    assert _reason(_policy(), "abcdefghijklm") == "insufficient_complexity"


def test_accepts_three_classes() -> None:
    # upper + lower + digit (no symbol) at 12 chars satisfies "3 of 4".
    _validate(_policy(), "Kp9mZt4qWb7d")


def test_accepts_long_passphrase_without_complexity() -> None:
    # Single class but >= 16 chars satisfies the complexity-or-length rule.
    _validate(_policy(), "korvblixmpungthaz")


def test_whitespace_counts_as_a_symbol_class() -> None:
    # 12 chars, under the 16-char shortcut: lower + digit + space (as symbol)
    # gives 3 classes. Without crediting whitespace this would be 2 -> fail.
    _validate(_policy(), "korv blix m9z")


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


def test_two_classes_satisfy_relaxed_complexity() -> None:
    # lower + digit only is 2 classes: fails the default 3-of-4 requirement but
    # passes when the preset relaxes the threshold to 2.
    assert _reason(_policy(), "korvblix9zqt") == "insufficient_complexity"
    _validate(_policy(complexity_classes=2), "korvblix9zqt")


def _preset_policy(name: str) -> PasswordPolicy:
    preset = PRESETS[name]
    return _policy(
        min_length=preset.min_length,
        require_complexity=preset.require_complexity,
        complexity_classes=preset.complexity_classes,
        check_common_list=preset.check_common_list,
        forbid_user_info=preset.forbid_user_info,
        forbid_simple_patterns=preset.forbid_simple_patterns,
    )


def test_preset_thresholds_are_monotonic() -> None:
    # Each step up tightens (or keeps) the minimum length; the presets are ordered
    # low < middle < high in strictness.
    assert PRESETS["low"].min_length < PRESETS["middle"].min_length
    assert PRESETS["middle"].min_length < PRESETS["high"].min_length


def test_low_preset_is_length_only() -> None:
    policy = _preset_policy("low")
    # An 8-char single-class password passes (no complexity / pattern screen),
    # but the 7-char one is too short and the common-list screen still bites.
    _validate(policy, "korvblix")
    assert _reason(policy, "korvbl") == "too_short"
    assert _reason(policy, "password") == "common_password"


def test_middle_preset_accepts_mixed_case_and_digit() -> None:
    policy = _preset_policy("middle")
    # 10 chars, upper + lower + digit (and even just two classes) is enough.
    _validate(policy, "Korvblix9z")
    # 9 chars is one below the middle floor.
    assert _reason(policy, "Korvblix9") == "too_short"
    # Single class under 16 chars still fails the relaxed complexity rule.
    assert _reason(policy, "korvblixmz") == "insufficient_complexity"


def test_high_preset_matches_legacy_fixed_posture() -> None:
    # The high preset reproduces the historical fixed knobs exactly.
    assert PRESETS["high"].min_length == 12
    assert PRESETS["high"].require_complexity is True
    assert PRESETS["high"].complexity_classes == 3
    assert PRESETS["high"].check_common_list is True
    assert PRESETS["high"].forbid_user_info is True
    assert PRESETS["high"].forbid_simple_patterns is True
    policy = _preset_policy("high")
    _validate(policy, "Wm7!qz#Lp2vT")
    # 11 chars below the 12 floor; two classes under 16 chars fails 3-of-4.
    assert _reason(policy, "Wm7!qz#Lp2v") == "too_short"
    assert _reason(policy, "korvblix9zqt") == "insufficient_complexity"
