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
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from dataclasses import dataclass
from typing import IO, cast

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
    FileAlreadyExistsError,
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

# The decompressed-size cap for an uploaded/restored backup archive. The
# compressed body is bounded by MAX_UPLOAD_BYTES (512 MiB), but a gzip member can
# expand ~1000x, so the compressed cap alone does not bound how much a hostile
# archive inflates on extraction (a gzip bomb). 8 GiB bounds that amplification
# while comfortably covering a real Minecraft world (restores of large worlds are
# expected; the point is bounding the blow-up ratio, not blocking big worlds). The
# bound counts ACTUAL decompressed bytes as members are drained, so a member that
# under-reports its header size cannot slip past. A constant is intentional (no
# config knob requested); document if it ever needs tuning.
MAX_DECOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024

# The archive-entry-count cap. The cumulative-size cap alone does not bound a
# member-count bomb: a tiny archive of hundreds of thousands of 1-byte members
# stays under MAX_UPLOAD_BYTES yet each member is a separate versioned write
# (an atomic temp+fsync+rename plus a retained version), so an unbounded member
# count is a DoS amplification independent of total size. 10_000 entries
# comfortably covers a real mod/datapack/world archive while refusing a
# pathological count. Exceeding it is reported as :class:`FileTooLargeError`
# (413) — the same response the size cap uses, since both are "this archive is
# too costly to extract" rejections and a single response keeps the edge mapping
# simple. A constant is intentional (no config knob requested); document if it
# ever needs tuning.
MAX_ARCHIVE_ENTRIES = 10_000

# How much to pull per chunk when streaming an archive member's decompressed
# bytes. The cumulative cap is enforced *during* decompression (every chunk is
# counted before the next is pulled), so a single high-ratio member cannot be
# fully materialized in memory before the guard trips (the single-entry
# decompression-bomb defence).
_DECOMPRESS_CHUNK_BYTES = 1024 * 1024

# The default result cap for a file search. A search walks the whole authoritative
# subtree, so an unbounded match list could return a giant response; 500 matches
# comfortably covers an interactive "find this config" query while bounding the
# response. Exceeding it sets the ``truncated`` flag rather than erroring (a
# partial result is still useful). A constant is intentional (no config knob
# requested); document if it ever needs tuning.
MAX_SEARCH_RESULTS = 500

# The per-file size cap for a CONTENT search: a file larger than this is skipped
# (not grepped). Content search decodes a file as text and substring-scans it, so
# scanning a multi-GiB world region would be pointless (binary) and ruinous
# (whole file in memory). 1 MiB covers configs / scripts / datapack JSON — the
# text files a content search is for — while skipping bulk binary data. A constant
# is intentional (no config knob requested); document if it ever needs tuning.
MAX_SEARCH_FILE_BYTES = 1024 * 1024

# The aggregate scanned-files cap for a search. The per-file cap bounds a single
# read, but a content search over a working set of tens of thousands of files
# would still issue one read_file per under-cap file until the result cap (500) is
# reached — a cost unbounded by the small match count. 10_000 files comfortably
# covers a real working set's text config / datapack tree while bounding the total
# scan; exceeding it stops the walk and sets ``truncated`` (a partial result is
# still useful), the same partial-result posture the result cap takes. A constant
# is intentional (no config knob requested); document if it ever needs tuning.
MAX_SEARCH_SCANNED = 10_000


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


def _archive_entries(
    filename: str, content: bytes, *, max_bytes: int, max_entries: int
) -> Iterator[tuple[str, bytes]]:
    """Yield ``(entry_path, bytes)`` for a zip / tar.gz upload, validated per entry.

    Per-entry defences (FR-FILE-4, the zip-slip class): every member path must be
    relative with no ``..`` component (a hostile entry like ``../../etc/passwd``
    is the zip-slip vector), and only regular files are accepted — a symlink,
    device, or other special member is refused outright rather than materialized.
    Directory members carry no bytes and are skipped (their files re-create them).
    Raises :class:`InvalidFilePathError` for an unsafe or special member.

    The cumulative extracted size is capped at ``max_bytes`` and the member count
    at ``max_entries`` (both decompression-bomb guards); raises
    :class:`FileTooLargeError` when either running total would exceed its cap. The
    size cap is enforced *during* decompression on both paths (a member is read in
    chunks, counted as it inflates), so a single high-ratio member cannot be
    materialized in memory before the guard trips.

    This is the SAME generator both the validate pass and the write pass run over,
    so the per-entry checks are defined once (no duplication). The validate pass
    (:func:`_validate_archive`) drains it without writing; the write pass consumes
    it again and persists each member. The archive bytes are already in memory
    (bounded by ``MAX_UPLOAD_BYTES``), so a second pass is cheap.
    """

    if filename.endswith(".zip"):
        yield from _zip_entries(content, max_bytes=max_bytes, max_entries=max_entries)
    elif filename.endswith((".tar.gz", ".tgz")):
        yield from _tar_gz_entries(
            content, max_bytes=max_bytes, max_entries=max_entries
        )
    else:
        raise InvalidFilePathError(filename)


