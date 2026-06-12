"""PasswordHasher adapters: argon2 (primary) and bcrypt (alternative).

Concrete implementations of the :class:`PasswordHasher` Port (FR-AUTH-3),
selected at the edge by ``auth.password.hash`` (CONFIGURATION.md Section 5.3).
Each algorithm generates and embeds its own per-user salt, so the Port surface
stays a single ``hash`` call. Both libraries use their own secure defaults.

The KDF work is CPU-bound and blocks for tens of milliseconds, so each method
offloads it to a worker thread via :func:`asyncio.to_thread` rather than running
it inline on the event loop (issue #938). The offloaded callables keep the exact
synchronous logic — same return values and same raised exceptions (e.g. bcrypt's
>72-byte ``ValueError``, which propagates out of ``to_thread`` unchanged).
"""

from __future__ import annotations

import asyncio
import logging

import argon2
import bcrypt

from mc_server_dashboard_api.identity.domain.password_hasher import PasswordHasher

_LOG = logging.getLogger(__name__)

# bcrypt ignores bytes past 72, so two passwords sharing a 72-byte prefix would
# verify identically. The password policy rejects >72-byte input when bcrypt is
# configured, so longer input never reaches this adapter; the guard below is
# defensive — it raises rather than silently truncating, never masking a bug.
_BCRYPT_MAX_BYTES = 72


class Argon2PasswordHasher(PasswordHasher):
    """:class:`PasswordHasher` adapter over argon2-cffi (library defaults)."""

    def __init__(self) -> None:
        self._hasher = argon2.PasswordHasher()

    async def hash(self, plaintext: str) -> str:
        return await asyncio.to_thread(self._hasher.hash, plaintext)

    async def verify(self, plaintext: str, password_hash: str) -> bool:
        return await asyncio.to_thread(self._verify, plaintext, password_hash)

    def _verify(self, plaintext: str, password_hash: str) -> bool:
        try:
            return self._hasher.verify(password_hash, plaintext)
        except argon2.exceptions.VerifyMismatchError:
            return False
        except (argon2.exceptions.Argon2Error, argon2.exceptions.InvalidHash):
            # A wrong password raises VerifyMismatchError (handled above). Any
            # other argon2 failure means the stored hash is malformed/corrupt
            # (InvalidHash is not an Argon2Error subclass in the pinned version,
            # so it is caught explicitly). Return False for a uniform 401 rather
            # than a 500, and warn — corrupt stored data is worth surfacing, not
            # swallowing silently.
            _LOG.warning("argon2 verify failed on a malformed stored hash")
            return False


class BcryptPasswordHasher(PasswordHasher):
    """:class:`PasswordHasher` adapter over bcrypt (library default cost)."""

    async def hash(self, plaintext: str) -> str:
        return await asyncio.to_thread(self._hash, plaintext)

    async def verify(self, plaintext: str, password_hash: str) -> bool:
        return await asyncio.to_thread(self._verify, plaintext, password_hash)

    def _hash(self, plaintext: str) -> str:
        encoded = self._encode(plaintext)
        return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")

    def _verify(self, plaintext: str, password_hash: str) -> bool:
        encoded = plaintext.encode("utf-8")
        if len(encoded) > _BCRYPT_MAX_BYTES:
            # Login input is attacker-controlled, so unlike hash() this path is
            # reachable. It must NOT raise: a ValueError would 500 the auth
            # route, breaking the uniform-401 + artificial-delay posture and
            # turning the length into an oracle. Returning False leaks nothing —
            # in this greenfield system no stored hash was ever created from
            # truncated input, so a >72-byte presentation can never match a
            # legitimate hash; False is the correct, non-oracle answer.
            return False
        return bcrypt.checkpw(encoded, password_hash.encode("utf-8"))

    @staticmethod
    def _encode(plaintext: str) -> bytes:
        encoded = plaintext.encode("utf-8")
        if len(encoded) > _BCRYPT_MAX_BYTES:
            raise ValueError("password exceeds bcrypt's 72-byte limit")
        return encoded


def build_dummy_password_hash(algorithm: str, plaintext: str) -> str:
    """Synchronously hash a static plaintext for the login timing-defence dummy.

    The dummy verification hash (dependencies.py) is a one-time wiring constant,
    derived from a throwaway plaintext and memoized, that gives the unknown-user
    login path the same cost as a wrong-password verify. It is built in the sync
    dependency-assembly path, not on an async request, so it deliberately does
    *not* go through the now-async :class:`PasswordHasher.hash` (issue #938) —
    keeping the KDF call here, in the adapter layer, avoids both an event-loop
    round-trip at construction and leaking algorithm knowledge into the wiring.
    """

    if algorithm == "bcrypt":
        return BcryptPasswordHasher()._hash(plaintext)
    return argon2.PasswordHasher().hash(plaintext)
