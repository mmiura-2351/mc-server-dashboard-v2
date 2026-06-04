"""Use-case tests for file management with state branching (Section 6.9, 6.10).

Exercises :mod:`servers.application.files` against fakes (no DB, no real Storage),
per TESTING.md Section 4. Verifies the 6.9 state matrix:

- at rest (desired=stopped, observed in {stopped, unknown}) -> Storage seam;
- running (desired=running, observed=running, worker assigned) -> control plane;
- disconnected worker on the running path -> WorkerUnavailableError;
- a transitional/mismatched state -> ServerFilesUnsettledError;

plus the edit-size cap, rollback's at-rest-only rule, and the Worker file-status
-> servers-error translation.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from pathlib import PurePosixPath

import pytest

from mc_server_dashboard_api.servers.application.files import (
    MAX_EDIT_BYTES,
    ListDir,
    ListFileVersions,
    ReadFile,
    RollbackFile,
    WriteFile,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    FileEntry as OutcomeFileEntry,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    FileListing as OutcomeFileListing,
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
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ExecutionBackend,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)
from tests.servers.fakes import FakeControlPlane, FakeUnitOfWork

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


class FakeFileStore(FileStore):
    """In-memory authoritative-copy file store keyed by rel_path."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: dict[str, list[FileEntry]] = {}
        self.versions: dict[str, list[str]] = {}
        self.writes: list[tuple[str, bytes]] = []
        self.rollbacks: list[tuple[str, str]] = []
        self.missing = False
        self.bad_path = False

    def validate_rel_path(self, rel_path: str) -> None:
        # Mirror the seam's string-level traversal rule (absolute / ".."
        # rejection) so the running branch pre-rejects without a real adapter.
        parts = PurePosixPath(rel_path)
        if parts.is_absolute() or ".." in parts.parts:
            raise InvalidFilePathError(rel_path)

    async def read_file(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> bytes:
        if self.bad_path:
            raise InvalidFilePathError(rel_path)
        if rel_path not in self.files:
            raise ServerFileNotFoundError(str(server_id.value))
        return self.files[rel_path]

    async def list_dir(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> list[FileEntry]:
        if self.missing:
            raise ServerFileNotFoundError(str(server_id.value))
        return self.dirs.get(rel_path, [])

    async def write_file(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: str,
        content: bytes,
    ) -> None:
        if self.bad_path:
            raise InvalidFilePathError(rel_path)
        self.files[rel_path] = content
        self.writes.append((rel_path, content))

    async def list_versions(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> list[str]:
        return self.versions.get(rel_path, [])

    async def rollback(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: str,
        version_id: str,
    ) -> None:
        self.rollbacks.append((rel_path, version_id))


def _server(
    *,
    community_id: uuid.UUID,
    server_id: uuid.UUID,
    desired: DesiredState,
    observed: ObservedState,
    worker: uuid.UUID | None = None,
) -> Server:
    return Server(
        id=ServerId(server_id),
        community_id=CommunityId(community_id),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        execution_backend=ExecutionBackend.HOST_PROCESS,
        config={},
        desired_state=desired,
        observed_state=observed,
        observed_at=_NOW,
        assigned_worker_id=None if worker is None else WorkerId(worker),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _seed(uow: FakeUnitOfWork, server: Server) -> None:
    uow.servers.seed(server)


# --- read: state branching -------------------------------------------------


async def test_read_at_rest_reads_storage() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.STOPPED,
            observed=ObservedState.STOPPED,
        ),
    )
    store = FakeFileStore()
    store.files["server.properties"] = b"motd=hi"
    cp = FakeControlPlane()
    use_case = ReadFile(uow=uow, control_plane=cp, file_store=store)

    out = await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        rel_path="server.properties",
    )
    assert out == b"motd=hi"
    assert cp.dispatched == []  # never touched the worker


async def test_read_running_reads_control_plane() -> None:
    community, server_id, worker = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker=worker,
        ),
    )
    store = FakeFileStore()
    cp = FakeControlPlane(
        outcome=CommandOutcome(status=CommandStatus.OK, file_content=b"live-bytes")
    )
    use_case = ReadFile(uow=uow, control_plane=cp, file_store=store)

    out = await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        rel_path="server.properties",
    )
    assert out == b"live-bytes"
    assert [d[0] for d in cp.dispatched] == ["read_file"]


