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
import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class PoolStats:
    """Aggregate stats for the pool: number of JARs and their total bytes (#286)."""

    count: int
    total_bytes: int


@dataclass(frozen=True)
class PoolEntry:
    """One pooled JAR's content key, size, and store time (the GC's unit, #293).

    The versions-side mirror of the storage ``JarPoolEntry`` slice the GC scans:
    ``sha256`` is the content key (same string the server config records),
    ``size_bytes`` feeds the freed-bytes accounting, and ``modified_at`` (UTC,
    timezone-aware) is what the GC's safety window compares against ``now``.
    """

    sha256: str
    size_bytes: int
    modified_at: dt.datetime


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

    @abc.abstractmethod
    async def stats(self) -> PoolStats:
        """Count + total bytes of the pooled JARs (operational visibility, #286)."""

    @abc.abstractmethod
    async def list_entries(self) -> list[PoolEntry]:
        """Enumerate the pooled JARs with key, size, and store time (the GC, #293)."""

    @abc.abstractmethod
    async def delete(self, sha256: str) -> None:
        """Remove a pooled JAR by content key. Idempotent (no error if absent)."""
