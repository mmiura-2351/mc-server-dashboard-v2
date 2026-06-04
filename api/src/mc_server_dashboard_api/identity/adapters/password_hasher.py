"""PasswordHasher adapters: argon2 (primary) and bcrypt (alternative).

Concrete implementations of the :class:`PasswordHasher` Port (FR-AUTH-3),
selected at the edge by ``auth.password.hash`` (CONFIGURATION.md Section 5.3).
Each algorithm generates and embeds its own per-user salt, so the Port surface
stays a single ``hash`` call. Both libraries use their own secure defaults.
"""

from __future__ import annotations

import argon2
import bcrypt

from mc_server_dashboard_api.identity.domain.password_hasher import PasswordHasher

# bcrypt ignores bytes past 72 and bcrypt>=5 raises on longer input; the policy
# permits up to ``max_length`` (default 128) characters, so truncate to the cap.
_BCRYPT_MAX_BYTES = 72


class Argon2PasswordHasher(PasswordHasher):
    """:class:`PasswordHasher` adapter over argon2-cffi (library defaults)."""

    def __init__(self) -> None:
        self._hasher = argon2.PasswordHasher()

    def hash(self, plaintext: str) -> str:
        return self._hasher.hash(plaintext)


class BcryptPasswordHasher(PasswordHasher):
    """:class:`PasswordHasher` adapter over bcrypt (library default cost)."""

    def hash(self, plaintext: str) -> str:
        encoded = plaintext.encode("utf-8")[:_BCRYPT_MAX_BYTES]
        return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")
