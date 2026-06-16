"""Application use cases for the resource pack library (issue #1176).

Resource packs are global (not community-scoped). Upload validates the file
extension and size cap, computes SHA-1/SHA-256, stores the blob, and persists
the metadata row. Delete guards against packs still assigned to servers and
checks caller ownership (uploader or platform admin). Download opens a byte
stream the HTTP layer can stream.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    PermissionDeniedError,
    ResourcePackInUseError,
    ResourcePackNotFoundError,
)
from mc_server_dashboard_api.servers.domain.resource_pack import (
    ResourcePack,
    ResourcePackId,
)
from mc_server_dashboard_api.servers.domain.resource_pack_store import (
    ByteStream,
    ResourcePackStore,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork

# 256 MiB upload cap for resource packs (issue #1176).
MAX_RESOURCE_PACK_BYTES = 256 * 1024 * 1024


async def _bytes_stream(data: bytes) -> ByteStream:
    """Wrap ``bytes`` into an ``AsyncIterator[bytes]``."""

    yield data


@dataclass(frozen=True)
class UploadResourcePack:
    """Upload a resource pack: validate, hash, store blob, persist metadata."""

    uow: UnitOfWork
    store: ResourcePackStore
    clock: Clock

    async def __call__(
        self,
        *,
        filename: str,
        display_name: str,
        content: bytes,
        uploaded_by: uuid.UUID,
    ) -> ResourcePack:
        if not filename.lower().endswith(".zip"):
            raise ValueError("filename must end with .zip")
        if len(content) > MAX_RESOURCE_PACK_BYTES:
            raise FileTooLargeError(str(len(content)))

        sha1 = hashlib.sha1(content).hexdigest()
        sha256 = hashlib.sha256(content).hexdigest()
        now = self.clock.now()

        pack_id = ResourcePackId.new()
        pack = ResourcePack(
            id=pack_id,
            filename=filename,
            display_name=display_name,
            description=None,
            sha1_hash=sha1,
            sha256_hash=sha256,
            size_bytes=len(content),
            uploaded_by=uploaded_by,
            created_at=now,
            updated_at=now,
        )

        await self.store.put(pack_id, filename, _bytes_stream(content))

        async with self.uow:
            await self.uow.resource_packs.add(pack)
            await self.uow.commit()

        return pack


@dataclass(frozen=True)
class ListResourcePacks:
    """Return all resource packs ordered by display_name."""

    uow: UnitOfWork

    async def __call__(self) -> list[ResourcePack]:
        async with self.uow:
            return await self.uow.resource_packs.list_all()


@dataclass(frozen=True)
class DeleteResourcePack:
    """Delete a resource pack after ownership and in-use validation."""

    uow: UnitOfWork
    store: ResourcePackStore

    async def __call__(
        self,
        *,
        resource_pack_id: ResourcePackId,
        caller_id: uuid.UUID,
        is_platform_admin: bool,
    ) -> None:
        async with self.uow:
            pack = await self.uow.resource_packs.get_by_id(resource_pack_id)
            if pack is None:
                raise ResourcePackNotFoundError(str(resource_pack_id.value))

            # Only the uploader or a platform admin may delete.
            if pack.uploaded_by != caller_id and not is_platform_admin:
                raise PermissionDeniedError(str(resource_pack_id.value))

            assignments = await self.uow.resource_packs.list_assignments_for_pack(
                resource_pack_id
            )
            if assignments:
                raise ResourcePackInUseError(str(resource_pack_id.value))

            await self.store.delete(resource_pack_id)
            await self.uow.resource_packs.delete(resource_pack_id)
            await self.uow.commit()


@dataclass(frozen=True)
class DownloadResourcePack:
    """Open a byte stream for a resource pack."""

    uow: UnitOfWork
    store: ResourcePackStore

    async def __call__(
        self,
        *,
        resource_pack_id: ResourcePackId,
    ) -> tuple[ByteStream, ResourcePack]:
        async with self.uow:
            pack = await self.uow.resource_packs.get_by_id(resource_pack_id)
        if pack is None:
            raise ResourcePackNotFoundError(str(resource_pack_id.value))
        stream = self.store.open(resource_pack_id, pack.filename)
        return stream, pack
