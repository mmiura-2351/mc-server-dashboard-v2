"""The servers-side authoritative-file seam (the file layer's view of Storage).

The file use cases must read/edit a *stopped* server's authoritative working set
and manage its file versions (Section 6.9, FR-FILE-3) — all Storage concerns. The
servers domain and application may not import the storage context (import-linter
contract), so they depend on this narrow Port; the wiring binds it to a storage
adapter that drives the real :class:`Storage` Port (mirroring the lifecycle
layer's :class:`ControlPlane` seam).

The Port speaks the servers domain's own ids and raises the servers file errors
(:class:`ServerFileNotFoundError`, :class:`InvalidFilePathError`); the adapter
translates the storage ``NotFoundError`` / ``PathTraversalError`` at the seam, so
no storage type crosses into the application layer.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)


@dataclass(frozen=True)
class FileEntry:
    """One entry in a directory listing of the authoritative working set."""

    name: str
    is_dir: bool
    size: int


class FileStore(abc.ABC):
    """Port: the file layer's seam to the authoritative-copy file store."""

    @abc.abstractmethod
    def validate_rel_path(self, rel_path: str) -> None:
        """Reject a traversal-unsafe ``rel_path`` at the string level (FR-FILE-4).

        The running branch forwards the raw ``rel_path`` to the Worker rather than
        through this seam, so the use case asks the seam to pre-reject a doomed
        path before dispatch. The adapter applies the same string-level rule the
        storage value object enforces, keeping the storage type behind the seam.
        Raises :class:`InvalidFilePathError` for a traversal-unsafe path.
        """

    @abc.abstractmethod
    async def read_file(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> bytes:
        """Read one file from the authoritative ``current/`` (FR-FILE-1).

        Raises :class:`ServerFileNotFoundError` for a missing path and
        :class:`InvalidFilePathError` for a traversal-unsafe one (FR-FILE-4).
        """

    @abc.abstractmethod
    def open_file_stream(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> AsyncIterator[bytes]:
        """Stream one file's bytes from ``current/`` in chunks (issue #265).

        The bounded-memory read seam for a large single-file download: the bytes
        are yielded incrementally so the whole file is never buffered in RAM
        (unlike :meth:`read_file`, which is the small-edit / base64-payload read).
        Raises :class:`ServerFileNotFoundError` for a missing path and
        :class:`InvalidFilePathError` for a traversal-unsafe one.
        """

    @abc.abstractmethod
    async def list_dir(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> list[FileEntry]:
        """Browse a directory in the authoritative ``current/`` (FR-FILE-1).

        ``rel_path == "."`` lists the working-set root. Raises the same errors as
        :meth:`read_file`.
        """

    @abc.abstractmethod
    async def write_file(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: str,
        content: bytes,
    ) -> None:
        """Edit one file in ``current/``, retaining the prior version (FR-FILE-3)."""

    @abc.abstractmethod
    async def delete_file(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> None:
        """Delete one file from ``current/``, retaining the prior content (#259).

        Raises :class:`ServerFileNotFoundError` for a missing path and
        :class:`InvalidFilePathError` for a traversal-unsafe one.
        """

    @abc.abstractmethod
    async def delete_dir(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> None:
        """Recursively delete a directory subtree from ``current/`` (#259).

        No per-file version capture (the Storage Port contract); whole-subtree
        recovery is the backups' job. Raises the same errors as :meth:`delete_file`.
        """

    @abc.abstractmethod
    async def make_dir(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> None:
        """Create an (empty) directory in ``current/`` (#259).

        Backend-dependent: fs materializes a real empty directory; object storage
        cannot represent one (no-op there). Raises :class:`InvalidFilePathError`
        for a traversal-unsafe path and :class:`ServerFileNotFoundError` when no
        working set has been published yet (fs has no snapshot to create it in).
        """

    @abc.abstractmethod
    def download_dir(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> AsyncIterator[bytes]:
        """Stream a zip of a directory subtree of the authoritative ``current/``.

        Used by the directory-download branch (issue #259). The zip is generated
        incrementally so peak memory is bounded by one in-flight file plus the
        stream buffer, never the whole subtree. ``rel_path == "."`` zips the
        working-set root. Raises :class:`ServerFileNotFoundError` for a missing
        directory and :class:`InvalidFilePathError` for a traversal-unsafe one.
        """

    @abc.abstractmethod
    def export_dir(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: str,
        extra: list[tuple[str, bytes]],
    ) -> AsyncIterator[bytes]:
        """Stream a zip of a subtree plus ``extra`` in-memory entries (issue #274).

        Like :meth:`download_dir` (same incremental, bounded-memory streaming sink),
        but each ``(arcname, bytes)`` in ``extra`` is appended to the zip after the
        subtree's files. The whole-server export uses this to carry an
        ``export_metadata.json`` descriptor alongside the working set in one stream.
        Raises the same errors as :meth:`download_dir`.
        """

    @abc.abstractmethod
    async def list_versions(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> list[str]:
        """List retained prior version ids of a file, newest-first (file:history)."""

    @abc.abstractmethod
    async def rollback(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: str,
        version_id: str,
    ) -> None:
        """Restore a file to a retained version (file:rollback, FR-FILE-3).

        Raises :class:`ServerFileNotFoundError` for an unknown path/version.
        """
