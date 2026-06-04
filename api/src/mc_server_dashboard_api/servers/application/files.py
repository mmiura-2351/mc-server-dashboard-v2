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

from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandStatus,
    ControlPlane,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    CommandDispatchError,
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


def _map_file_status(server_id: ServerId, status: CommandStatus, message: str) -> None:
    """Translate a Worker file-command failure to a servers file error."""

    if status is CommandStatus.SERVER_NOT_FOUND:
        # The Worker reports a missing target file (or no such running server);
        # either way the path the caller asked for is not there.
        raise ServerFileNotFoundError(str(server_id.value))
    if status is CommandStatus.FILE_ACCESS_DENIED:
        raise InvalidFilePathError(message or str(server_id.value))
    raise CommandDispatchError(message or status.value)


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
                _map_file_status(server_id, outcome.status, outcome.message)
            return outcome.file_content
        raise ServerFilesUnsettledError(str(server_id.value))


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
    ) -> list[FileEntry]:
        async with self.uow:
            server = await _load(self.uow, community_id, server_id)

        if server.is_at_rest():
            return await self.file_store.list_dir(
                community_id=community_id, server_id=server_id, rel_path=rel_path
            )
        if _is_running(server):
            self.file_store.validate_rel_path(rel_path)
            outcome = await self.control_plane.list_files(
                worker_id=server.assigned_worker_id,  # type: ignore[arg-type]
                server_id=server_id,
                rel_path=rel_path,
            )
            if not outcome.success:
                _map_file_status(server_id, outcome.status, outcome.message)
            listing = outcome.listing
            entries = () if listing is None else listing.entries
            return [
                FileEntry(name=e.name, is_dir=e.is_dir, size=e.size) for e in entries
            ]
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
                _map_file_status(server_id, outcome.status, outcome.message)
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
