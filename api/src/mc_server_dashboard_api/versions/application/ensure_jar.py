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
import logging
from dataclasses import dataclass

from mc_server_dashboard_api.versions.domain.catalog import VersionCatalog
from mc_server_dashboard_api.versions.domain.errors import (
    JarHashMismatchError,
    VersionError,
)
from mc_server_dashboard_api.versions.domain.jar_fetcher import JarFetcher
from mc_server_dashboard_api.versions.domain.jar_pool import JarPool
from mc_server_dashboard_api.versions.domain.value_objects import (
    HashAlgorithm,
    JarSource,
    ServerType,
)

_LOG = logging.getLogger(__name__)

_HASHLIB_NAME = {
    HashAlgorithm.SHA1: "sha1",
    HashAlgorithm.SHA256: "sha256",
}


@dataclass(frozen=True)
class EnsuredJar:
    """Result of an ensure-on-start: the pool content key and the source fingerprint."""

    key: str  # pool content key (SHA-256)
    source_fingerprint: str | None  # None only on resolve-failure fallback


def source_fingerprint(source: JarSource) -> str:
    """Derive a comparable fingerprint from a resolved :class:`JarSource`.

    For sources that publish a digest (vanilla SHA-1, Paper SHA-256, Forge SHA-1):
    ``"{algorithm}:{hash}"``.  For sources without a digest (Fabric):
    ``"url:{download_url}"`` — the URL embeds the loader/installer versions, so a
    version bump produces a different fingerprint.
    """

    if source.expected_hash is not None and source.hash_algorithm is not None:
        return f"{source.hash_algorithm.value}:{source.expected_hash.lower()}"
    return f"url:{source.url}"


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
        known_source: str | None = None,
    ) -> EnsuredJar:
        # Always resolve the latest build so we detect upstream updates.
        try:
            source = await self.catalog.resolve(server_type, version)
        except VersionError:
            # Catalog unavailable: fall back to the existing JAR if pooled.
            if known_key is not None and await self.pool.has(known_key):
                _LOG.warning(
                    "catalog resolve failed for %s %s; falling back to pooled JAR %s",
                    server_type.value,
                    version,
                    known_key,
                )
                return EnsuredJar(known_key, known_source)
            raise

        fingerprint = source_fingerprint(source)

        if known_key is not None and await self.pool.has(known_key):
            # The existing JAR is still pooled.  Skip the download when the
            # source fingerprint matches (same build) OR when we can prove
            # identity via SHA-256 (Paper back-compat shortcut: the pool key IS
            # the published sha256, so no recorded fingerprint is needed).
            if known_source == fingerprint:
                return EnsuredJar(known_key, fingerprint)
            if (
                source.hash_algorithm is HashAlgorithm.SHA256
                and source.expected_hash is not None
                and known_key.lower() == source.expected_hash.lower()
            ):
                return EnsuredJar(known_key, fingerprint)
            # Fingerprint differs: a newer build is available upstream.
            _LOG.info(
                "upstream JAR updated for %s %s (old fingerprint: %s, "
                "new: %s); downloading new build",
                server_type.value,
                version,
                known_source,
                fingerprint,
            )

        data = await self.fetcher.fetch(source.url)
        _verify(source, data)
        key = await self.pool.put(data)
        return EnsuredJar(key, fingerprint)


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