async def test_read_running_disconnected_worker_raises_unavailable() -> None:
    community, server_id, worker = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker=worker,
        ),
    )
    cp = FakeControlPlane(raise_unavailable=True)
    use_case = ReadFile(uow=uow, control_plane=cp, file_store=FakeFileStore())

    with pytest.raises(WorkerUnavailableError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            rel_path="server.properties",
        )


async def test_read_transitional_state_is_unsettled() -> None:
    community, server_id, worker = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.STARTING,
            worker=worker,
        ),
    )
    use_case = ReadFile(
        uow=uow, control_plane=FakeControlPlane(), file_store=FakeFileStore()
    )
    with pytest.raises(ServerFilesUnsettledError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            rel_path="f",
        )


async def test_read_unknown_observed_is_at_rest() -> None:
    # desired=stopped + observed=unknown (worker gone) still counts at rest.
    community, server_id = uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.STOPPED,
            observed=ObservedState.UNKNOWN,
        ),
    )
    store = FakeFileStore()
    store.files["f"] = b"x"
    use_case = ReadFile(uow=uow, control_plane=FakeControlPlane(), file_store=store)
    assert (
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            rel_path="f",
        )
        == b"x"
    )


async def test_crashed_observed_is_at_rest_for_read_write_and_list() -> None:
    # desired=stopped + observed=crashed (the EULA-crash case, issue #197): a
    # crashed process has no live working set, so file ops branch to Storage
    # instead of 409 server_unsettled. Covers read, write, and list in one place.
    community, server_id = uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.STOPPED,
            observed=ObservedState.CRASHED,
        ),
    )
    store = FakeFileStore()
    store.files["eula.txt"] = b"eula=false"
    store.dirs[""] = [FileEntry(name="eula.txt", is_dir=False, size=10)]
    cp = FakeControlPlane()

    read_out = await ReadFile(uow=uow, control_plane=cp, file_store=store)(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        rel_path="eula.txt",
    )
    assert read_out == b"eula=false"

    await WriteFile(uow=uow, control_plane=cp, file_store=store)(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        rel_path="eula.txt",
        content=b"eula=true",
    )
    assert store.files["eula.txt"] == b"eula=true"

    listing = await ListDir(uow=uow, control_plane=cp, file_store=store)(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        rel_path="",
    )
    assert [e.name for e in listing.entries] == ["eula.txt"]
    assert cp.dispatched == []  # never touched the worker


async def test_read_missing_server_is_not_found() -> None:
    uow = FakeUnitOfWork()
    use_case = ReadFile(
        uow=uow, control_plane=FakeControlPlane(), file_store=FakeFileStore()
    )
    with pytest.raises(ServerNotFoundError):
        await use_case(
            community_id=CommunityId(uuid.uuid4()),
            server_id=ServerId(uuid.uuid4()),
            rel_path="f",
        )


async def test_read_running_file_access_denied_maps_to_invalid_path() -> None:
    community, server_id, worker = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker=worker,
        ),
    )
    cp = FakeControlPlane(
        outcome=CommandOutcome(status=CommandStatus.FILE_ACCESS_DENIED, message="nope")
    )
    use_case = ReadFile(uow=uow, control_plane=cp, file_store=FakeFileStore())
    with pytest.raises(InvalidFilePathError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            rel_path="../escape",
        )


async def test_read_running_traversal_rejected_before_dispatch() -> None:
    community, server_id, worker = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker=worker,
        ),
    )
    cp = FakeControlPlane()
    use_case = ReadFile(uow=uow, control_plane=cp, file_store=FakeFileStore())
    with pytest.raises(InvalidFilePathError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            rel_path="../escape",
        )
    assert cp.dispatched == []  # rejected at the edge; never reached the worker


async def test_read_running_server_not_found_maps_to_file_not_found() -> None:
    community, server_id, worker = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker=worker,
        ),
    )
    cp = FakeControlPlane(outcome=CommandOutcome(status=CommandStatus.SERVER_NOT_FOUND))
    use_case = ReadFile(uow=uow, control_plane=cp, file_store=FakeFileStore())
    with pytest.raises(ServerFileNotFoundError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            rel_path="gone",
        )


