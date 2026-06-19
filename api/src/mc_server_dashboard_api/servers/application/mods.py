"""Application use cases for the global mod library (issue #1261).

Mods are global (not community-scoped). Upload validates the ``.jar`` extension
and size cap, computes SHA-256/SHA-512, parses the embedded manifest, stores the
blob, and persists the metadata row. Identical content (same SHA-256) resolves to
the existing library entry instead of storing a duplicate. Delete checks caller
ownership (uploader or platform admin); download opens a byte stream the HTTP
layer can stream.

Server assignment, Modrinth import, and the client modpack are later sub-issues
of epic #1258 and are out of scope here.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from mc_server_dashboard_api.servers.application.mod_manifest import parse_manifest
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    ModNotFoundError,
    PermissionDeniedError,
)
from mc_server_dashboard_api.servers.domain.mod import Mod, ModId, ModSide
from mc_server_dashboard_api.servers.domain.mod_store import ByteStream, ModStore
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork

# 256 MiB upload cap for mods, matching the resource pack cap (epic #1258).
MAX_MOD_BYTES = 256 * 1024 * 1024


async def _bytes_stream(data: bytes) -> ByteStream:
    """Wrap ``bytes`` into an ``AsyncIterator[bytes]``."""

    yield data


@dataclass(frozen=True)
class UploadMod:
    """Upload a mod jar: validate, hash, parse manifest, store blob, persist.

    Content-addressed dedup: an identical upload (same SHA-256) resolves to the
    existing library entry rather than storing a duplicate.
    """

    uow: UnitOfWork
    store: ModStore
    clock: Clock

    async def __call__(
        self,
        *,
        filename: str,
        display_name: str,
        content: bytes,
        uploaded_by: uuid.UUID,
        side: ModSide | None = None,
    ) -> Mod:
        if not filename.lower().endswith(".jar"):
            raise ValueError("filename must end with .jar")
        if len(content) > MAX_MOD_BYTES:
            raise FileTooLargeError(str(len(content)))

        sha256 = hashlib.sha256(content).hexdigest()

        # Dedup: an identical upload returns the existing library entry.
        async with self.uow:
            existing = await self.uow.mods.get_by_sha256(sha256)
        if existing is not None:
            return existing

        sha512 = hashlib.sha512(content).hexdigest()
        parsed = parse_manifest(content)
        now = self.clock.now()

        mod_id = ModId.new()
        mod = Mod(
            id=mod_id,
            filename=filename,
            display_name=display_name,
            description=None,
            # The parser returns "unknown" for an unreadable/absent manifest; the
            # column type narrows to ModLoader, so an unknown loader is not a valid
            # library entry yet -- but the foundation keeps it permissive and the
            # caller may override later (epic #1258). Persist the parsed value.
            loader_type=parsed.loader_type,  # type: ignore[arg-type]
            mod_identifier=parsed.mod_identifier,
            provides=parsed.provides,
            version_number=parsed.version_number,
            mc_versions=parsed.mc_versions,
            # Side: the caller's override wins; otherwise the parser's auto-detected
            # value (which itself defaults to "both" when undetectable).
            side=side if side is not None else parsed.side,
            dependencies=parsed.dependencies,
            sha256_hash=sha256,
            sha512_hash=sha512,
            size_bytes=len(content),
            source="local",
            source_project_id=None,
            source_version_id=None,
            uploaded_by=uploaded_by,
            created_at=now,
            updated_at=now,
        )

        await self.store.put(mod_id, filename, _bytes_stream(content))

        async with self.uow:
            await self.uow.mods.add(mod)
            await self.uow.commit()

        return mod


@dataclass(frozen=True)
class ListMods:
    """Return library mods ordered by display_name, optionally filtered."""

    uow: UnitOfWork

    async def __call__(
        self,
        *,
        loader_type: str | None = None,
        mc_version: str | None = None,
        side: str | None = None,
    ) -> list[Mod]:
        async with self.uow:
            return await self.uow.mods.list_all(
                loader_type=loader_type,  # type: ignore[arg-type]
                mc_version=mc_version,
                side=side,  # type: ignore[arg-type]
            )


@dataclass(frozen=True)
class DeleteMod:
    """Delete a mod from the library after ownership validation.

    Issue #1262 will add the "refuse delete while assigned to a server" guard; the
    ``server_mods`` assignment table does not exist yet, so this deletes
    unconditionally.
    """

    uow: UnitOfWork
    store: ModStore

    async def __call__(
        self,
        *,
        mod_id: ModId,
        caller_id: uuid.UUID,
        is_platform_admin: bool,
    ) -> None:
        async with self.uow:
            mod = await self.uow.mods.get_by_id(mod_id)
            if mod is None:
                raise ModNotFoundError(str(mod_id.value))

            # Only the uploader or a platform admin may delete.
            if mod.uploaded_by != caller_id and not is_platform_admin:
                raise PermissionDeniedError(str(mod_id.value))

            await self.store.delete(mod_id)
            await self.uow.mods.delete(mod_id)
            await self.uow.commit()


@dataclass(frozen=True)
class DownloadMod:
    """Open a byte stream for a mod jar."""

    uow: UnitOfWork
    store: ModStore

    async def __call__(
        self,
        *,
        mod_id: ModId,
    ) -> tuple[ByteStream, Mod]:
        async with self.uow:
            mod = await self.uow.mods.get_by_id(mod_id)
        if mod is None:
            raise ModNotFoundError(str(mod_id.value))
        stream = self.store.open(mod_id, mod.filename)
        return stream, mod
