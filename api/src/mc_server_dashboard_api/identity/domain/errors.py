"""Domain errors for the identity context.

Raised by the pure domain (value objects, entities) on invariant violations.
They carry no framework type and are translated to transport errors at the edge.
"""

from __future__ import annotations

import uuid


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


class InvalidCredentialsError(IdentityError):
    """Login failed: unknown user or wrong password.

    Deliberately one error for both cases so the edge returns a uniform 401 and
    cannot be used to tell "no such user" from "wrong password" (SECURITY.md
    Section 2; username-enumeration defence).
    """


class InvalidAccessTokenError(IdentityError):
    """An access token failed verification (bad signature, malformed, expired)."""


class InvalidRefreshTokenError(IdentityError):
    """A presented refresh token is unknown, revoked, or expired (FR-AUTH-2)."""


class RefreshTokenReuseError(InvalidRefreshTokenError):
    """An already-rotated refresh token was presented again (token reuse).

    A subclass of :class:`InvalidRefreshTokenError` so the edge still maps it to
    the same uniform 401 (no signal that distinguishes reuse from a plain bad
    token to the client). It carries ``user_id`` so the route can attribute the
    family-revocation audit record to the affected user (a security event).
    """

    def __init__(self, user_id: "uuid.UUID") -> None:
        super().__init__()
        self.user_id = user_id
