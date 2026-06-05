"""File-management use cases with state branching (Section 6.9, 6.10).

These run after the route's authorization dependency admitted the caller, so they
assume an authorized member and only do the file work. Reads and edits branch on
server state per the 6.9 table:

- **at rest** (``server.is_at_rest()``: desired=stopped, observed in
  {stopped, unknown}) → the authoritative Storage copy, through the
  :class:`FileStore` seam. Edits are versioned (FR-FILE-3) and history/rollback
  act here.
- **running** (desired=running, observed=running, a worker assigned) → the
  Worker's live working set, through the :class:`ControlPlane` seam's
  ReadFile/EditFile (ARCHITECTURE.md Section 7.2). A disconnected worker surfaces
  :class:`WorkerUnavailableError` (the edge returns 503).
- **anything else** (starting/stopping/restarting/crashed, or a desired/observed
  mismatch) → :class:`ServerFilesUnsettledError` (the edge returns 409): neither
  resting target is well-defined.

Browsing (:class:`ListDir`) branches like read/edit: a running server lists its
live working set via the control plane's ListFiles, a server at rest reads
Storage (issue #121 closes the RPO-stale-listing gap). History
(:class:`ListFileVersions`) and rollback (:class:`RollbackFile`) stay
authoritative-only regardless of run state: versions exist only on the
authoritative copy, so history reads Storage even while running, and rollback
additionally requires the server at rest (it republishes the authoritative copy,
which would diverge from a live working set) and is 409 while running. The control
plane carries no version or rollback command (CONTROL_PLANE.md Section 5 table).

Edits are bounded to :data:`MAX_EDIT_BYTES`: file access rides the control plane
for small, interactive edits (ARCHITECTURE.md Section 7.2), so a multi-MiB write
is refused at the edge before any dispatch or Storage write.
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass

from mc_server_dashboard_api.servers.application.command_dispatch import (
    dispatch_failure,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
    ControlPlane,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    FileTooLargeError,
    InvalidFilePathError,
    ServerFileNotFoundError,
    ServerFilesUnsettledError,
    ServerNotFoundError,
    ServerNotStoppedError,
)
from mc_server_dashboard_api.servers.domain.file_store import FileEntry, FileStore
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
)

# The edit-size cap. File access rides the control plane for small, interactive
# edits (server.properties, ops.json, a datapack file), not bulk world data —
# that moves on the data plane. 4 MiB comfortably covers config/text edits while
# keeping a single edit off the latency-sensitive stream's danger zone. A constant
# is intentional (no config knob requested); document if it ever needs tuning.
MAX_EDIT_BYTES = 4 * 1024 * 1024

# The upload-size cap. Unlike an interactive edit (which rides the control plane,
# hence the small MAX_EDIT_BYTES), an upload is an at-rest Storage-side operation
# that bypasses the control-plane edit path, so it carries a much larger cap:
# whole mods/datapacks/world regions are plausible payloads. 512 MiB bounds a
# single upload (and a single extracted archive's total) while keeping one upload
# off pathological memory use. A constant is intentional (no config knob
# requested); document if it ever needs tuning.
MAX_UPLOAD_BYTES = 512 * 1024 * 1024


async def _load(
    uow: UnitOfWork, community_id: CommunityId, server_id: ServerId
) -> Server:
    server = await uow.servers.get_by_id(server_id)
    if server is None or server.community_id != community_id:
        raise ServerNotFoundError(str(server_id.value))
    return server


def _is_running(server: Server) -> bool:
    return (
        server.desired_state is DesiredState.RUNNING
        and server.observed_state is ObservedState.RUNNING
        and server.assigned_worker_id is not None
    )


def _map_file_status(server_id: ServerId, kind: str, outcome: CommandOutcome) -> None:
    """Translate a Worker file-command failure to a servers file error.

    ``kind`` is the command label (the calling use case) so an unmapped failure
    is logged once with server and kind context before raising (issue #200).
    """

    if outcome.status is CommandStatus.SERVER_NOT_FOUND:
        # The Worker reports a missing target file (or no such running server);
        # either way the path the caller asked for is not there.
        raise ServerFileNotFoundError(str(server_id.value))
    if outcome.status is CommandStatus.FILE_ACCESS_DENIED:
        raise InvalidFilePathError(outcome.message or str(server_id.value))
    raise dispatch_failure(server_id=server_id, kind=kind, outcome=outcome)


@dataclass(frozen=True)
class ReadFile:
    """Read a file, branching at-rest -> Storage / running -> Worker (file:read)."""

    uow: UnitOfWork
    control_plane: ControlPlane
    file_store: FileStore

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> bytes:
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)

        if server.is_at_rest():
            return await self.file_store.read_file(
                community_id=community_id, server_id=server_id, rel_path=rel_path
            )
        if _is_running(server):
            self.file_store.validate_rel_path(rel_path)
            outcome = await self.control_plane.read_file(
                worker_id=server.assigned_worker_id,  # type: ignore[arg-type]
                server_id=server_id,
                rel_path=rel_path,
            )
            if not outcome.success:
                _map_file_status(server_id, "ReadFile", outcome)
            return outcome.file_content
        raise ServerFilesUnsettledError(str(server_id.value))


@dataclass(frozen=True)
class DirListing:
    """A directory listing returned by :class:`ListDir`.

    ``truncated`` is set only on the running path when the Worker clipped the
    listing to its per-listing cap; the at-rest Storage path is never truncated.
    """

    entries: list[FileEntry]
    truncated: bool = False


@dataclass(frozen=True)
class ListDir:
    """Browse a directory, branching at-rest -> Storage / running -> Worker (file:read).

    For a running server the listing comes from the Worker's live working set via
    ListFiles over the control plane (closing the RPO-stale-listing gap, issue
    #121); for a server at rest it reads the authoritative Storage copy. Both
    sources yield the same :class:`FileEntry` shape (name / is_dir / size), so the
    caller cannot tell them apart. A disconnected worker surfaces
    :class:`WorkerUnavailableError` (the edge returns 503), matching read/edit.
    """

    uow: UnitOfWork
    control_plane: ControlPlane
    file_store: FileStore

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> DirListing:
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)

        if server.is_at_rest():
            entries = await self.file_store.list_dir(
                community_id=community_id, server_id=server_id, rel_path=rel_path
            )
            return DirListing(entries=entries, truncated=False)
        if _is_running(server):
            self.file_store.validate_rel_path(rel_path)
            outcome = await self.control_plane.list_files(
                worker_id=server.assigned_worker_id,  # type: ignore[arg-type]
                server_id=server_id,
                rel_path=rel_path,
            )
            if not outcome.success:
                _map_file_status(server_id, "ListDir", outcome)
            listing = outcome.listing
            cp_entries = () if listing is None else listing.entries
            return DirListing(
                entries=[
                    FileEntry(name=e.name, is_dir=e.is_dir, size=e.size)
                    for e in cp_entries
                ],
                truncated=False if listing is None else listing.truncated,
            )
        raise ServerFilesUnsettledError(str(server_id.value))


@dataclass(frozen=True)
class WriteFile:
    """Edit a file, branching at-rest -> Storage / running -> Worker (file:edit)."""

    uow: UnitOfWork
    control_plane: ControlPlane
    file_store: FileStore

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: str,
        content: bytes,
    ) -> None:
        if len(content) > MAX_EDIT_BYTES:
            raise FileTooLargeError(str(len(content)))

        async with self.uow:
            server = await _load(self.uow, community_id, server_id)

        if server.is_at_rest():
            await self.file_store.write_file(
                community_id=community_id,
                server_id=server_id,
                rel_path=rel_path,
                content=content,
            )
            return
        if _is_running(server):
            self.file_store.validate_rel_path(rel_path)
            outcome = await self.control_plane.edit_file(
                worker_id=server.assigned_worker_id,  # type: ignore[arg-type]
                server_id=server_id,
                rel_path=rel_path,
                content=content,
            )
            if not outcome.success:
                _map_file_status(server_id, "WriteFile", outcome)
            return
        # A crashed server (desired=RUNNING, observed=CRASHED) lands here -> 409,
        # not an at-rest Storage edit. Rationale: every (re)start hydrates the
        # working set from the authoritative copy, but a subsequent Stop dispatches
        # a final snapshot of the crashed working set, which would CLOBBER any
        # authoritative edit made while crashed. Requiring stop-first guarantees
        # that final snapshot lands before any at-rest edit. (A smarter crashed-edit
        # flow is possible post-M1.)
        raise ServerFilesUnsettledError(str(server_id.value))


@dataclass(frozen=True)
class ListFileVersions:
    """List retained prior versions of a file (file:history, FR-FILE-3).

    History lives only on the authoritative copy; a running server still answers
    from Storage (versions are produced by authoritative-copy edits).
    """

    uow: UnitOfWork
    file_store: FileStore

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> list[str]:
        async with self.uow:
            await _load(self.uow, community_id, server_id)
        return await self.file_store.list_versions(
            community_id=community_id, server_id=server_id, rel_path=rel_path
        )


@dataclass(frozen=True)
class RollbackFile:
    """Roll a file back to a retained version (file:rollback, FR-FILE-3).

    Rollback republishes the authoritative copy, so it requires the server at rest
    (Section 6.9: a hot replacement of a live working set is unsafe — not in the
    6.9 table, documented here). Running -> :class:`ServerNotStoppedError` (409).
    """

    uow: UnitOfWork
    file_store: FileStore

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: str,
        version_id: str,
    ) -> None:
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
        if not server.is_at_rest():
            raise ServerNotStoppedError(str(server_id.value))
        await self.file_store.rollback(
            community_id=community_id,
            server_id=server_id,
            rel_path=rel_path,
            version_id=version_id,
        )


def _join(dir_path: str, name: str) -> str:
    """Join an upload target dir with a file/entry name into a rel_path."""

    base = "" if dir_path in ("", ".") else dir_path.rstrip("/")
    return f"{base}/{name}" if base else name


def _archive_entries(filename: str, content: bytes) -> Iterator[tuple[str, bytes]]:
    """Yield ``(entry_path, bytes)`` for a zip / tar.gz upload, validated per entry.

    Per-entry defences (FR-FILE-4, the zip-slip class): every member path must be
    relative with no ``..`` component (a hostile entry like ``../../etc/passwd``
    is the zip-slip vector), and only regular files are accepted — a symlink,
    device, or other special member is refused outright rather than materialized.
    Directory members carry no bytes and are skipped (their files re-create them).
    Raises :class:`InvalidFilePathError` for an unsafe or special member.

    The cumulative extracted size is capped at :data:`MAX_UPLOAD_BYTES` so a
    decompression bomb cannot blow past the upload budget; raises
    :class:`FileTooLargeError` when the running total would exceed it.
    """

    if filename.endswith(".zip"):
        yield from _zip_entries(content)
    elif filename.endswith((".tar.gz", ".tgz")):
        yield from _tar_gz_entries(content)
    else:
        raise InvalidFilePathError(filename)


def _check_entry_path(name: str) -> None:
    parts = name.split("/")
    if name.startswith("/") or ".." in parts:
        raise InvalidFilePathError(name)


def _zip_entries(content: bytes) -> Iterator[tuple[str, bytes]]:
    total = 0
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            _check_entry_path(info.filename)
            # A zip can encode a symlink in the external-attrs unix mode; refuse
            # any member whose type bits are set to a non-regular type (a symlink,
            # device, fifo). An entry that carries only permission bits (no
            # type bits, the common case) is a plain file and passes.
            file_type = (info.external_attr >> 16) & 0o170000
            if file_type not in (0, 0o100000):
                raise InvalidFilePathError(info.filename)
            data = zf.read(info)
            total += len(data)
            if total > MAX_UPLOAD_BYTES:
                raise FileTooLargeError(str(total))
            yield info.filename, data


def _tar_gz_entries(content: bytes) -> Iterator[tuple[str, bytes]]:
    total = 0
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tf:
        for member in tf:
            if member.isdir():
                continue
            # Only regular files are materialized; a symlink/hardlink/device/fifo
            # member is refused (the tar analogue of the zip symlink check).
            if not member.isreg():
                raise InvalidFilePathError(member.name)
            _check_entry_path(member.name)
            total += member.size
            if total > MAX_UPLOAD_BYTES:
                raise FileTooLargeError(str(total))
            handle = tf.extractfile(member)
            data = b"" if handle is None else handle.read()
            yield member.name, data


@dataclass(frozen=True)
class UploadFile:
    """Upload a file (or extract an archive) to the authoritative copy (file:edit).

    At rest only: an upload republishes the authoritative copy, so a running
    server is :class:`ServerFilesUnsettledError` (the edge returns 409), reusing
    the unsettled posture other bulk at-rest ops take. The target directory and
    the filename are traversal-validated before any write; with ``extract``, each
    archive member is validated per entry (zip-slip defence) and the total
    extracted size is capped.

    Versioning: each written file captures a version exactly as
    :class:`WriteFile` does (one published version per file). An archive extract
    therefore captures N versions — one per member; the simpler sequential
    write_file composition is chosen over a snapshot-level commit because it
    reuses the proven write path with no new Storage plumbing.
    """

    uow: UnitOfWork
    file_store: FileStore

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        dir_path: str,
        filename: str,
        content: bytes,
        extract: bool,
    ) -> None:
        if len(content) > MAX_UPLOAD_BYTES:
            raise FileTooLargeError(str(len(content)))
        self.file_store.validate_rel_path(dir_path)
        _check_entry_path(filename)

        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
        if not server.is_at_rest():
            raise ServerFilesUnsettledError(str(server_id.value))

        if extract:
            for entry_path, data in _archive_entries(filename, content):
                await self.file_store.write_file(
                    community_id=community_id,
                    server_id=server_id,
                    rel_path=_join(dir_path, entry_path),
                    content=data,
                )
            return
        await self.file_store.write_file(
            community_id=community_id,
            server_id=server_id,
            rel_path=_join(dir_path, filename),
            content=content,
        )


@dataclass(frozen=True)
class DownloadFile:
    """Download a file (bytes) or a directory (zip stream) at rest (file:read).

    At rest only: the download reads the authoritative copy, so a running server
    is :class:`ServerFilesUnsettledError` (the edge returns 409). A file path
    yields its bytes; a directory path yields a zip byte stream of the subtree
    built incrementally over the Storage read stream (bounded memory).
    """

    uow: UnitOfWork
    file_store: FileStore

    async def file_bytes(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> bytes:
        await self._require_at_rest(community_id, server_id)
        return await self.file_store.read_file(
            community_id=community_id, server_id=server_id, rel_path=rel_path
        )

    async def dir_zip(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> AsyncIterator[bytes]:
        await self._require_at_rest(community_id, server_id)
        return self.file_store.download_dir(
            community_id=community_id, server_id=server_id, rel_path=rel_path
        )

    async def is_dir(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> bool:
        """Resolve whether ``rel_path`` names a directory (file vs. dir dispatch).

        Reads the parent listing rather than trusting the caller, so the edge can
        pick the file/zip branch. A path that resolves to neither raises
        :class:`ServerFileNotFoundError`.
        """

        await self._require_at_rest(community_id, server_id)
        return await self._resolve_is_dir(community_id, server_id, rel_path)

    async def _resolve_is_dir(
        self, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> bool:
        if rel_path in ("", "."):
            return True
        self.file_store.validate_rel_path(rel_path)
        try:
            await self.file_store.list_dir(
                community_id=community_id, server_id=server_id, rel_path=rel_path
            )
            return True
        except ServerFileNotFoundError:
            # Not a directory; confirm it is a readable file (else re-raise).
            await self.file_store.read_file(
                community_id=community_id, server_id=server_id, rel_path=rel_path
            )
            return False

    async def _require_at_rest(
        self, community_id: CommunityId, server_id: ServerId
    ) -> None:
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
        if not server.is_at_rest():
            raise ServerFilesUnsettledError(str(server_id.value))
