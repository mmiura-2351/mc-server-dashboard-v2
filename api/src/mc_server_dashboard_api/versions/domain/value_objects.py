"""Value objects for the versions context (FR-VER-1, ARCHITECTURE.md Section 5.1).

Pure, immutable, standard-library only. The versions domain is global: a
:class:`ServerType` and an MC version string identify a downloadable JAR, with no
community/server scope (the catalog is platform-wide — STORAGE.md Section 8.1:
JARs are shared across all Communities).

``ServerType`` is duplicated here rather than imported from the servers domain:
the versions domain owns the *catalog* notion of a distribution and must not
depend on another context (import-linter contract). The two enums share values
on purpose (``vanilla`` / ``paper``), but ``forge`` is deliberately absent — it
is listed nowhere and not resolvable at M1 (the issue's documented non-goal;
create-validation rejects it explicitly).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class ServerType(enum.Enum):
    """Server distributions the catalog can list/resolve at M1.

    Only ``vanilla`` and ``paper`` are resolvable at M1 (the catalog sources are
    the Mojang version manifest and the PaperMC API). ``forge`` is intentionally
    not a member: it is not listed and not resolvable, and server create-validation
    rejects it with an "unsupported at M1" error even though the DB CHECK enum
    still permits the value.
    """

    VANILLA = "vanilla"
    PAPER = "paper"


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
    The catalog always returns a hash for its M1 sources (both publish one), so the
    download path can verify before storing content-addressed (FR-VER-2/3).
    """

    server_type: ServerType
    version: str
    url: str
    expected_hash: str
    hash_algorithm: HashAlgorithm
