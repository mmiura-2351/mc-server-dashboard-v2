"""Ensure a resolved server JAR is present in the content-addressed pool (FR-VER-3).

The ensure-on-start use case (the issue's PM ruling): server CREATE only validates
the version exists (cheap, no download); START ensures the JAR is in the pool.
Given a ``(server_type, version)`` it resolves the :class:`JarSource` via the
catalog, downloads the bytes, verifies them against the source's published digest
(SHA-1 for vanilla, SHA-256 for Paper), and stores them content-addressed —
returning the resulting :class:`JarKey` (its SHA-256). A hash mismatch rejects the
bytes and raises before anything is stored, so a start fails cleanly before
placement/dispatch.

The download is skipped when the JAR is already pooled. The pool key is the
bytes' SHA-256, which is *not* the source's expected digest for vanilla (SHA-1),
so presence cannot be tested from the source descriptor alone before the first
download; once the content key is known (recorded on the server, issue #118) the
caller passes it to short-circuit the re-download.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from mc_server_dashboard_api.versions.domain.catalog import VersionCatalog
from mc_server_dashboard_api.versions.domain.errors import JarHashMismatchError
from mc_server_dashboard_api.versions.domain.jar_fetcher import JarFetcher
from mc_server_dashboard_api.versions.domain.jar_pool import JarPool
from mc_server_dashboard_api.versions.domain.value_objects import (
    HashAlgorithm,
    JarSource,
    ServerType,
)

_HASHLIB_NAME = {
    HashAlgorithm.SHA1: "sha1",
    HashAlgorithm.SHA256: "sha256",
}


@dataclass(frozen=True)
class EnsureJar:
    """Resolve, download-and-verify, and pool a server JAR; return its content key."""

    catalog: VersionCatalog
    fetcher: JarFetcher
    pool: JarPool

    async def __call__(
        self,
        *,
        server_type: ServerType,
        version: str,
        known_key: str | None = None,
    ) -> str:
        if known_key is not None and await self.pool.has(known_key):
            return known_key
        source = await self.catalog.resolve(server_type, version)
        data = await self.fetcher.fetch(source.url)
        _verify(source, data)
        return await self.pool.put(data)


def _verify(source: JarSource, data: bytes) -> None:
    if source.hash_algorithm is None or source.expected_hash is None:
        # Fabric's meta API publishes no digest for the generated launcher JAR, so
        # there is nothing to verify against; the bytes are still pooled
        # content-addressed by their own SHA-256.
        return
    digest = hashlib.new(_HASHLIB_NAME[source.hash_algorithm], data).hexdigest()
    if digest.lower() != source.expected_hash.lower():
        raise JarHashMismatchError(
            f"{source.server_type.value} {source.version}: "
            f"expected {source.hash_algorithm.value} {source.expected_hash}, "
            f"got {digest}"
        )
