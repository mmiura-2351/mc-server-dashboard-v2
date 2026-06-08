"""Entities for the identity context: ``User`` and ``RefreshToken``.

Pure data with their invariants, standard-library only. ``User`` is a global
identity (FR-AUTH-5) carrying the platform-admin flag (FR-AUTH-6).
``RefreshToken`` is the server-side revocation/expiry record for a persisted
session (FR-AUTH-2); its validity rule mirrors DATABASE.md Section 4.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from mc_server_dashboard_api.identity.domain.value_objects import (
    EmailAddress,
    RefreshTokenId,
    UserId,
    Username,
)


@dataclass
class User:
    """A global user account (DATABASE.md ``user``).

    ``is_platform_admin`` is the admin axis (FR-AUTH-6), not a separate table.
    The password is held only as its hash (FR-AUTH-3); hashing itself is an
    adapter concern and out of scope for the domain.

    ``active`` is the account lifecycle flag (issue #278): a deactivated account
    keeps its row (so audit history and uniqueness survive) but cannot
    authenticate — login refuses it with the uniform 401 and every request
    carrying its access token is rejected by ``AuthenticateRequest``.
    """

    id: UserId
    username: Username
    email: EmailAddress
    password_hash: str
    created_at: dt.datetime
    updated_at: dt.datetime
    is_platform_admin: bool = False
    active: bool = True


# Why a refresh token was revoked (``refresh_token.revoked_reason``). Only a
# ``ROTATED`` predecessor is eligible for the reuse grace window (issue #369): a
# re-presented rotated token is a legitimate concurrent refresh / lost-response
# retry. A ``FAMILY``-revoked token (theft response, password change, deactivate,
# delete) or a ``LOGOUT``-revoked token must never be graced -- re-presenting it
# stays on the theft path -- so an attacker cannot escape a family revoke by
# re-presenting a just-revoked successor within the window.
REVOKED_ROTATED = "rotated"
REVOKED_FAMILY = "family"
REVOKED_LOGOUT = "logout"
# A session the user explicitly revoked through the session-management API
# (``DELETE /users/me/sessions[/{id}]``, issue #387). Like ``LOGOUT`` and
# ``FAMILY`` it is never graced in the reuse window: re-presenting a user-revoked
# token stays on the theft path (the grace covers only ``ROTATED`` predecessors).
REVOKED_USER = "user_revoked"
# The cookie-carried token of a both-transports refresh/logout, revoked because
# the body token won precedence (issue #384). The browser jar overwrote it with
# the body token's successor, so no client holds it any more -- revoking it closes
# the dangling-valid-token gap. It is a *single-token* revoke (never a family
# revoke), so the just-issued successor in the same family is untouched. Like the
# other non-rotation reasons it is never graced: the grace covers only ``ROTATED``
# predecessors.
REVOKED_SUPERSEDED = "superseded"


@dataclass
class RefreshToken:
    """A persisted refresh-token session (DATABASE.md ``refresh_token``).

    The token is stored hashed, never in plaintext. A token is valid iff it is
    unrevoked and unexpired; :meth:`is_active` encodes exactly that rule.
    ``revoked_reason`` records *why* a revoked token was revoked (one of the
    ``REVOKED_*`` codes); it is ``None`` exactly when ``revoked_at`` is ``None``.
    """

    id: RefreshTokenId
    user_id: UserId
    token_hash: str
    issued_at: dt.datetime
    expires_at: dt.datetime
    revoked_at: dt.datetime | None = None
    revoked_reason: str | None = None

    def is_active(self, *, now: dt.datetime) -> bool:
        """Return whether the token is usable at ``now``.

        Mirrors the DATABASE.md validity rule:
        ``revoked_at IS NULL AND expires_at > now()``.
        """

        return self.revoked_at is None and self.expires_at > now
