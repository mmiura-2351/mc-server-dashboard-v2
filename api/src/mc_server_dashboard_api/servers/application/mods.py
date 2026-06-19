"""Application use cases for the global mod library (issues #1261, #1264).

Mods are global (not community-scoped). Upload validates the ``.jar`` extension
and size cap, computes SHA-256/SHA-512, parses the embedded manifest, stores the
blob, and persists the metadata row. Identical content (same SHA-256) resolves to
the existing library entry instead of storing a duplicate. Delete checks caller
ownership (uploader or platform admin); download opens a byte stream the HTTP
layer can stream.

Modrinth import (``ImportMod``, issue #1264) reuses the same ingest spine: it
downloads the chosen catalog version's jar, re-parses the *same* manifest (the
jar manifest is the uniform metadata source for both paths), then persists with
``source="modrinth"`` plus the Modrinth project/version ids and published SHA-512.

The client modpack is a later sub-issue of epic #1258 and is out of scope here.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from mc_server_dashboard_api.servers.application.mod_manifest import (
    ParsedModMetadata,
    parse_manifest,
)
from mc_server_dashboard_api.servers.domain.catalog_provider import CatalogProvider
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    InvalidModJarError,
    ModIntegrityError,
    ModInUseError,
    ModNotFoundError,
    PermissionDeniedError,
)
from mc_server_dashboard_api.servers.domain.mod import Mod, ModId, ModSide, ModSource
from mc_server_dashboard_api.servers.domain.mod_store import ByteStream, ModStore
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork

# 256 MiB upload cap for mods, matching the resource pack cap (epic #1258).
MAX_MOD_BYTES = 256 * 1024 * 1024


async def _bytes_stream(data: bytes) -> ByteStream:
    """Wrap ``bytes`` into an ``AsyncIterator[bytes]``."""

    yield data


def _parse_for_library(content: bytes) -> ParsedModMetadata:
    """Parse a jar's manifest, rejecting one with no recognized loader.

    Shared by ``UploadMod`` and ``ImportMod``: a readable jar with no recognized
    manifest parses as ``"unknown"``. Such a mod has no determinable loader, so it
    cannot be deployed (mods/ vs plugins/) and the ``ck_mods_loader_type`` CHECK
    would reject it; reject it here, before the blob is stored, so nothing is
    orphaned.
    """

    parsed = parse_manifest(content)
    if parsed.loader_type == "unknown":
        raise InvalidModJarError(
            "unrecognized mod jar: no fabric/forge/neoforge/quilt/paper manifest found"
        )
    return parsed


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

        parsed = _parse_for_library(content)
        return await _persist_mod(
            uow=self.uow,
            store=self.store,
            clock=self.clock,
            filename=filename,
            display_name=display_name,
            content=content,
            sha256=sha256,
            sha512=hashlib.sha512(content).hexdigest(),
            parsed=parsed,
            # Side: the caller's override wins; otherwise the parser's auto-detected
            # value (which itself defaults to "both" when undetectable).
            side=side if side is not None else parsed.side,
            uploaded_by=uploaded_by,
            source="local",
            source_project_id=None,
            source_version_id=None,
        )


async def _persist_mod(
    *,
    uow: UnitOfWork,
    store: ModStore,
    clock: Clock,
    filename: str,
    display_name: str,
    content: bytes,
    sha256: str,
    sha512: str,
    parsed: ParsedModMetadata,
    side: ModSide,
    uploaded_by: uuid.UUID,
    source: ModSource,
    source_project_id: str | None,
    source_version_id: str | None,
) -> Mod:
    """Store the blob and persist the library row from already-parsed metadata.

    The shared ingest tail of ``UploadMod`` and ``ImportMod``: the callers differ
    only in where the bytes/metadata/source fields come from. Dedup and validation
    happen in the callers before this point.
    """

    # Callers run _parse_for_library first, which rejects "unknown" — so the
    # loader is always a real ModLoader here. Assert to narrow for the typechecker.
    assert parsed.loader_type != "unknown"
    now = clock.now()
    mod_id = ModId.new()
    mod = Mod(
        id=mod_id,
        filename=filename,
        display_name=display_name,
        description=None,
        loader_type=parsed.loader_type,
        mod_identifier=parsed.mod_identifier,
        provides=parsed.provides,
        version_number=parsed.version_number,
        mc_versions=parsed.mc_versions,
        side=side,
        dependencies=parsed.dependencies,
        sha256_hash=sha256,
        sha512_hash=sha512,
        size_bytes=len(content),
        source=source,
        source_project_id=source_project_id,
        source_version_id=source_version_id,
        uploaded_by=uploaded_by,
        created_at=now,
        updated_at=now,
    )

    await store.put(mod_id, filename, _bytes_stream(content))

    async with uow:
        await uow.mods.add(mod)
        await uow.commit()

    return mod


@dataclass(frozen=True)
class ImportMod:
    """Import a Modrinth project/version into the library (issue #1264).

    Resolves the chosen catalog version, downloads its primary jar, re-parses the
    *same* manifest (the uniform metadata source), then persists with
    ``source="modrinth"`` plus the project/version ids and the published SHA-512.

    Validation mirrors ``UploadMod`` (``.jar``/size cap, recognized loader) and
    dedup is the same content address: an identical jar already in the library
    (same SHA-256) resolves to the existing entry rather than storing a duplicate.

    ``side``: when given (the Modrinth-derived deployment signal, the most
    accurate source per epic #1258) it wins over the manifest's auto-detected
    value; when ``None`` the manifest's value is used.
    """

    uow: UnitOfWork
    store: ModStore
    clock: Clock
    catalog: CatalogProvider

    async def __call__(
        self,
        *,
        project_id: str,
        version_id: str,
        imported_by: uuid.UUID,
        side: ModSide | None = None,
    ) -> Mod:
        version = await self.catalog.get_version(version_id)
        if not version.download_url:
            raise InvalidModJarError("catalog version has no downloadable file")

        if not version.filename.lower().endswith(".jar"):
            raise ValueError("catalog version file must end with .jar")

        content = await self.catalog.download(version.download_url)
        if len(content) > MAX_MOD_BYTES:
            raise FileTooLargeError(str(len(content)))

        sha512 = hashlib.sha512(content).hexdigest()
        # Integrity: when the catalog published a SHA-512, the bytes must match it
        # (a corrupted download or tampered CDN object). Fail closed before storing.
        if version.sha512 is not None and version.sha512 != sha512:
            raise ModIntegrityError(version_id)

        sha256 = hashlib.sha256(content).hexdigest()
        async with self.uow:
            existing = await self.uow.mods.get_by_sha256(sha256)
        if existing is not None:
            return existing

        parsed = _parse_for_library(content)
        return await _persist_mod(
            uow=self.uow,
            store=self.store,
            clock=self.clock,
            filename=version.filename,
            display_name=version.name or version.filename,
            content=content,
            sha256=sha256,
            sha512=sha512,
            parsed=parsed,
            side=side if side is not None else parsed.side,
            uploaded_by=imported_by,
            source="modrinth",
            source_project_id=project_id,
            source_version_id=version_id,
        )


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
    """Delete a mod from the library after ownership and in-use validation.

    A mod assigned to any server cannot be deleted (issue #1262): the caller must
    unassign it everywhere first, so a deployed jar never references a vanished
    library entry.
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

            assignments = await self.uow.mods.list_assignments_for_mod(mod_id)
            if assignments:
                raise ModInUseError(str(mod_id.value))

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