async def test_read_running_internal_failure_maps_to_dispatch_error() -> None:
    community, server_id, worker = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker=worker,
        ),
    )
    cp = FakeControlPlane(outcome=CommandOutcome(status=CommandStatus.INTERNAL))
    use_case = ReadFile(uow=uow, control_plane=cp, file_store=FakeFileStore())
    with pytest.raises(CommandDispatchError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            rel_path="f",
        )


async def test_read_running_failure_logs_warning_with_server_and_kind(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # An unmapped Worker file-command failure turns into a CommandDispatchError;
    # the Worker's message is logged at WARN with server_id and command kind
    # context so the failure is diagnosable, while the raw message stays out of
    # the HTTP body (issue #200).
    community, server_id, worker = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker=worker,
        ),
    )
    cp = FakeControlPlane(
        outcome=CommandOutcome(status=CommandStatus.INTERNAL, message="disk error")
    )
    use_case = ReadFile(uow=uow, control_plane=cp, file_store=FakeFileStore())

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(CommandDispatchError),
    ):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            rel_path="f",
        )

    record = next(r for r in caplog.records if r.levelno == logging.WARNING)
    message = record.getMessage()
    assert "disk error" in message
    assert "ReadFile" in message
    assert str(server_id) in message


# --- write: state branching + cap ------------------------------------------


async def test_write_at_rest_writes_storage() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.STOPPED,
            observed=ObservedState.STOPPED,
        ),
    )
    store = FakeFileStore()
    cp = FakeControlPlane()
    use_case = WriteFile(uow=uow, control_plane=cp, file_store=store)

    await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        rel_path="ops.json",
        content=b"[]",
    )
    assert store.writes == [("ops.json", b"[]")]
    assert cp.dispatched == []


async def test_write_running_edits_control_plane() -> None:
    community, server_id, worker = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker=worker,
        ),
    )
    store = FakeFileStore()
    cp = FakeControlPlane()
    use_case = WriteFile(uow=uow, control_plane=cp, file_store=store)

    await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        rel_path="ops.json",
        content=b"[]",
    )
    assert [d[0] for d in cp.dispatched] == ["edit_file"]
    assert store.writes == []


async def test_write_over_cap_is_rejected_before_dispatch() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.STOPPED,
            observed=ObservedState.STOPPED,
        ),
    )
    store = FakeFileStore()
    use_case = WriteFile(uow=uow, control_plane=FakeControlPlane(), file_store=store)
    with pytest.raises(FileTooLargeError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            rel_path="big",
            content=b"x" * (MAX_EDIT_BYTES + 1),
        )
    assert store.writes == []  # never reached the store


async def test_write_running_traversal_rejected_before_dispatch() -> None:
    community, server_id, worker = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker=worker,
        ),
    )
    cp = FakeControlPlane()
    use_case = WriteFile(uow=uow, control_plane=cp, file_store=FakeFileStore())
    with pytest.raises(InvalidFilePathError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            rel_path="../escape",
            content=b"x",
        )
    assert cp.dispatched == []  # rejected at the edge; never reached the worker


async def test_write_transitional_state_is_unsettled() -> None:
    community, server_id, worker = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.STOPPING,
            worker=worker,
        ),
    )
    use_case = WriteFile(
        uow=uow, control_plane=FakeControlPlane(), file_store=FakeFileStore()
    )
    with pytest.raises(ServerFilesUnsettledError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            rel_path="f",
            content=b"x",
        )


# --- list / history / rollback ---------------------------------------------


async def test_list_dir_at_rest_reads_storage() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.STOPPED,
            observed=ObservedState.STOPPED,
        ),
    )
    store = FakeFileStore()
    store.dirs["."] = [FileEntry(name="world", is_dir=True, size=0)]
    cp = FakeControlPlane()
    use_case = ListDir(uow=uow, control_plane=cp, file_store=store)
    listing = await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        rel_path=".",
    )
    assert [e.name for e in listing.entries] == ["world"]
    assert listing.truncated is False  # the at-rest path is never truncated
    assert cp.dispatched == []  # never touched the worker


