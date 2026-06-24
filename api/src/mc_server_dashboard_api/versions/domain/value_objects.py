"""Value objects for the versions context (FR-VER-1, ARCHITECTURE.md Section 5.1).

Pure, immutable, standard-library only. The versions domain is global: a
:class:`ServerType` and an MC version string identify a downloadable JAR, with no
community/server scope (the catalog is platform-wide — STORAGE.md Section 8.1:
JARs are shared across all Communities).

``ServerType`` is duplicated here rather than imported from the servers domain:
the versions domain owns the *catalog* notion of a distribution and must not
depend on another context (import-linter contract). The two enums share values
on purpose (``vanilla`` / ``paper`` / ``fabric`` / ``forge``). ``forge`` resolves
to the *installer* JAR (the worker runs ``--installServer`` on first start,
issue #307).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class ServerType(enum.Enum):
    """Server distributions the catalog can list/resolve.

    ``vanilla`` (Mojang version manifest), ``paper`` (PaperMC API), ``fabric``
    (meta.fabricmc.net), and ``forge`` (the Forge Maven, issue #307) are
    resolvable. ``forge`` resolves to the *installer* JAR — the worker runs the
    supervised ``--installServer`` step on first start.
    """

    VANILLA = "vanilla"
    PAPER = "paper"
    FABRIC = "fabric"
    FORGE = "forge"


class HashAlgorithm(enum.Enum):
    """The digest algorithm an external source publishes for a JAR.

    Mojang's version manifest publishes a SHA-1 for ``server.jar``; the PaperMC
    API publishes a SHA-256 per build download. The verify step on download hashes
    the bytes with the matching algorithm and compares constant-time.
    """

    SHA1 = "sha1"
    SHA256 = "sha256"


@dataclass(frozen=True)
class VersionRef:
    """One listable version of a server type (FR-VER-1)."""

    server_type: ServerType
    version: str


@dataclass(frozen=True)
class JarSource:
    """A resolved, downloadable JAR descriptor (FR-VER-1).

    ``url`` is the external download URL; ``expected_hash`` is the lowercase-hex
    digest the source published, with ``hash_algorithm`` naming how to verify it.
    Vanilla (SHA-1) and Paper (SHA-256) both publish a hash, so the download path
    verifies before storing content-addressed (FR-VER-2/3). Fabric does not: its
    meta API generates the server launcher JAR on demand and publishes no digest
    for it, so both fields are ``None`` and the download is stored unverified
    (still content-addressed by its own SHA-256 in the pool).
    """

    server_type: ServerType
    version: str
    url: str
    expected_hash: str | None
    hash_algorithm: HashAlgorithm | None
