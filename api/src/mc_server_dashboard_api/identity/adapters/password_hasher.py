"""PasswordHasher adapters: argon2 (primary) and bcrypt (alternative).

Concrete implementations of the :class:`PasswordHasher` Port (FR-AUTH-3),
selected at the edge by ``auth.password.hash`` (CONFIGURATION.md Section 5.3).
Each algorithm generates and embeds its own per-user salt, so the Port surface
stays a single ``hash`` call. Both libraries use their own secure defaults.

The KDF work is CPU-bound and blocks for tens of milliseconds, so each method
offloads it to a dedicated :data:`_KDF_EXECUTOR` thread pool rather than running
it inline on the event loop (issue #938). A dedicated pool isolates the KDF work
from asyncio's shared default ``ThreadPoolExecutor``, so data-plane transfer
storms cannot starve password hashing (issue #1696). The offloaded callables keep
the exact synchronous logic — same return values and same raised exceptions (e.g.
bcrypt's >72-byte ``ValueError``, which propagates out of the executor unchanged).
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

import argon2
import bcrypt

from mc_server_dashboard_api.identity.domain.errors import (
    PasswordVerificationUnavailableError,
)
from mc_server_dashboard_api.identity.domain.password_hasher import PasswordHasher

_LOG = logging.getLogger(__name__)

_KDF_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="password-kdf")

_T = TypeVar("_T")


async def _run_kdf(func: Callable[..., _T], *args: object) -> _T:
    """Run a blocking KDF call on the dedicated password-hashing pool (#1696)."""
    return await asyncio.get_running_loop().run_in_executor(
        _KDF_EXECUTOR, functools.partial(func, *args)
    )


# bcrypt ignores bytes past 72, so two passwords sharing a 72-byte prefix would
# verify identically. The password policy rejects >72-byte input when bcrypt is
# configured, so longer input never reaches this adapter; the guard below is
# defensive — it raises rather than silently truncating, never masking a bug.
_BCRYPT_MAX_BYTES = 72


# libargon2's messages for a stored hash it cannot use: its decoder could not
# parse it, or a parameter the hash encodes is out of range. A corrupt hash is
# refused like a wrong password (a uniform 401) with a WARNING. argon2-cffi
# raises the same VerificationError for every other failure, such as running out
# of memory or threads, so the message is the only thing that tells them apart.
# A message missing here is treated as a server failure (#3749).
_MALFORMED_HASH_ERRORS = frozenset(
    {
        "Decoding failed",  # ARGON2_DECODING_FAIL
        "Some of encoded parameters are too long or too short",  # ..._LENGTH_FAIL
        "Output is too short",  # ARGON2_OUTPUT_TOO_SHORT
        "Output is too long",  # ARGON2_OUTPUT_TOO_LONG
        "Salt is too short",  # ARGON2_SALT_TOO_SHORT
        "Salt is too long",  # ARGON2_SALT_TOO_LONG
        "Memory cost is too small",  # ARGON2_MEMORY_TOO_LITTLE
        "Memory cost is too large",  # ARGON2_MEMORY_TOO_MUCH
        "Time cost is too small",  # ARGON2_TIME_TOO_SMALL
        "Time cost is too large",  # ARGON2_TIME_TOO_LARGE
        "Too few lanes",  # ARGON2_LANES_TOO_FEW
        "Too many lanes",  # ARGON2_LANES_TOO_MANY
        "Not enough threads",  # ARGON2_THREADS_TOO_FEW
        "Too many threads",  # ARGON2_THREADS_TOO_MANY
    }
)


class Argon2PasswordHasher(PasswordHasher):
    """:class:`PasswordHasher` adapter over argon2-cffi (library defaults)."""

    def __init__(self) -> None:
        self._hasher = argon2.PasswordHasher()

    async def hash(self, plaintext: str) -> str:
        return await _run_kdf(self._hasher.hash, plaintext)

    async def verify(self, plaintext: str, password_hash: str) -> bool:
        return await _run_kdf(self._verify, plaintext, password_hash)

    def _verify(self, plaintext: str, password_hash: str) -> bool:
        try:
            return self._hasher.verify(password_hash, plaintext)
        except argon2.exceptions.VerifyMismatchError:
            return False
        except argon2.exceptions.InvalidHashError:
            # Not an argon2 hash at all: argon2-cffi refuses it before libargon2
            # sees it. Corrupt stored data is refused like a wrong password (a
            # uniform 401) and surfaced as a WARNING rather than swallowed.
            _LOG.warning("argon2 verify failed on a malformed stored hash")
            return False
        except argon2.exceptions.Argon2Error as exc:
            if str(exc) in _MALFORMED_HASH_ERRORS:
                _LOG.warning("argon2 verify failed on a malformed stored hash")
                return False
            # Anything else, such as running out of memory or threads, failed on
            # a hash libargon2 accepted, so the password was never judged:
            # refusing it would count a correct password towards a lockout.
            _LOG.error("argon2 verify failed: %s", exc)
            raise PasswordVerificationUnavailableError from exc


class BcryptPasswordHasher(PasswordHasher):
    """:class:`PasswordHasher` adapter over bcrypt (library default cost)."""

    async def hash(self, plaintext: str) -> str:
        return await _run_kdf(self._hash, plaintext)

    async def verify(self, plaintext: str, password_hash: str) -> bool:
        return await _run_kdf(self._verify, plaintext, password_hash)

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
