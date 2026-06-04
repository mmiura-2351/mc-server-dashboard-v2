"""Value objects for the storage context: scope ids, content keys, and rel paths.

Pure, immutable, standard-library only. ``CommunityId`` / ``ServerId`` are
*foreign* references held by id value only — the storage domain never imports the
community or servers domains (STORAGE.md Section 1.1: Storage returns opaque
keys/handles and never queries another context). ``JarKey`` is the content
address of an immutable JAR; ``BackupKey``, ``SnapshotId`` and ``VersionId`` are
opaque blob handles the metadata layer (DATABASE.md #15) indexes.

``RelPath`` carries the *string-level* half of the traversal defence (STORAGE.md
Section 6): it rejects absolute paths and ``..`` components at construction, with
no filesystem access. The filesystem-level half — canonicalize and verify the
result stays inside the server root, refusing symlink escapes — is the adapter's
job (it needs real I/O), so it lives in :mod:`...adapters.fs`.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath

from mc_server_dashboard_api.storage.domain.errors import PathTraversalError


@dataclass(frozen=True)
class CommunityId:
    """A foreign reference to the owning community, by id value only."""

    value: uuid.UUID


@dataclass(frozen=True)
class ServerId:
    """A foreign reference to the scoped server, by id value only."""

    value: uuid.UUID


# A JAR content key is the lowercase hex SHA-256 of the JAR bytes (STORAGE.md
# Section 3.2): exactly 64 hex characters.
_SHA256_HEX = re.compile(r"\A[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class JarKey:
    """The content address of a stored JAR: its lowercase-hex SHA-256.

    Identical bytes always yield the same key, so an identical JAR is stored once
    and reused across servers and Communities (STORAGE.md Section 3.2, FR-VER-3).
    """

    sha256: str

    def __post_init__(self) -> None:
        if not _SHA256_HEX.match(self.sha256):
            raise ValueError(
                f"JarKey must be a 64-char lowercase hex digest: {self.sha256!r}"
            )


@dataclass(frozen=True)
class BackupKey:
    """An opaque handle to a retained backup archive (STORAGE.md Section 3.3)."""

    value: str


@dataclass(frozen=True)
class SnapshotId:
    """An opaque id naming one published working-set snapshot (STORAGE.md Section 2).

    A fresh, never-before-published name is minted per publish so the staged copy
    moves into ``snapshots/<id>/`` without ever overwriting an authoritative one
    (Section 4.2).
    """

    value: str

    @classmethod
    def new(cls) -> SnapshotId:
        return cls(uuid.uuid4().hex)


@dataclass(frozen=True)
class VersionId:
    """An opaque id naming one retained prior content of a file.

    See STORAGE.md Section 5.
    """

    value: str


@dataclass(frozen=True)
class RelPath:
    """A caller-supplied path, validated as relative-and-contained at the string level.

    Construction enforces the string-level traversal rules (STORAGE.md Section 6):
    the path must not be absolute and must not contain a ``..`` component. ``.``
    components and redundant separators are normalised away. ``.`` (or the empty
    string) denotes the server root itself — legal for :meth:`list_dir` (browse
    the working-set root), so :attr:`parts` may be empty. The stored parts are the
    clean POSIX components the adapter joins under the server root; the adapter
    then performs the filesystem-level containment + symlink-escape check.
    """

    parts: tuple[str, ...]

    def __init__(self, raw: str) -> None:
        pure = PurePosixPath(raw)
        if pure.is_absolute():
            raise PathTraversalError(
                f"rel_path must be relative, got absolute: {raw!r}"
            )
        parts: list[str] = []
        for part in pure.parts:
            if part == "..":
                raise PathTraversalError(f"rel_path must not contain '..': {raw!r}")
            if part in ("", "."):
                continue
            parts.append(part)
        object.__setattr__(self, "parts", tuple(parts))

    @property
    def value(self) -> str:
        """The normalised POSIX path (``world/level.dat``; ``.`` for the root)."""

        return "/".join(self.parts) if self.parts else "."
