"""The ``PasswordHasher`` Port: one-way password hashing (FR-AUTH-3).

A Port so the domain never depends on a concrete KDF; the argon2 / bcrypt
adapters live in ``identity.adapters`` and are selected at the edge via
``auth.password.hash`` (CONFIGURATION.md Section 5.3). Per-user salt is handled
internally by the algorithm, so the Port takes only the plaintext. Verification
(login, FR-AUTH-2) compares a candidate plaintext against a stored hash without
the domain learning the algorithm.

The methods are ``async`` because a memory-hard KDF (argon2) is CPU-bound and
blocks for tens of milliseconds per call; the adapters offload the work to a
worker thread so a hash on an async route does not stall the event loop and the
in-flight requests sharing it (issue #938). The Port surface is async so this
offloading is invisible to callers and the domain never sees the threading.
"""

from __future__ import annotations

import abc


class PasswordHasher(abc.ABC):
    """Port: hashes a plaintext password into a self-describing hash string."""

    @abc.abstractmethod
    async def hash(self, plaintext: str) -> str:
        """Return a storable hash of ``plaintext`` (salt embedded by the KDF)."""

    @abc.abstractmethod
    async def verify(self, plaintext: str, password_hash: str) -> bool:
        """Return whether ``plaintext`` matches ``password_hash`` (FR-AUTH-2)."""