def _validate_archive(
    filename: str, content: bytes, *, max_bytes: int, max_entries: int
) -> None:
    """Dry-run the hardened extraction so a rejection happens before any write.

    Iterates the entire archive through the SAME :func:`_archive_entries` checks
    (traversal / symlink / special-member, cumulative size, entry count) and
    discards the yielded bytes — nothing is persisted. If the whole archive
    validates this returns; otherwise it raises the same
    :class:`InvalidFilePathError` / :class:`FileTooLargeError` the write pass
    would. Running this first makes extraction atomic: a mid-archive rejection
    (zip-slip / size cap / entry-count) leaves NOTHING written (issue #269), and
    on import it fires before the server row is committed (issue #277).
    """

    for _entry_path, _data in _archive_entries(
        filename, content, max_bytes=max_bytes, max_entries=max_entries
    ):
        pass


def _check_entry_path(name: str) -> None:
    parts = name.split("/")
    if name.startswith("/") or ".." in parts:
        raise InvalidFilePathError(name)


def _read_capped(reader: IO[bytes], total: int, max_bytes: int) -> bytes:
    """Drain a member stream in chunks, raising once the cumulative cap is crossed.

    ``total`` is the bytes already accounted for across prior members; the running
    sum is checked after every chunk, so a single member that inflates past the
    budget aborts mid-decompression rather than being fully materialized first
    (the single-entry decompression-bomb defence). The compressed member header
    can lie about its size, so the count is over *actual* decompressed bytes.
    """

    out = bytearray()
    while True:
        chunk = reader.read(_DECOMPRESS_CHUNK_BYTES)
        if not chunk:
            return bytes(out)
        total += len(chunk)
        if total > max_bytes:
            raise FileTooLargeError(str(total))
        out += chunk


def _zip_entries(
    content: bytes, *, max_bytes: int, max_entries: int
) -> Iterator[tuple[str, bytes]]:
    total = 0
    count = 0
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            count += 1
            if count > max_entries:
                raise FileTooLargeError(str(count))
            _check_entry_path(info.filename)
            # A zip can encode a symlink in the external-attrs unix mode; refuse
            # any member whose type bits are set to a non-regular type (a symlink,
            # device, fifo). An entry that carries only permission bits (no
            # type bits, the common case) is a plain file and passes.
            file_type = (info.external_attr >> 16) & 0o170000
            if file_type not in (0, 0o100000):
                raise InvalidFilePathError(info.filename)
            # Stream the member instead of zf.read(info): zf.read decompresses the
            # whole member into memory before any size check, so one high-ratio
            # entry could OOM the process before the cap trips. Counting actual
            # decompressed bytes as they arrive bounds a single-entry bomb too.
            with zf.open(info) as reader:
                data = _read_capped(reader, total, max_bytes)
            total += len(data)
            yield info.filename, data


