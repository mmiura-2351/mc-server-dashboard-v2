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


class RegistrationDisabledError(IdentityError):
    """Open self-registration is turned off by the operator (issue #362).

    A private deployment provisions accounts through the admin surface only; the
    edge maps this to 403. Admin-created accounts are unaffected.
    """


class RegistrationThrottledError(IdentityError):
    """Too many registrations from one source IP within the window (issue #362).

    The per-IP sliding-window cap (reusing FR-AUTH-4's machinery) was crossed; the
    edge maps this to 429 so a script cannot flood the user table.
    """


class InvalidCredentialsError(IdentityError):
    """Login failed: unknown user or wrong password.

    Deliberately one error for both cases so the edge returns a uniform 401 and
    cannot be used to tell "no such user" from "wrong password" (SECURITY.md
    Section 2; username-enumeration defence).

    ``retry_after`` optionally carries how many seconds until the client should
    retry (RFC 6585 ``Retry-After``); set when the rejection is due to a lockout
    or IP throttle, ``None`` for a plain wrong-password or unknown-user failure
    (issue #637). The value never leaks the *reason* for the rejection — only the
    timing hint.
    """

    def __init__(self, retry_after: int | None = None) -> None:
        super().__init__()
        self.retry_after = retry_after


class UserNotFoundError(IdentityError):
    """A user referenced by id was not found (e.g. deleted concurrently)."""


class CommunityOwnedError(IdentityError):
    """Self-deletion refused: the user still owns at least one community.

    A community owner cannot delete their account because doing so would orphan
    the community (its sole administrator would vanish). The user must transfer
    ownership or have the community deleted first (FR-COMM-4).
    """


class LastPlatformAdminError(IdentityError):
    """Self-deletion refused: the user is the last platform administrator.

    The platform must always retain at least one administrator, so the final one
    cannot delete their own account (FR-AUTH-6).
    """


class SelfTargetError(IdentityError):
    """An admin lifecycle action refused because the actor targeted themselves.

    The platform-admin user-administration routes (issue #278) refuse to let an
    admin deactivate or delete *their own* account, steering them to the
    self-service ``/users/me`` routes instead; the edge maps this to 409. (Self
    platform-admin revoke is allowed unless it is the last active admin, so it is
    not covered by this error.)
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
