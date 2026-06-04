"""Domain errors for the identity context.

Raised by the pure domain (value objects, entities) on invariant violations.
They carry no framework type and are translated to transport errors at the edge.
"""

from __future__ import annotations


class IdentityError(Exception):
    """Base class for identity-domain invariant violations."""


class InvalidUsernameError(IdentityError):
    """A username failed its validation rules (e.g. blank)."""


class InvalidEmailError(IdentityError):
    """An email address failed its validation rules (e.g. no ``@``)."""


class PasswordPolicyError(IdentityError):
    """A password failed the policy (SECURITY.md Section 1).

    ``reason`` names the rule that failed (a stable, machine-readable code) so
    the edge can report *which* rule failed without ever echoing the password.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UsernameAlreadyExistsError(IdentityError):
    """Registration hit the case-insensitive username uniqueness constraint."""


class EmailAlreadyExistsError(IdentityError):
    """Registration hit the email uniqueness constraint."""
