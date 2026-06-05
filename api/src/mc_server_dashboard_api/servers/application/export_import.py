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

Failure posture (import): the row commits first, then the working set is
published. A publish failure mid-way reuses the #243/#252 seed-failure posture --
:class:`WorkingSetSeedFailedError` (503 ``seed_failed``) -- leaving a committed
but degraded row that is repairable via the files API, rather than an unmapped
500.

Legacy incompatibility: legacy exports carry a different metadata shape. This
slice does NOT implement compatibility; the format is documented (DEPLOYMENT.md)
so a converter can be written later.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass

from mc_server_dashboard_api.servers.application.files import (
    MAX_ARCHIVE_ENTRIES,
    MAX_UPLOAD_BYTES,
    _archive_entries,
)
from mc_server_dashboard_api.servers.application.manage_server import CreateServer
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
    own eula.txt if any). After the row commits, the archive contents are
    published as the initial working set through the hardened extraction
    (``_archive_entries``: zip-slip, size, and entry-count caps), with the
    metadata member itself excluded. The name comes from the caller, not the
    metadata.

    The caps are fields (not bare constants) so a test can inject tiny caps and
    trip the size / entry-count guards with a small archive; production wiring uses
    the defaults.
    """

    create_server: CreateServer
    file_store: FileStore
    max_bytes: int = MAX_UPLOAD_BYTES
    max_entries: int = MAX_ARCHIVE_ENTRIES

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

        Reuses the hardened extraction (``_archive_entries``) for the zip-slip /
        size / entry-count caps, skipping the metadata member so it never lands in
        the working set. A cap breach (413) or an unsafe member path (422) is a
        property of the archive and is surfaced as-is. Any other storage failure
        mid-publish reuses the #243/#252 seed-failure posture: the committed row
        stays (degraded but repairable via the files API) and the failure surfaces
        as a mapped 503, never an unmapped 500.
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
                await self.file_store.write_file(
                    community_id=community_id,
                    server_id=server_id,
                    rel_path=entry_path,
                    content=data,
                )
        except (FileTooLargeError, InvalidFilePathError):
            # A cap breach (413) or an unsafe member path (422) is a property of
            # the archive, surfaced to the caller as-is -- not a seed failure.
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
