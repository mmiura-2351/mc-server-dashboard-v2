"""Pure password-strength policy (SECURITY.md Section 1, FR-AUTH-4).

Deterministic domain logic with no persistent state and no I/O: the rules and
their knobs come from configuration (CONFIGURATION.md Section 7.1) and the
common-password blocklist is injected as data, so the policy stays in the domain
layer and is callable from the registration use case. The first failing rule
raises :class:`PasswordPolicyError` carrying a stable ``reason`` code; the
plaintext is never echoed.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.identity.domain.errors import PasswordPolicyError
from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    Username,
)

# Sequences whose 4+-long ascending or descending runs count as a simple
# pattern (SECURITY.md Section 1: sequential alphabet/keyboard/numeric runs).
_SEQUENCES = (
    "abcdefghijklmnopqrstuvwxyz",
    "0123456789",
    "qwertyuiop",
    "asdfghjkl",
    "zxcvbnm",
)
_RUN_LENGTH = 4


@dataclass(frozen=True)
class PolicyPreset:
    """The strength knobs a named preset fixes (SECURITY.md Section 1).

    A preset bundles the *strength* rules so the deployment selects a posture by
    name (``auth.password.policy``) instead of toggling each rule. It deliberately
    omits the hasher-bound ``max_bytes`` and the injected ``common_passwords``:
    those are runtime concerns supplied where the :class:`PasswordPolicy` is built.
    """

    min_length: int
    require_complexity: bool
    complexity_classes: int
    check_common_list: bool
    forbid_user_info: bool
    forbid_simple_patterns: bool


# Selectable password-strength presets. The reason codes the rules emit are
# identical across presets; a preset only changes WHICH rules fire and their
# thresholds (SECURITY.md Section 1):
#   - low:    length-only floor (8+), still screening the common-password list
#             and user-info so the weakest posture is not trivially bypassable.
#   - middle: 10+ with mixed classes (2 of 4, e.g. mixed case + digits) and the
#             simple-pattern guard; the out-of-box default.
#   - high:   the historical fixed posture (12+, 3-of-4-or-16, every screen on).
PRESETS: dict[str, PolicyPreset] = {
    "low": PolicyPreset(
        min_length=8,
        require_complexity=False,
        # Inert when require_complexity is False: the complexity check
        # short-circuits, so this 0 is never read.
        complexity_classes=0,
        check_common_list=True,
        forbid_user_info=True,
        forbid_simple_patterns=False,
    ),
    "middle": PolicyPreset(
        min_length=10,
        require_complexity=True,
        complexity_classes=2,
        check_common_list=True,
        forbid_user_info=True,
        forbid_simple_patterns=True,
    ),
    "high": PolicyPreset(
        min_length=12,
        require_complexity=True,
        complexity_classes=3,
        check_common_list=True,
        forbid_user_info=True,
        forbid_simple_patterns=True,
    ),
}


@dataclass(frozen=True)
class PasswordPolicy:
    """The configured password rules; :meth:`validate` enforces them in order."""

    min_length: int
    max_length: int
    # Upper bound on the UTF-8 byte length, set to 72 when the configured hasher
    # is bcrypt (which ignores bytes past 72); ``None`` for argon2, which has no
    # such cap. Enforced here so the bcrypt adapter never truncates silently.
    max_bytes: int | None
    require_complexity: bool
    # Minimum number of distinct character classes ({upper, lower, digit, symbol})
    # the complexity rule demands when the 16-char length shortcut is not met.
    # Only consulted while ``require_complexity`` is set.
    complexity_classes: int
    check_common_list: bool
    forbid_user_info: bool
    forbid_simple_patterns: bool
    # Common passwords, stored case-folded for case-insensitive membership.
    common_passwords: frozenset[str]

    def validate(
        self, password: str, *, username: Username, email: EmailAddress
    ) -> None:
        """Raise :class:`PasswordPolicyError` for the first rule the password fails."""

        if len(password) < self.min_length:
            raise PasswordPolicyError("too_short")
        if len(password) > self.max_length:
            raise PasswordPolicyError("too_long")
        if (
            self.max_bytes is not None
            and len(password.encode("utf-8")) > self.max_bytes
        ):
            raise PasswordPolicyError("too_long_for_bcrypt")
        if self.require_complexity and not _has_complexity_or_length(
            password, self.complexity_classes
        ):
            raise PasswordPolicyError("insufficient_complexity")
        if self.check_common_list and password.casefold() in self.common_passwords:
            raise PasswordPolicyError("common_password")
        if self.forbid_user_info and _contains_user_info(password, username, email):
            raise PasswordPolicyError("contains_user_info")
        if self.forbid_simple_patterns and _has_simple_pattern(password):
            raise PasswordPolicyError("simple_pattern")


def _has_complexity_or_length(password: str, required_classes: int) -> bool:
    """At least ``required_classes`` of {upper, lower, digit, symbol}, or 16+ chars.

    Whitespace counts toward the symbol class so passphrases with spaces get the
    credit they deserve.
    """

    if len(password) >= 16:
        return True
    classes = 0
    if any(c.isupper() for c in password):
        classes += 1
    if any(c.islower() for c in password):
        classes += 1
    if any(c.isdigit() for c in password):
        classes += 1
    if any(not c.isalnum() for c in password):
        classes += 1
    return classes >= required_classes


def _contains_user_info(password: str, username: Username, email: EmailAddress) -> bool:
    """Whether the password contains the username or the email local-part."""

    folded = password.casefold()
    local_part = email.value.partition("@")[0]
    return username.value.casefold() in folded or local_part.casefold() in folded


def _has_simple_pattern(password: str) -> bool:
    """4+ repeated characters, or a 4+-long sequential run."""

    return _has_repeated_run(password) or _has_sequential_run(password)


def _has_repeated_run(password: str) -> bool:
    run = 1
    for prev, curr in zip(password, password[1:]):
        run = run + 1 if curr == prev else 1
        if run >= _RUN_LENGTH:
            return True
    return False


def _has_sequential_run(password: str) -> bool:
    lowered = password.casefold()
    for window_start in range(len(lowered) - _RUN_LENGTH + 1):
        window = lowered[window_start : window_start + _RUN_LENGTH]
        if any(window in seq or window in seq[::-1] for seq in _SEQUENCES):
            return True
    return False