def _tar_gz_entries(
    content: bytes, *, max_bytes: int, max_entries: int
) -> Iterator[tuple[str, bytes]]:
    total = 0
    count = 0
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tf:
        for member in tf:
            if member.isdir():
                continue
            # Only regular files are materialized; a symlink/hardlink/device/fifo
            # member is refused (the tar analogue of the zip symlink check).
            if not member.isreg():
                raise InvalidFilePathError(member.name)
            _check_entry_path(member.name)
            count += 1
            if count > max_entries:
                raise FileTooLargeError(str(count))
            handle = tf.extractfile(member)
            # member.size is a header field a hostile archive can under-report, so
            # count actual decompressed bytes during the read rather than trusting
            # it; the chunked drain enforces the cap mid-member.
            data = b"" if handle is None else _read_capped(handle, total, max_bytes)
            total += len(data)
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
    # The size / entry-count caps are fields (not bare constants) so a test can
    # inject tiny caps and trip the decompression-bomb guards with a small
    # archive, rather than building a multi-GiB fixture. Production wiring uses
    # the defaults.
    max_bytes: int = MAX_UPLOAD_BYTES
    max_entries: int = MAX_ARCHIVE_ENTRIES

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
        if len(content) > self.max_bytes:
            raise FileTooLargeError(str(len(content)))
        self.file_store.validate_rel_path(dir_path)
        _check_entry_path(filename)

        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
        if not server.is_at_rest():
            raise ServerFilesUnsettledError(str(server_id.value))

        if extract:
            # Validate the whole archive (traversal / symlink / size / entry-count)
            # in a dry-run pass BEFORE writing anything: a mid-archive rejection
            # then leaves no partially-written versioned files (issue #269). The
            # archive bytes are already in memory, so the second pass is cheap.
            _validate_archive(
                filename,
                content,
                max_bytes=self.max_bytes,
                max_entries=self.max_entries,
            )
            # Duplicate entry names (two members with the same path) are written
            # in archive order, so the last occurrence wins — the same last-write
            # semantics a sequence of plain writes would have. Left as-is (no
            # de-dup / reject) for M2; an archive with colliding names is
            # malformed and the resulting authoritative copy is well-defined.
            for entry_path, data in _archive_entries(
                filename,
                content,
                max_bytes=self.max_bytes,
                max_entries=self.max_entries,
            ):
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

    async def file_stream(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> AsyncIterator[bytes]:
        # A single-file download streams the file in bounded chunks (issue #265):
        # a file on disk may be arbitrarily large (e.g. a world region exceeding
        # the upload cap), so it must never be buffered whole in RAM. The seam's
        # per-file stream resolves + locates the file on first iteration and
        # surfaces ServerFileNotFoundError there.
        await self._require_at_rest(community_id, server_id)
        return self.file_store.open_file_stream(
            community_id=community_id, server_id=server_id, rel_path=rel_path
        )

    async def file_size(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> int | None:
        """Return the file's size for a Content-Length header, or ``None``.

        Reads the cheap parent listing (no file bytes) to find the entry's size;
        a path the listing cannot resolve cheaply (the working-set root has no
        parent, or a listing miss) yields ``None`` so the edge falls back to
        chunked transfer rather than failing. The actual bytes still flow through
        :meth:`file_stream`; this is only the optional length hint (issue #265).
        """

        await self._require_at_rest(community_id, server_id)
        parent, _, name = rel_path.rstrip("/").rpartition("/")
        try:
            entries = await self.file_store.list_dir(
                community_id=community_id,
                server_id=server_id,
                rel_path=parent or ".",
            )
        except ServerFileNotFoundError:
            return None
        for entry in entries:
            if entry.name == name and not entry.is_dir:
                return entry.size
        return None

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
            # Not a directory; confirm it is a readable file (else re-raise). Probe
            # the per-file stream rather than read_file so a huge file is not
            # buffered whole just to confirm existence (issue #265): the stream
            # resolves + locates the file on its first iteration, so consuming one
            # chunk (or hitting a clean EOF for an empty file) is enough; missing
            # surfaces ServerFileNotFoundError there.
            stream = cast(
                "AsyncGenerator[bytes, None]",
                self.file_store.open_file_stream(
                    community_id=community_id, server_id=server_id, rel_path=rel_path
                ),
            )
            try:
                await stream.__anext__()
            except StopAsyncIteration:
                pass
            finally:
                await stream.aclose()
            return False

    async def _require_at_rest(
        self, community_id: CommunityId, server_id: ServerId
    ) -> None:
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
        if not server.is_at_rest():
            raise ServerFilesUnsettledError(str(server_id.value))


async def _path_is_dir(
    file_store: FileStore,
    community_id: CommunityId,
    server_id: ServerId,
    rel_path: str,
) -> bool:
    """Resolve whether ``rel_path`` is a directory (vs a file), at rest.

    Mirrors :meth:`DownloadFile._resolve_is_dir`: the root is always a directory;
    otherwise a successful ``list_dir`` means a directory, and a
    :class:`ServerFileNotFoundError` from it falls back to a file read (re-raising
    if that is missing too). Validates the path first.
    """

    if rel_path in ("", "."):
        return True
    file_store.validate_rel_path(rel_path)
    try:
        await file_store.list_dir(
            community_id=community_id, server_id=server_id, rel_path=rel_path
        )
        return True
    except ServerFileNotFoundError:
        await file_store.read_file(
            community_id=community_id, server_id=server_id, rel_path=rel_path
        )
        return False


async def _path_exists(
    file_store: FileStore,
    community_id: CommunityId,
    server_id: ServerId,
    rel_path: str,
) -> bool:
    """True if ``rel_path`` names an existing file or directory at rest."""

    try:
        await _path_is_dir(file_store, community_id, server_id, rel_path)
        return True
    except ServerFileNotFoundError:
        return False


@dataclass(frozen=True)
class DeleteFile:
    """Delete a file or directory (recursive) at rest (file:edit, issue #259).

    At rest only: a delete mutates the authoritative copy, so a running server is
    :class:`ServerFilesUnsettledError` (the edge returns 409), the same unsettled
    posture the other bulk at-rest ops take. The path is resolved to a file or a
    directory and dispatched to the matching seam method; a missing path is
    :class:`ServerFileNotFoundError` (404). A file delete retains the prior
    content as a version (reversible); a directory delete does not (backups cover
    whole-subtree recovery).
    """

    uow: UnitOfWork
    file_store: FileStore

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> None:
        if rel_path in ("", "."):
            # Refuse to delete the working-set root itself: that is a wipe, not a
            # file op (use backups/restore for whole-working-set lifecycle).
            raise InvalidFilePathError(rel_path)
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
        if not server.is_at_rest():
            raise ServerFilesUnsettledError(str(server_id.value))

        is_dir = await _path_is_dir(self.file_store, community_id, server_id, rel_path)
        if is_dir:
            await self.file_store.delete_dir(
                community_id=community_id, server_id=server_id, rel_path=rel_path
            )
        else:
            await self.file_store.delete_file(
                community_id=community_id, server_id=server_id, rel_path=rel_path
            )


@dataclass(frozen=True)
class MakeDir:
    """Create an (empty) directory at rest (file:edit, issue #259).

    At rest only (running -> 409). Backend-dependent: fs materializes a real empty
    directory; object storage cannot represent an empty directory and the seam
    is a no-op there (the documented limitation). The path is traversal-validated.
    """

    uow: UnitOfWork
    file_store: FileStore

    async def __call__(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> None:
        self.file_store.validate_rel_path(rel_path)
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
        if not server.is_at_rest():
            raise ServerFilesUnsettledError(str(server_id.value))
        await self.file_store.make_dir(
            community_id=community_id, server_id=server_id, rel_path=rel_path
        )


@dataclass(frozen=True)
class RenameFile:
    """Rename/move a file at rest (file:edit, issue #259).

    At rest only (running -> 409). Composed over the existing seam — read the
    source, write the destination, delete the source — so versioning comes for
    free (the destination write and the source delete each capture a version) with
    no new Storage rename primitive. Both paths are traversal-validated; a missing
    source is :class:`ServerFileNotFoundError` (404) and an existing destination is
    :class:`FileAlreadyExistsError` (409): rename never clobbers, so a typo cannot
    silently overwrite data. Renaming a directory is out of scope for this slice
    (the read/write seam is file-granular); ``from`` must name a file.

    The composition (write destination, then delete source) is not atomic: a crash
    in the window between the two leaves BOTH the source and the destination
    present. This favours never losing data (the source survives) over strict
    move-once semantics; a caller seeing both can safely retry or delete the stale
    source.
    """

    uow: UnitOfWork
    file_store: FileStore

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        from_path: str,
        to_path: str,
    ) -> None:
        self.file_store.validate_rel_path(from_path)
        self.file_store.validate_rel_path(to_path)
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
        if not server.is_at_rest():
            raise ServerFilesUnsettledError(str(server_id.value))

        if from_path == to_path:
            # A no-op rename onto itself: confirm the source exists (404 if not),
            # then return without rewriting (no spurious version).
            await self.file_store.read_file(
                community_id=community_id, server_id=server_id, rel_path=from_path
            )
            return

        # Read the source first (404 if missing). A directory source raises here
        # (read_file of a dir is NotFound), so a directory rename is refused as a
        # missing file rather than partially moved.
        content = await self.file_store.read_file(
            community_id=community_id, server_id=server_id, rel_path=from_path
        )
        if await _path_exists(self.file_store, community_id, server_id, to_path):
            raise FileAlreadyExistsError(to_path)

        await self.file_store.write_file(
            community_id=community_id,
            server_id=server_id,
            rel_path=to_path,
            content=content,
        )
        await self.file_store.delete_file(
            community_id=community_id, server_id=server_id, rel_path=from_path
        )


@dataclass(frozen=True)
class SearchResult:
    """A bounded list of matching paths plus whether the result was truncated."""

    paths: list[str]
    truncated: bool


@dataclass(frozen=True)
class SearchFiles:
    """Search the authoritative copy by name or content at rest (file:read, #259).

    At rest only (running -> 409): search reads the authoritative Storage copy.
    ``by="name"`` matches a case-insensitive substring of each entry's *basename*
    (substring, not glob — the simpler, no-surprise choice; documented). ``by=
    "content"`` scans each file's raw bytes for the UTF-8-encoded query as a plain
    byte substring (case-sensitive), skipping any file whose *listed* size exceeds
    :data:`MAX_SEARCH_FILE_BYTES` — gated before any read, so a bulk binary /
    multi-GiB region file is never pulled into memory (a content search is for text
    configs). Results are bounded to ``max_results`` (capped at
    :data:`MAX_SEARCH_RESULTS`) and the whole walk is bounded to
    :data:`MAX_SEARCH_SCANNED` files; hitting either bound sets ``truncated``
    rather than erroring (a partial result is still useful).
    """

    uow: UnitOfWork
    file_store: FileStore
    max_file_bytes: int = MAX_SEARCH_FILE_BYTES
    max_scanned: int = MAX_SEARCH_SCANNED

    async def __call__(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        query: str,
        by: str,
        max_results: int,
    ) -> SearchResult:
        if by not in ("name", "content"):
            raise InvalidFilePathError(by)
        await self._require_at_rest(community_id, server_id)
        limit = min(max_results, MAX_SEARCH_RESULTS)
        name_needle = query.lower()
        content_needle = query.encode("utf-8")

        paths: list[str] = []
        truncated = False
        scanned = 0
        async for rel_path, name, size in self._walk(community_id, server_id):
            if scanned >= self.max_scanned:
                # The aggregate scan budget is spent: stop the walk and report a
                # partial result rather than reading the whole tree.
                truncated = True
                break
            scanned += 1
            if by == "name":
                matched = name_needle in name.lower()
            else:
                matched = await self._content_matches(
                    community_id, server_id, rel_path, size, content_needle
                )
            if not matched:
                continue
            if len(paths) >= limit:
                truncated = True
                break
            paths.append(rel_path)
        return SearchResult(paths=paths, truncated=truncated)

    async def _walk(
        self, community_id: CommunityId, server_id: ServerId
    ) -> AsyncIterator[tuple[str, str, int]]:
        """Yield ``(rel_path, basename, size)`` for every file, depth-first."""

        stack = ["."]
        while stack:
            current = stack.pop()
            entries = await self.file_store.list_dir(
                community_id=community_id, server_id=server_id, rel_path=current
            )
            base = "" if current == "." else current
            for entry in entries:
                child = f"{base}/{entry.name}" if base else entry.name
                if entry.is_dir:
                    stack.append(child)
                else:
                    yield child, entry.name, entry.size

    async def _content_matches(
        self,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: str,
        size: int,
        needle: bytes,
    ) -> bool:
        # Gate on the already-listed entry size BEFORE reading: skip a bulk binary /
        # huge file without ever pulling it into memory (the per-file cap's point).
        if size > self.max_file_bytes:
            return False
        data = await self.file_store.read_file(
            community_id=community_id, server_id=server_id, rel_path=rel_path
        )
        return needle in data

    async def _require_at_rest(
        self, community_id: CommunityId, server_id: ServerId
    ) -> None:
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)
        if not server.is_at_rest():
            raise ServerFilesUnsettledError(str(server_id.value))
