"""Storage-backed adapter for the servers :class:`FileStore` seam.

Binds the servers file seam to the real :class:`Storage` Port (its ``FileStore`` /
``FileVersionStore`` slices, STORAGE.md Sections 3.4/3.5). This is an adapter-layer
composition across bounded contexts (mirroring ``FleetControlPlaneAdapter``); the
servers *domain* and *application* never import the storage context (import-linter
contract).

The seam translates the storage value objects (``RelPath`` rejects traversal at
construction; ``VersionId`` names a retained version) and the storage errors
(``NotFoundError`` -> :class:`ServerFileNotFoundError`, ``PathTraversalError`` ->
:class:`InvalidFilePathError`) so no storage type crosses back into the servers
layer.
"""

from __future__ import annotations

import zipfile
from collections.abc import AsyncIterator

from mc_server_dashboard_api.servers.domain.errors import (
    InvalidFilePathError,
    ServerFileNotFoundError,
)
from mc_server_dashboard_api.servers.domain.file_store import FileEntry, FileStore
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)
from mc_server_dashboard_api.storage.domain.errors import (
    NotFoundError,
    PathTraversalError,
)
from mc_server_dashboard_api.storage.domain.port import Storage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import RelPath, VersionId
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)


def _scope(
    community_id: CommunityId, server_id: ServerId
) -> tuple[StorageCommunityId, StorageServerId]:
    return (
        StorageCommunityId(community_id.value),
        StorageServerId(server_id.value),
    )


def _rel_path(raw: str) -> RelPath:
    # RelPath construction enforces the string-level traversal rules (absolute /
    # ".." rejection); surface a rejection as the servers file error.
    try:
        return RelPath(raw)
    except PathTraversalError as exc:
        raise InvalidFilePathError(raw) from exc


