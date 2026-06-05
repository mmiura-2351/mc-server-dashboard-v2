"""Whole-server ZIP export / import use cases (M2 Epic C2, issue #274).

Move a whole server in and out as a ZIP archive: the export streams the
authoritative working set plus an ``export_metadata.json`` descriptor; the import
parses that descriptor, validates and creates a fresh server row, and publishes
the archive contents as the new server's initial working set.

Both reuse the C1 machinery rather than re-implementing it:

- **Export** reuses the #262 streaming zip sink (``FileStore.export_dir``), so the
  zip is built incrementally and peak memory is bounded by one in-flight file.
  Export is at-rest only (running -> :class:`ServerFilesUnsettledError` -> 409):
  the authoritative copy is only well-defined at rest, matching the download
  posture. ``exported_at`` reads the :class:`Clock` seam, never the wall clock.
- **Import** reuses the #267 version validator (the SAME check create runs, so
  spigot / forge / unknown-version are rejected identically), the #243 port
  auto-assign (via :class:`CreateServer`'s composition), and the hardened
  extraction (``_archive_entries``: zip-slip, size, and entry-count caps). The
  ``export_metadata.json`` member itself is NOT written into the working set. The
  name comes from the request, not the metadata (uniqueness 409 as usual);
  ``accept_eula`` is never implied (the imported working set may carry its own
  eula.txt).

Failure posture (import): the archive is fully validated (metadata shape AND the
hardened entry checks — zip-slip, size, entry-count) BEFORE the row commits, so a
hostile archive is rejected (413/422) with NO server row created (issue #277).
Only a genuine storage failure during the post-commit write pass can leave a
committed row behind; that mid-write class reuses the #243/#252 seed-failure
posture -- :class:`WorkingSetSeedFailedError` (503 ``seed_failed``) -- leaving a
committed but degraded row that is repairable via the files API, rather than an
unmapped 500. It is now the ONLY post-commit failure class.

Legacy incompatibility: legacy exports carry a different metadata shape. This
slice does NOT implement compatibility; the format is documented (DEPLOYMENT.md)
so a converter can be written later.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from mc_server_dashboard_api.servers.application.files import (
    MAX_ARCHIVE_ENTRIES,
    MAX_UPLOAD_BYTES,
    _archive_entries,
    _validate_archive,
)
from mc_server_dashboard_api.servers.application.manage_server import (
    CreateServer,
    _generate_rcon_password,
)
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    InvalidExportMetadataError,
    InvalidFilePathError,
    ServerFilesUnsettledError,
    ServerNotFoundError,
    WorkingSetSeedFailedError,
)
from mc_server_dashboard_api.servers.domain.file_store import FileStore
from mc_server_dashboard_api.servers.domain.server_properties import apply_rcon_settings
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId

_logger = logging.getLogger(__name__)

# The export-format version (the metadata ``format`` field). Bumped only on a
# breaking change to the descriptor shape; the import refuses any other value so a
# legacy / future archive is rejected loudly rather than misread (issue #274).
EXPORT_FORMAT_VERSION = 1

# The metadata member name. Carried inside the zip at the root; export writes it
# and import reads + strips it (it never lands in the new working set). A working
# set that already holds a file by this name would collide, so import treats the
# member as metadata only and never materializes it.
EXPORT_METADATA_FILENAME = "export_metadata.json"

# The archive member that carries a server's RCON / port settings. When an
# imported archive includes it, the three RCON keys are enforced on the way in so
# the console / graceful-stop path works out of the box (issue #335); when the
# archive omits it, create's own seeding (which also enables RCON) survives.
_PROPERTIES_REL_PATH = "server.properties"

# The cap on the (decompressed) metadata descriptor. The descriptor is a handful
# of short string fields, so 64 KiB is generous; the bound exists only to refuse a
# decompression-bomb metadata member before it is materialized in memory. The
# member is read in chunks so the guard trips mid-decompression, not after.
_MAX_METADATA_BYTES = 64 * 1024
_METADATA_CHUNK_BYTES = 16 * 1024


async def _load(
    uow: UnitOfWork, community_id: CommunityId, server_id: ServerId
) -> Server:
    server = await uow.servers.get_by_id(server_id)
    if server is None or server.community_id != community_id:
        raise ServerNotFoundError(str(server_id.value))
    return server


@dataclass(frozen=True)
class ExportServer:
    """Stream a whole server as a ZIP (working set + metadata) at rest (file:read).

    At rest only (Section 6.9): a running server is
    :class:`ServerFilesUnsettledError` (the edge returns 409), matching the
    directory-download posture -- the authoritative copy is the export source and
    is only well-defined at rest. The zip is the working-set root (built
    incrementally over the #262 streaming sink) plus a single
    ``export_metadata.json`` descriptor whose ``exported_at`` is read from the
    :class:`Clock` seam.
    """

    uow: UnitOfWork
    clock: Clock
    file_store: FileStore

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> AsyncIterator[bytes]:
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
        if not server.is_at_rest():
            raise ServerFilesUnsettledError(str(server_id.value))

        metadata = {
            "format": EXPORT_FORMAT_VERSION,
            "name": server.name.value,
            "mc_edition": server.mc_edition,
            "mc_version": server.mc_version,
            "server_type": server.server_type.value,
            "exported_at": self.clock.now().isoformat(),
        }
        metadata_bytes = json.dumps(metadata, indent=2).encode("utf-8")
        return self.file_store.export_dir(
            community_id=community_id,
            server_id=server_id,
            rel_path=".",
            extra=[(EXPORT_METADATA_FILENAME, metadata_bytes)],
        )


@dataclass(frozen=True)
class ImportServer:
    """Create a fresh server from a ZIP export and publish its working set.

    Parses the archive's ``export_metadata.json`` (rejecting a wrong/missing
    format version or a malformed descriptor as
    :class:`InvalidExportMetadataError` -> 422), then composes the existing
    :class:`CreateServer` use case so the SAME validation (server_type/version via
    the #267 validator, edition, name uniqueness) and the #243 port auto-assign
    apply -- ``accept_eula`` is left false (the imported working set carries its
    own eula.txt if any).

    The whole archive is then validated in a dry-run pass (the hardened
    ``_archive_entries`` checks: zip-slip, size, and entry-count caps) BEFORE the
    row is created, so a hostile archive 413/422s with no server row left behind
    (issue #277). Only after both the metadata and the entries validate does the
    row commit; the write pass then publishes the entries as the initial working
    set, with the metadata member itself excluded. The name comes from the caller,
    not the metadata.

    The imported ``server.properties`` (if any) has the three RCON keys enforced on
    the way in (``enable-rcon=true``, ``rcon.port=25575``, and a generated
    ``rcon.password`` when the archive's is blank), so ``/command`` works out of
    the box (issue #335); an archive that omits ``server.properties`` keeps the
    RCON-enabled file create's own seeding wrote.

    The caps are fields (not bare constants) so a test can inject tiny caps and
    trip the size / entry-count guards with a small archive; production wiring uses
    the defaults.
    """

    create_server: CreateServer
    file_store: FileStore
    max_bytes: int = MAX_UPLOAD_BYTES
    max_entries: int = MAX_ARCHIVE_ENTRIES
    rcon_password_factory: Callable[[], str] = field(default=_generate_rcon_password)

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        name: str,
        execution_backend: str,
        content: bytes,
    ) -> Server:
        if len(content) > self.max_bytes:
            raise FileTooLargeError(str(len(content)))

        metadata = _parse_metadata(content)
        # Validate the whole archive (zip-slip / size / entry-count) BEFORE the row
        # is created, so a hostile archive 413/422s with no server row left behind
        # (issue #277). The metadata member is included in this pass (it is a normal
        # archive entry); only its write is skipped later. The body bytes are
        # already in memory, so the dry-run pass is cheap.
        _validate_archive(
            EXPORT_METADATA_FILENAME + ".zip",  # route to the zip branch
            content,
            max_bytes=self.max_bytes,
            max_entries=self.max_entries,
        )
        # Create the row through the shared use case: it runs the version validator
        # (spigot/forge/unknown-version -> 422), assigns the game port (#243), and
        # enforces name uniqueness (409). accept_eula stays false on purpose.
        server = await self.create_server(
            community_id=community_id,
            name=name,
            mc_edition=metadata.mc_edition,
            mc_version=metadata.mc_version,
            server_type=metadata.server_type,
            execution_backend=execution_backend,
            config={},
            accept_eula=False,
        )
        await self._publish_working_set(
            community_id=community_id, server_id=server.id, content=content
        )
        return server

    async def _publish_working_set(
        self, *, community_id: CommunityId, server_id: ServerId, content: bytes
    ) -> None:
        """Write the archive members into the new server's first published version.

        Runs only after :meth:`__call__`'s pre-commit validate pass has cleared the
        whole archive, so the entries here are already known-safe; re-running the
        SAME hardened extraction (``_archive_entries``) keeps the streamed decode in
        one place and skips the metadata member so it never lands in the working
        set. The archive-property cap/path rejections (413/422) cannot fire here
        (the validate pass already raised them, before the row existed) but the
        re-raise is kept defensively. The ONLY genuine post-commit failure is a
        storage write error mid-publish: it reuses the #243/#252 seed-failure
        posture -- the committed row stays (degraded but repairable via the files
        API) and the failure surfaces as a mapped 503, never an unmapped 500.
        """

        try:
            for entry_path, data in _archive_entries(
                EXPORT_METADATA_FILENAME + ".zip",  # route to the zip branch
                content,
                max_bytes=self.max_bytes,
                max_entries=self.max_entries,
            ):
                if entry_path == EXPORT_METADATA_FILENAME:
                    continue
                if entry_path == _PROPERTIES_REL_PATH:
                    # The imported properties may have RCON off / blank; enforce the
                    # three RCON keys so /command works out of the box (issue #335).
                    # An importer's known password survives; a blank one is filled.
                    data = apply_rcon_settings(data, self.rcon_password_factory())
                await self.file_store.write_file(
                    community_id=community_id,
                    server_id=server_id,
                    rel_path=entry_path,
                    content=data,
                )
        except (FileTooLargeError, InvalidFilePathError):
            # Defensive: the pre-commit validate pass already rejected these as an
            # archive property (413/422) with no row created, so they should not
            # reach here; re-raise as-is rather than mask them as a seed failure.
            raise
        except Exception as exc:
            _logger.warning(
                "import working-set publish failed; server row committed but "
                "working set is unpublished (repairable via files API)",
                extra={"server_id": str(server_id.value)},
            )
            raise WorkingSetSeedFailedError(str(server_id.value)) from exc


@dataclass(frozen=True)
class _Metadata:
    mc_edition: str
    mc_version: str
    server_type: str


def _parse_metadata(content: bytes) -> _Metadata:
    """Read and validate ``export_metadata.json`` from the zip's root.

    Raises :class:`InvalidExportMetadataError` (422) for a non-zip body, a missing
    or unreadable metadata member, malformed JSON, a wrong/missing ``format``
    version (legacy incompatibility is loud, not silent), or a missing required
    field. The server_type / version themselves are validated downstream by the
    shared create path (the #267 validator), so this only shape-checks them.
    """

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            try:
                info = zf.getinfo(EXPORT_METADATA_FILENAME)
            except KeyError as exc:
                raise InvalidExportMetadataError("missing metadata") from exc
            # Read the descriptor in bounded chunks rather than ``zf.read`` so a
            # decompression-bomb metadata member cannot be fully materialized in
            # memory before a size check (the same defence the working-set
            # extraction applies). The descriptor is tiny by design.
            raw = bytearray()
            with zf.open(info) as reader:
                while True:
                    chunk = reader.read(_METADATA_CHUNK_BYTES)
                    if not chunk:
                        break
                    raw += chunk
                    if len(raw) > _MAX_METADATA_BYTES:
                        raise InvalidExportMetadataError("metadata too large")
    except zipfile.BadZipFile as exc:
        raise InvalidExportMetadataError("not a zip archive") from exc

    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidExportMetadataError("malformed metadata json") from exc
    if not isinstance(parsed, dict):
        raise InvalidExportMetadataError("metadata is not an object")
    if parsed.get("format") != EXPORT_FORMAT_VERSION:
        raise InvalidExportMetadataError(
            f"unsupported format: {parsed.get('format')!r}"
        )

    try:
        mc_edition = parsed["mc_edition"]
        mc_version = parsed["mc_version"]
        server_type = parsed["server_type"]
    except KeyError as exc:
        raise InvalidExportMetadataError(f"missing field: {exc.args[0]}") from exc
    if not all(
        isinstance(value, str) for value in (mc_edition, mc_version, server_type)
    ):
        raise InvalidExportMetadataError("metadata field is not a string")
    return _Metadata(
        mc_edition=mc_edition, mc_version=mc_version, server_type=server_type
    )
