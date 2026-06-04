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
