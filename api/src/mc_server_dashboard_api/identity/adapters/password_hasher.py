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

# bcrypt ignores bytes past 72, so two passwords sharing a 72-byte prefix would
# verify identically. The password policy rejects >72-byte input when bcrypt is
# configured, so longer input never reaches this adapter; the guard below is
# defensive — it raises rather than silently truncating, never masking a bug.
_BCRYPT_MAX_BYTES = 72


class Argon2PasswordHasher(PasswordHasher):
    """:class:`PasswordHasher` adapter over argon2-cffi (library defaults)."""

    def __init__(self) -> None:
        self._hasher = argon2.PasswordHasher()

    def hash(self, plaintext: str) -> str:
        return self._hasher.hash(plaintext)

    def verify(self, plaintext: str, password_hash: str) -> bool:
        try:
            return self._hasher.verify(password_hash, plaintext)
        except argon2.exceptions.VerifyMismatchError:
            return False


class BcryptPasswordHasher(PasswordHasher):
    """:class:`PasswordHasher` adapter over bcrypt (library default cost)."""

    def hash(self, plaintext: str) -> str:
        encoded = self._encode(plaintext)
        return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")

    def verify(self, plaintext: str, password_hash: str) -> bool:
        encoded = self._encode(plaintext)
        return bcrypt.checkpw(encoded, password_hash.encode("utf-8"))

    @staticmethod
    def _encode(plaintext: str) -> bytes:
        encoded = plaintext.encode("utf-8")
        if len(encoded) > _BCRYPT_MAX_BYTES:
            raise ValueError("password exceeds bcrypt's 72-byte limit")
        return encoded