async def test_list_dir_running_reads_control_plane() -> None:
    community, server_id, worker = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker=worker,
        ),
    )
    store = FakeFileStore()
    # Storage is stale (empty); the live listing must come from the worker.
    cp = FakeControlPlane(
        outcome=CommandOutcome(
            status=CommandStatus.OK,
            listing=OutcomeFileListing(
                entries=(
                    OutcomeFileEntry(name="server.properties", is_dir=False, size=42),
                    OutcomeFileEntry(name="world", is_dir=True, size=0),
                ),
                truncated=False,
            ),
        )
    )
    use_case = ListDir(uow=uow, control_plane=cp, file_store=store)

    listing = await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        rel_path=".",
    )
    assert [(e.name, e.is_dir, e.size) for e in listing.entries] == [
        ("server.properties", False, 42),
        ("world", True, 0),
    ]
    assert listing.truncated is False
    assert [d[0] for d in cp.dispatched] == ["list_files"]


async def test_list_dir_running_passes_truncated_flag_through() -> None:
    community, server_id, worker = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker=worker,
        ),
    )
    cp = FakeControlPlane(
        outcome=CommandOutcome(
            status=CommandStatus.OK,
            listing=OutcomeFileListing(
                entries=(OutcomeFileEntry(name="world", is_dir=True, size=0),),
                truncated=True,
            ),
        )
    )
    use_case = ListDir(uow=uow, control_plane=cp, file_store=FakeFileStore())

    listing = await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        rel_path=".",
    )
    assert [e.name for e in listing.entries] == ["world"]
    assert listing.truncated is True


async def test_list_dir_running_disconnected_worker_raises_unavailable() -> None:
    community, server_id, worker = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker=worker,
        ),
    )
    cp = FakeControlPlane(raise_unavailable=True)
    use_case = ListDir(uow=uow, control_plane=cp, file_store=FakeFileStore())

    with pytest.raises(WorkerUnavailableError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            rel_path=".",
        )


async def test_list_dir_transitional_state_is_unsettled() -> None:
    community, server_id, worker = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.STARTING,
            worker=worker,
        ),
    )
    use_case = ListDir(
        uow=uow, control_plane=FakeControlPlane(), file_store=FakeFileStore()
    )
    with pytest.raises(ServerFilesUnsettledError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            rel_path=".",
        )


async def test_list_dir_running_traversal_rejected_before_dispatch() -> None:
    community, server_id, worker = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker=worker,
        ),
    )
    cp = FakeControlPlane()
    use_case = ListDir(uow=uow, control_plane=cp, file_store=FakeFileStore())

    with pytest.raises(InvalidFilePathError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            rel_path="../escape",
        )
    assert cp.dispatched == []  # rejected before any dispatch


async def test_list_dir_running_file_access_denied_maps_to_invalid_path() -> None:
    community, server_id, worker = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker=worker,
        ),
    )
    cp = FakeControlPlane(
        outcome=CommandOutcome(status=CommandStatus.FILE_ACCESS_DENIED, message="nope")
    )
    use_case = ListDir(uow=uow, control_plane=cp, file_store=FakeFileStore())

    with pytest.raises(InvalidFilePathError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            rel_path="plugins",
        )


async def test_history_lists_versions() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.STOPPED,
            observed=ObservedState.STOPPED,
        ),
    )
    store = FakeFileStore()
    store.versions["f"] = ["v2", "v1"]
    use_case = ListFileVersions(uow=uow, file_store=store)
    assert await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        rel_path="f",
    ) == ["v2", "v1"]


async def test_rollback_at_rest_calls_store() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.STOPPED,
            observed=ObservedState.STOPPED,
        ),
    )
    store = FakeFileStore()
    use_case = RollbackFile(uow=uow, file_store=store)
    await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        rel_path="f",
        version_id="v1",
    )
    assert store.rollbacks == [("f", "v1")]


async def test_rollback_running_is_not_stopped() -> None:
    community, server_id, worker = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker=worker,
        ),
    )
    store = FakeFileStore()
    use_case = RollbackFile(uow=uow, file_store=store)
    with pytest.raises(ServerNotStoppedError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            rel_path="f",
            version_id="v1",
        )
    assert store.rollbacks == []