class StorageFileStoreAdapter(FileStore):
    """Bind the servers file seam to the Storage file/version slices."""

    def __init__(self, *, storage: Storage) -> None:
        self._storage = storage

    def validate_rel_path(self, rel_path: str) -> None:
        # Apply the storage string-level traversal rule (RelPath construction)
        # at the seam, so the running branch can pre-reject without importing
        # the storage value object into the servers layer.
        _rel_path(rel_path)

    async def read_file(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> bytes:
        community, server = _scope(community_id, server_id)
        try:
            return await self._storage.read_file(community, server, _rel_path(rel_path))
        except PathTraversalError as exc:
            raise InvalidFilePathError(rel_path) from exc
        except NotFoundError as exc:
            raise ServerFileNotFoundError(str(server_id.value)) from exc

    def open_file_stream(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> AsyncIterator[bytes]:
        # Stream the storage per-file read seam (issue #265), translating the
        # storage errors at the seam. The errors surface lazily (the Storage
        # stream resolves + locates the file on first iteration), so the
        # translation wraps the iteration, mirroring read_file's mapping.
        return self._open_file_stream_gen(community_id, server_id, rel_path)

    async def _open_file_stream_gen(
        self, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> AsyncIterator[bytes]:
        community, server = _scope(community_id, server_id)
        try:
            async for chunk in self._storage.open_file_stream(
                community, server, _rel_path(rel_path)
            ):
                yield chunk
        except PathTraversalError as exc:
            raise InvalidFilePathError(rel_path) from exc
        except NotFoundError as exc:
            raise ServerFileNotFoundError(str(server_id.value)) from exc

    async def list_dir(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> list[FileEntry]:
        community, server = _scope(community_id, server_id)
        try:
            entries = await self._storage.list_dir(
                community, server, _rel_path(rel_path)
            )
        except PathTraversalError as exc:
            raise InvalidFilePathError(rel_path) from exc
        except NotFoundError as exc:
            raise ServerFileNotFoundError(str(server_id.value)) from exc
        return [
            FileEntry(name=entry.name, is_dir=entry.is_dir, size=entry.size)
            for entry in entries
        ]

    async def write_file(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: str,
        content: bytes,
    ) -> None:
        community, server = _scope(community_id, server_id)
        try:
            await self._storage.write_file(
                community, server, _rel_path(rel_path), content
            )
        except PathTraversalError as exc:
            raise InvalidFilePathError(rel_path) from exc

    async def delete_file(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> None:
        community, server = _scope(community_id, server_id)
        try:
            await self._storage.delete_file(community, server, _rel_path(rel_path))
        except PathTraversalError as exc:
            raise InvalidFilePathError(rel_path) from exc
        except NotFoundError as exc:
            raise ServerFileNotFoundError(str(server_id.value)) from exc

    async def delete_dir(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> None:
        community, server = _scope(community_id, server_id)
        try:
            await self._storage.delete_dir(community, server, _rel_path(rel_path))
        except PathTraversalError as exc:
            raise InvalidFilePathError(rel_path) from exc
        except NotFoundError as exc:
            raise ServerFileNotFoundError(str(server_id.value)) from exc

    async def make_dir(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> None:
        community, server = _scope(community_id, server_id)
        try:
            await self._storage.make_dir(community, server, _rel_path(rel_path))
        except PathTraversalError as exc:
            raise InvalidFilePathError(rel_path) from exc
        except NotFoundError as exc:
            raise ServerFileNotFoundError(str(server_id.value)) from exc

    def download_dir(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> AsyncIterator[bytes]:
        # Build the zip incrementally over the Storage read stream (issue #259):
        # walk the subtree via list_dir (cheap listings), then for each file read
        # its bytes and write one zip entry, draining the zip's buffer after each
        # entry. Peak memory is one in-flight file plus its just-written zip
        # block — never the whole subtree. Storage's read_file / list_dir apply
        # the filesystem-level traversal containment, so a hostile symlink inside
        # the tree is refused at the seam, not zipped.
        return self._download_dir_gen(community_id, server_id, rel_path)

    def export_dir(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: str,
        extra: list[tuple[str, bytes]],
    ) -> AsyncIterator[bytes]:
        # Same incremental, bounded-memory zip stream as download_dir, with the
        # ``extra`` in-memory entries (the export_metadata.json descriptor, issue
        # #274) appended after the subtree's files.
        return self._download_dir_gen(community_id, server_id, rel_path, extra=extra)

    async def _download_dir_gen(
        self,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: str,
        *,
        extra: list[tuple[str, bytes]] | None = None,
    ) -> AsyncIterator[bytes]:
        # Verify the directory exists up front so a missing/invalid path surfaces
        # the servers error before any bytes are streamed.
        await self.list_dir(
            community_id=community_id, server_id=server_id, rel_path=rel_path
        )
        sink = _ZipStreamSink()
        # An unseekable sink drives zipfile's streaming mode (data descriptors,
        # no seek-back to patch headers), so each entry's bytes can be flushed
        # out as soon as they are written. Peak memory is one in-flight CHUNK plus
        # its just-written zip block, never a whole member or the whole subtree:
        # each member is read through the Storage per-file stream and copied into
        # the zip chunk-by-chunk (issue #265).
        with zipfile.ZipFile(sink, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            async for arcname, member_stream in self._walk_files(
                community_id, server_id, rel_path
            ):
                with zf.open(arcname, mode="w") as member:
                    async for chunk in member_stream:
                        member.write(chunk)
                        for out in sink.drain():
                            yield out
                for out in sink.drain():
                    yield out
            for arcname, content in extra or ():
                zf.writestr(arcname, content)
                for out in sink.drain():
                    yield out
        for out in sink.drain():
            yield out

    async def _walk_files(
        self, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> AsyncIterator[tuple[str, AsyncIterator[bytes]]]:
        """Yield ``(arcname, byte_stream)`` for every file under ``rel_path``.

        Depth-first. Arcnames are relative to ``rel_path`` so the zip contains the
        subtree itself, not the path leading to it. Each file is handed back as
        the Storage per-file stream so the caller copies it into the zip
        chunk-by-chunk (bounded memory, issue #265).
        """

        base = "" if rel_path in ("", ".") else rel_path.rstrip("/")
        stack = [base]
        while stack:
            current = stack.pop()
            entries = await self.list_dir(
                community_id=community_id,
                server_id=server_id,
                rel_path=current or ".",
            )
            for entry in entries:
                child = f"{current}/{entry.name}" if current else entry.name
                if entry.is_dir:
                    stack.append(child)
                    continue
                arcname = child[len(base) + 1 :] if base else child
                yield (
                    arcname,
                    self.open_file_stream(
                        community_id=community_id,
                        server_id=server_id,
                        rel_path=child,
                    ),
                )

    async def list_versions(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> list[str]:
        community, server = _scope(community_id, server_id)
        try:
            versions = await self._storage.list_file_versions(
                community, server, _rel_path(rel_path)
            )
        except PathTraversalError as exc:
            raise InvalidFilePathError(rel_path) from exc
        except NotFoundError as exc:
            raise ServerFileNotFoundError(str(server_id.value)) from exc
        return [version.value for version in versions]

    async def rollback(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: str,
        version_id: str,
    ) -> None:
        community, server = _scope(community_id, server_id)
        try:
            await self._storage.rollback_file(
                community, server, _rel_path(rel_path), VersionId(version_id)
            )
        except PathTraversalError as exc:
            raise InvalidFilePathError(rel_path) from exc
        except NotFoundError as exc:
            raise ServerFileNotFoundError(str(server_id.value)) from exc


class _ZipStreamSink:
    """An unseekable byte sink for :class:`zipfile.ZipFile` streaming output.

    ``ZipFile`` treats a sink that is not seekable as a stream: it emits data
    descriptors instead of seeking back to patch each entry's local header, so
    bytes can be flushed out as soon as they are written. The sink buffers what
    ``ZipFile`` writes and hands it off in :meth:`drain`; ``tell`` reports the
    running offset ``ZipFile`` needs for the central directory.
    """

    def __init__(self) -> None:
        self._pending: list[bytes] = []
        self._offset = 0

    def write(self, data: bytes, /) -> int:
        self._pending.append(bytes(data))
        self._offset += len(data)
        return len(data)

    def tell(self) -> int:
        return self._offset

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None

    def seekable(self) -> bool:
        return False

    def drain(self) -> list[bytes]:
        chunks = self._pending
        self._pending = []
        return chunks
