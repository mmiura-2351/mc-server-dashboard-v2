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
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from mc_server_dashboard_api.servers.application.platform_properties import (
    pack_download_url,
    read_properties,
)
from mc_server_dashboard_api.servers.application.resource_pack_zip import (
    validate_and_normalize,
)
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    PermissionDeniedError,
    ResourcePackInUseError,
    ResourcePackNotFoundError,
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
    ResourcePackProperties,
    apply_platform_properties,
    clear_resource_pack_properties,
    new_rcon_password,
    remove_keys,
    set_resource_pack_properties,
)
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)

logger = logging.getLogger(__name__)

# 256 MiB upload cap for resource packs (issue #1176).
MAX_RESOURCE_PACK_BYTES = 256 * 1024 * 1024

# The one platform-managed key ``set_resource_pack_properties`` leaves alone when
# the assignment carries no prompt; an assign then has to remove it explicitly, or
# a previous assignment's prompt outlives the row that justified it (issue #2792).
_RESOURCE_PACK_PROMPT_KEY = "resource-pack-prompt"


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

            # Commit the DB delete first: an orphaned blob on later failure is
            # benign, but destroyed bytes with a surviving row strand a
            # still-referenced pack (issue #1962).
            await self.uow.resource_packs.delete(resource_pack_id)
            await self.uow.commit()

        # Best-effort blob removal — the DB row is already gone, so a failure
        # here leaves an orphaned blob which is harmless.
        try:
            await self.store.delete(resource_pack_id)
        except Exception:
            logger.warning(
                "blob delete failed for resource pack %s; orphaned",
                resource_pack_id.value,
                exc_info=True,
            )


@dataclass(frozen=True)
class DownloadResourcePack:
    """Open a byte stream for a resource pack.

    Returns ``(stream, pack, size_bytes)`` so the edge can declare a
    ``Content-Length`` (issue #2317). The size is read from the blob store, never
    from ``ResourcePack.size_bytes`` — the declared length must equal the streamed
    byte count exactly, and only the store knows what it is about to stream.

    ``expected_filename`` is the filename a caller addressed the pack by (the
    public route's path segment); a mismatch is reported as a missing pack, and
    the caller that omits it accepts whatever filename the row carries.
    """

    uow: UnitOfWork
    store: ResourcePackStore

    async def __call__(
        self,
        *,
        resource_pack_id: ResourcePackId,
        expected_filename: str | None = None,
    ) -> tuple[ByteStream, ResourcePack, int]:
        async with self.uow:
            pack = await self.uow.resource_packs.get_by_id(resource_pack_id)
        if pack is None:
            raise ResourcePackNotFoundError(str(resource_pack_id.value))
        # Match the filename off the row already read, before any store round
        # trip: the public route is unauthenticated, so a wrong filename must not
        # cost a storage request (issue #2322).
        if expected_filename is not None and expected_filename != pack.filename:
            raise ResourcePackNotFoundError(str(resource_pack_id.value))
        # Size first: ``open`` is an async-generator factory that touches storage
        # only on first iteration, so opening first would leave the stream
        # unconsumed should ``size`` raise.
        size_bytes = await self.store.size(resource_pack_id, pack.filename)
        stream = self.store.open(resource_pack_id, pack.filename)
        return stream, pack, size_bytes


# ---------------------------------------------------------------------------
# Assignment use cases (issue #1177)
# ---------------------------------------------------------------------------


async def _load_server_at_rest(
    uow: UnitOfWork, community_id: CommunityId, server_id: ServerId
) -> Server:
    """Return the server, validating it exists, is in community, and is at rest."""

    server = await uow.servers.get_by_id(server_id)
    if server is None or server.community_id != community_id:
        raise ServerNotFoundError(str(server_id.value))
    if not server.is_at_rest():
        raise ServerFilesUnsettledError(str(server_id.value))
    return server


@dataclass(frozen=True)
class AssignResourcePack:
    """Assign a resource pack to a server (issue #1177).

    Validates server at-rest state, holds the lifecycle lock, reads/writes
    ``server.properties``, and upserts the assignment row.

    An assignment with no prompt removes any ``resource-pack-prompt`` line the
    previous assignment left behind (issue #2792): the row now says "no prompt", and
    :func:`set_resource_pack_properties` alone would leave the stale line to drift
    from it until the next restore re-applied the DB's view.

    A server with no ``server.properties`` at all gets the WHOLE platform half
    seeded, pack keys included (issue #2810) — writing only the pack keys would
    publish a file with no ``rcon.password`` for the worker to reach the server with
    and no ``server-port``, so the server would silently bind Mojang's 25565. An
    existing file is left otherwise untouched, exactly as before.
    """

    uow: UnitOfWork
    file_store: FileStore
    clock: Clock
    lifecycle_lock: LifecycleLock = NullLifecycleLock()
    # Fills in ``rcon.password`` when the platform half is seeded from scratch
    # (issue #2810). Injected so tests are deterministic.
    token_generator: Callable[[], str] = field(default=new_rcon_password)

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
                server = await _load_server_at_rest(self.uow, community_id, server_id)

                pack = await self.uow.resource_packs.get_by_id(resource_pack_id)
                if pack is None:
                    raise ResourcePackNotFoundError(str(resource_pack_id.value))

            props = await read_properties(
                self.file_store, community_id=community_id, server_id=server_id
            )

            url = pack_download_url(public_base_url, pack.id, pack.filename)
            if props is None:
                # No file to preserve: seed the whole platform half, the pack keys
                # included (issue #2810). The pack values come from THIS call's
                # arguments, never from the assignment row -- the row is upserted
                # below, so reading it here would write the previous assignment.
                new_props = apply_platform_properties(
                    b"",
                    game_port=server.game_port,
                    rcon_password=self.token_generator(),
                    resource_pack=ResourcePackProperties(
                        url=url,
                        sha1=pack.sha1_hash,
                        require=require_resource_pack,
                        prompt=resource_pack_prompt,
                    ),
                )
            else:
                new_props = set_resource_pack_properties(
                    props,
                    url=url,
                    sha1=pack.sha1_hash,
                    require=require_resource_pack,
                    prompt=resource_pack_prompt,
                )
                if resource_pack_prompt is None:
                    # The row says "no prompt", so the file must not keep a
                    # previous assignment's prompt line (issue #2792).
                    new_props = remove_keys(new_props, {_RESOURCE_PACK_PROMPT_KEY})

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

    A server with no ``server.properties`` has no pack keys to clear, so the file
    write is skipped entirely and only the row goes (issue #2810). Clearing from
    empty would publish a lone-newline file whose platform half is missing
    altogether — strictly worse than the absent file it replaces.
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

            props = await read_properties(
                self.file_store, community_id=community_id, server_id=server_id
            )
            if props is not None:
                await self.file_store.write_file(
                    community_id=community_id,
                    server_id=server_id,
                    rel_path="server.properties",
                    content=clear_resource_pack_properties(props),
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
