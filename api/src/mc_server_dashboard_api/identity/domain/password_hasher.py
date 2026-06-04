"""The ``PasswordHasher`` Port: one-way password hashing (FR-AUTH-3).

A Port so the domain never depends on a concrete KDF; the argon2 / bcrypt
adapters live in ``identity.adapters`` and are selected at the edge via
``auth.password.hash`` (CONFIGURATION.md Section 5.3). Per-user salt is handled
internally by the algorithm, so the Port takes only the plaintext. Verification
(login, FR-AUTH-2) compares a candidate plaintext against a stored hash without
the domain learning the algorithm.
"""

from __future__ import annotations

import abc


class PasswordHasher(abc.ABC):
    """Port: hashes a plaintext password into a self-describing hash string."""

    @abc.abstractmethod
    def hash(self, plaintext: str) -> str:
        """Return a storable hash of ``plaintext`` (salt embedded by the KDF)."""

    @abc.abstractmethod
    def verify(self, plaintext: str, password_hash: str) -> bool:
        """Return whether ``plaintext`` matches ``password_hash`` (FR-AUTH-2)."""
