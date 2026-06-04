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
    """

    id: UserId
    username: Username
    email: EmailAddress
    password_hash: str
    created_at: dt.datetime
    updated_at: dt.datetime
    is_platform_admin: bool = False


@dataclass
class RefreshToken:
    """A persisted refresh-token session (DATABASE.md ``refresh_token``).

    The token is stored hashed, never in plaintext. A token is valid iff it is
    unrevoked and unexpired; :meth:`is_active` encodes exactly that rule.
    """

    id: RefreshTokenId
    user_id: UserId
    token_hash: str
    issued_at: dt.datetime
    expires_at: dt.datetime
    revoked_at: dt.datetime | None = None

    def is_active(self, *, now: dt.datetime) -> bool:
        """Return whether the token is usable at ``now``.

        Mirrors the DATABASE.md validity rule:
        ``revoked_at IS NULL AND expires_at > now()``.
        """

        return self.revoked_at is None and self.expires_at > now
