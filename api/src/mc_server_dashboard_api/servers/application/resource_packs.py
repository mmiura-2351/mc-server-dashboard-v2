"""Application use cases for the resource pack library (issues #1176, #1177).

Resource packs are global (not community-scoped). Upload validates the file
extension and size cap, computes SHA-1/SHA-256, stores the blob, and persists
the metadata row. Delete guards against packs still assigned to servers and
checks caller ownership (uploader or platform admin). Download opens a byte
stream the HTTP layer can stream.

Assignment use cases (issue #1177) link a resource pack to a server, managing
the ``server.properties`` keys (``resource-pack``, ``resource-pack-sha1``,
``require-resource-pack``, ``resource-pack-prompt``) and the assignment row.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from urllib.parse import quote

from mc_server_dashboard_api.servers.application.resource_pack_zip import (
    validate_and_normalize,
)
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    PermissionDeniedError,
    ResourcePackInUseError,
    ResourcePackNotFoundError,
    ServerFileNotFoundError,
    ServerFilesUnsettledError,
    ServerNotFoundError,
)
from mc_server_dashboard_api.servers.domain.file_store import FileStore
from mc_server_dashboard_api.servers.domain.lifecycle_lock import (
    LifecycleLock,
    NullLifecycleLock,
)
from mc_server_dashboard_api.servers.domain.resource_pack import (
    ResourcePack,
    ResourcePackAssignment,
    ResourcePackId,
)
from mc_server_dashboard_api.servers.domain.resource_pack_store import (
    ByteStream,
    ResourcePackStore,
)
from mc_server_dashboard_api.servers.domain.server_properties import (
    clear_resource_pack_properties,
    set_resource_pack_properties,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)

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

        # Offloaded to a thread: zip validation/normalization and hashing are
        # CPU-bound over up to MAX_RESOURCE_PACK_BYTES (256 MiB) (issue #1620).
        content = await asyncio.to_thread(validate_and_normalize, content)

        sha1 = (await asyncio.to_thread(hashlib.sha1, content)).hexdigest()
        sha256 = (await asyncio.to_thread(hashlib.sha256, content)).hexdigest()
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


# ---------------------------------------------------------------------------
# Assignment use cases (issue #1177)
# ---------------------------------------------------------------------------


async def _load_server_at_rest(
    uow: UnitOfWork, community_id: CommunityId, server_id: ServerId
) -> None:
    """Validate the server exists, belongs to community, and is at rest."""

    server = await uow.servers.get_by_id(server_id)
    if server is None or server.community_id != community_id:
        raise ServerNotFoundError(str(server_id.value))
    if not server.is_at_rest():
        raise ServerFilesUnsettledError(str(server_id.value))


def _pack_download_url(
    public_base_url: str, pack_id: ResourcePackId, filename: str
) -> str:
    return (
        f"{public_base_url}/api/public/resource-packs/"
        f"{pack_id.value}/{quote(filename, safe='')}"
    )


@dataclass(frozen=True)
class AssignResourcePack:
    """Assign a resource pack to a server (issue #1177).

    Validates server at-rest state, holds the lifecycle lock, reads/writes
    ``server.properties``, and upserts the assignment row.
    """

    uow: UnitOfWork
    file_store: FileStore
    clock: Clock
    lifecycle_lock: LifecycleLock = NullLifecycleLock()

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        resource_pack_id: ResourcePackId,
        require_resource_pack: bool,
        resource_pack_prompt: str | None,
        assigned_by: uuid.UUID,
        public_base_url: str,
    ) -> tuple[ResourcePackAssignment, ResourcePack]:
        async with self.lifecycle_lock.hold(server_id):
            async with self.uow:
                await _load_server_at_rest(self.uow, community_id, server_id)

                pack = await self.uow.resource_packs.get_by_id(resource_pack_id)
                if pack is None:
                    raise ResourcePackNotFoundError(str(resource_pack_id.value))

            # Read server.properties (create empty if absent).
            try:
                props = await self.file_store.read_file(
                    community_id=community_id,
                    server_id=server_id,
                    rel_path="server.properties",
                )
            except ServerFileNotFoundError:
                props = b""

            url = _pack_download_url(public_base_url, pack.id, pack.filename)
            new_props = set_resource_pack_properties(
                props,
                url=url,
                sha1=pack.sha1_hash,
                require=require_resource_pack,
                prompt=resource_pack_prompt,
            )

            await self.file_store.write_file(
                community_id=community_id,
                server_id=server_id,
                rel_path="server.properties",
                content=new_props,
            )

            now = self.clock.now()
            assignment = ResourcePackAssignment(
                server_id=server_id,
                resource_pack_id=resource_pack_id,
                require_resource_pack=require_resource_pack,
                resource_pack_prompt=resource_pack_prompt,
                assigned_by=assigned_by,
                created_at=now,
                updated_at=now,
            )

            async with self.uow:
                # Upsert: delete existing, then add new.
                await self.uow.resource_packs.delete_assignment(server_id)
                await self.uow.resource_packs.add_assignment(assignment)
                await self.uow.commit()

        return assignment, pack


@dataclass(frozen=True)
class UnassignResourcePack:
    """Remove the resource pack assignment from a server (issue #1177).

    Validates server at-rest state, holds the lifecycle lock, clears the
    ``server.properties`` keys, and deletes the assignment row.
    """

    uow: UnitOfWork
    file_store: FileStore
    lifecycle_lock: LifecycleLock = NullLifecycleLock()

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
    ) -> None:
        async with self.lifecycle_lock.hold(server_id):
            async with self.uow:
                await _load_server_at_rest(self.uow, community_id, server_id)

                assignment = await self.uow.resource_packs.get_assignment_by_server(
                    server_id
                )
                if assignment is None:
                    raise ResourcePackNotFoundError(str(server_id.value))

            # Read server.properties (gracefully handle missing).
            try:
                props = await self.file_store.read_file(
                    community_id=community_id,
                    server_id=server_id,
                    rel_path="server.properties",
                )
            except ServerFileNotFoundError:
                props = b""

            new_props = clear_resource_pack_properties(props)

            await self.file_store.write_file(
                community_id=community_id,
                server_id=server_id,
                rel_path="server.properties",
                content=new_props,
            )

            async with self.uow:
                await self.uow.resource_packs.delete_assignment(server_id)
                await self.uow.commit()


@dataclass(frozen=True)
class GetResourcePackAssignment:
    """Return the resource pack assignment for a server, or None (issue #1177)."""

    uow: UnitOfWork

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
    ) -> tuple[ResourcePackAssignment, ResourcePack] | None:
        async with self.uow:
            server = await self.uow.servers.get_by_id(server_id)
            if server is None or server.community_id != community_id:
                raise ServerNotFoundError(str(server_id.value))

            assignment = await self.uow.resource_packs.get_assignment_by_server(
                server_id
            )
            if assignment is None:
                return None

            pack = await self.uow.resource_packs.get_by_id(assignment.resource_pack_id)
            if pack is None:
                return None

            return assignment, pack
