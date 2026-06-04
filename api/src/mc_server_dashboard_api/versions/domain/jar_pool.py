"""The ``JarPool`` seam: the versions context's view of the content-addressed JAR store.

A narrow Port mirroring the slice of the storage :class:`JarStore` the ensure-on-
start use case needs (presence test + store-returning-key). The versions
application depends on this rather than importing the storage context directly
(cross-context import contract); the wiring binds it to the real ``Storage``
adapter at the edge. The key is the JAR's content address (its lowercase-hex
SHA-256), the same value the storage ``JarKey`` carries — kept as a plain string
here so no storage type crosses the seam.
"""

from __future__ import annotations

import abc


class JarPool(abc.ABC):
    """Port: presence + store for content-addressed JARs (STORAGE.md Section 3.2)."""

    @abc.abstractmethod
    async def has(self, sha256: str) -> bool:
        """Return whether a JAR with this content key is already stored."""

    @abc.abstractmethod
    async def put(self, data: bytes) -> str:
        """Store the JAR bytes, returning their content key (lowercase-hex SHA-256).

        Idempotent: identical bytes yield the same key and no duplicate.
        """
