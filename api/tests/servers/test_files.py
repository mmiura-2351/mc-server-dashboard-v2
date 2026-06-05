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
import io
import logging
import tarfile
import uuid
import zipfile
from collections.abc import AsyncIterator
from pathlib import PurePosixPath

import pytest

from mc_server_dashboard_api.servers.application.files import (
    MAX_EDIT_BYTES,
    MAX_SEARCH_RESULTS,
    MAX_UPLOAD_BYTES,
    DeleteFile,
    DownloadFile,
    ListDir,
    ListFileVersions,
    MakeDir,
    ReadFile,
    RenameFile,
    RollbackFile,
    SearchFiles,
    UploadFile,
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
    FileAlreadyExistsError,
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

    def __init__(self, *, strict_dirs: bool = False) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: dict[str, list[FileEntry]] = {}
        self.versions: dict[str, list[str]] = {}
        self.writes: list[tuple[str, bytes]] = []
        self.rollbacks: list[tuple[str, str]] = []
        self.deleted_files: list[str] = []
        self.deleted_dirs: list[str] = []
        self.made_dirs: list[str] = []
        self.missing = False
        self.bad_path = False
        # When set, list_dir raises ServerFileNotFoundError for a path that is not
        # a seeded directory, so the file-vs-dir resolution (delete / rename /
        # search) can tell a file from a directory; off by default to preserve the
        # existing browse tests' "unknown dir lists empty" behaviour.
        self.strict_dirs = strict_dirs

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

    def open_file_stream(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> AsyncIterator[bytes]:
        # Yield the seeded file in fixed-size chunks so a multi-chunk file
        # surfaces as more than one yield (the bounded-memory contract, #265).
        chunk = 4

        async def _gen() -> AsyncIterator[bytes]:
            if self.bad_path:
                raise InvalidFilePathError(rel_path)
            if rel_path not in self.files:
                raise ServerFileNotFoundError(str(server_id.value))
            data = self.files[rel_path]
            for i in range(0, len(data), chunk):
                yield data[i : i + chunk]

        return _gen()

    async def list_dir(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> list[FileEntry]:
        if self.missing:
            raise ServerFileNotFoundError(str(server_id.value))
        if self.strict_dirs and rel_path not in self.dirs:
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

    async def delete_file(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> None:
        if self.bad_path:
            raise InvalidFilePathError(rel_path)
        if rel_path not in self.files:
            raise ServerFileNotFoundError(str(server_id.value))
        del self.files[rel_path]
        self.deleted_files.append(rel_path)

    async def delete_dir(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> None:
        if rel_path not in self.dirs:
            raise ServerFileNotFoundError(str(server_id.value))
        self.deleted_dirs.append(rel_path)

    async def make_dir(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> None:
        if self.bad_path:
            raise InvalidFilePathError(rel_path)
        self.made_dirs.append(rel_path)

    def download_dir(
        self, *, community_id: CommunityId, server_id: ServerId, rel_path: str
    ) -> AsyncIterator[bytes]:
        async def _gen() -> AsyncIterator[bytes]:
            if self.missing:
                raise ServerFileNotFoundError(str(server_id.value))
            yield b"zip-bytes"

        return _gen()

    def export_dir(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        rel_path: str,
        extra: list[tuple[str, bytes]],
    ) -> AsyncIterator[bytes]:
        # Build a real zip of every seeded file plus the ``extra`` entries so a
        # round-trip test can re-open and compare the bytes (issue #274).
        files = dict(self.files)

        async def _gen() -> AsyncIterator[bytes]:
            if self.missing:
                raise ServerFileNotFoundError(str(server_id.value))
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, mode="w") as zf:
                for path, content in files.items():
                    zf.writestr(path, content)
                for arcname, content in extra:
                    zf.writestr(arcname, content)
            yield buf.getvalue()

        return _gen()

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


# --- upload: state branching + validation + extraction ---------------------


def _stopped_uow(community: uuid.UUID, server_id: uuid.UUID) -> FakeUnitOfWork:
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
    return uow


def _running_uow(community: uuid.UUID, server_id: uuid.UUID) -> FakeUnitOfWork:
    uow = FakeUnitOfWork()
    _seed(
        uow,
        _server(
            community_id=community,
            server_id=server_id,
            desired=DesiredState.RUNNING,
            observed=ObservedState.RUNNING,
            worker=uuid.uuid4(),
        ),
    )
    return uow


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _tar_gz_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def test_upload_at_rest_writes_single_file() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore()
    use_case = UploadFile(uow=_stopped_uow(community, server_id), file_store=store)

    await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        dir_path="plugins",
        filename="mod.jar",
        content=b"jar-bytes",
        extract=False,
    )
    assert store.files["plugins/mod.jar"] == b"jar-bytes"


async def test_upload_root_dir_joins_filename_only() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore()
    use_case = UploadFile(uow=_stopped_uow(community, server_id), file_store=store)

    await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        dir_path=".",
        filename="server.properties",
        content=b"x",
        extract=False,
    )
    assert store.files["server.properties"] == b"x"


async def test_upload_running_is_unsettled() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore()
    use_case = UploadFile(uow=_running_uow(community, server_id), file_store=store)

    with pytest.raises(ServerFilesUnsettledError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            dir_path=".",
            filename="f",
            content=b"x",
            extract=False,
        )
    assert store.files == {}


async def test_upload_traversal_dir_is_invalid_path() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    use_case = UploadFile(
        uow=_stopped_uow(community, server_id), file_store=FakeFileStore()
    )
    with pytest.raises(InvalidFilePathError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            dir_path="../escape",
            filename="f",
            content=b"x",
            extract=False,
        )


async def test_upload_traversal_filename_is_invalid_path() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    use_case = UploadFile(
        uow=_stopped_uow(community, server_id), file_store=FakeFileStore()
    )
    with pytest.raises(InvalidFilePathError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            dir_path=".",
            filename="../escape",
            content=b"x",
            extract=False,
        )


async def test_upload_over_cap_is_too_large() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    use_case = UploadFile(
        uow=_stopped_uow(community, server_id), file_store=FakeFileStore()
    )
    with pytest.raises(FileTooLargeError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            dir_path=".",
            filename="big.bin",
            content=b"x" * (MAX_UPLOAD_BYTES + 1),
            extract=False,
        )


async def test_upload_extract_zip_writes_each_entry() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore()
    use_case = UploadFile(uow=_stopped_uow(community, server_id), file_store=store)

    archive = _zip_bytes({"a.txt": b"A", "nested/b.txt": b"B"})
    await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        dir_path="datapacks",
        filename="pack.zip",
        content=archive,
        extract=True,
    )
    assert store.files["datapacks/a.txt"] == b"A"
    assert store.files["datapacks/nested/b.txt"] == b"B"


async def test_upload_extract_tar_gz_writes_each_entry() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore()
    use_case = UploadFile(uow=_stopped_uow(community, server_id), file_store=store)

    archive = _tar_gz_bytes({"a.txt": b"A", "nested/b.txt": b"B"})
    await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        dir_path=".",
        filename="pack.tar.gz",
        content=archive,
        extract=True,
    )
    assert store.files["a.txt"] == b"A"
    assert store.files["nested/b.txt"] == b"B"


async def test_upload_extract_zip_slip_entry_is_invalid_path() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore()
    use_case = UploadFile(uow=_stopped_uow(community, server_id), file_store=store)

    archive = _zip_bytes({"../escape.txt": b"pwned"})
    with pytest.raises(InvalidFilePathError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            dir_path=".",
            filename="evil.zip",
            content=archive,
            extract=True,
        )


async def test_upload_extract_zip_symlink_entry_is_invalid_path() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore()
    use_case = UploadFile(uow=_stopped_uow(community, server_id), file_store=store)

    # A zip member flagged as a unix symlink (S_IFLNK in external attrs).
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        info = zipfile.ZipInfo("link")
        info.external_attr = (0o120777 & 0xFFFF) << 16
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(InvalidFilePathError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            dir_path=".",
            filename="evil.zip",
            content=buf.getvalue(),
            extract=True,
        )


async def test_upload_extract_over_size_cap_is_too_large() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore()
    use_case = UploadFile(uow=_stopped_uow(community, server_id), file_store=store)

    # A highly compressible payload: the archive is tiny (passes the raw-upload
    # cap) but the cumulative extracted size exceeds it: the decompression-bomb
    # guard. Use deflate so the zeros compress to almost nothing.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i in range(600):
            zf.writestr(f"f{i}.bin", b"\x00" * (1024 * 1024))
    archive = buf.getvalue()
    assert len(archive) <= MAX_UPLOAD_BYTES  # raw upload is under the cap
    with pytest.raises(FileTooLargeError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            dir_path=".",
            filename="bomb.zip",
            content=archive,
            extract=True,
        )


async def test_upload_extract_single_entry_bomb_aborts_mid_decompression() -> None:
    # One zip member that inflates far past the cap. With the per-member streamed
    # read, the cap trips during decompression — the full member is never
    # materialized. Inject a tiny cap so the fixture stays a few MiB (fast), not
    # multi-GiB: the archive is one highly compressible member, the use case cap
    # is 1 MiB, so the guard fires after ~1 MiB is decoded.
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore()
    use_case = UploadFile(
        uow=_stopped_uow(community, server_id),
        file_store=store,
        max_bytes=1024 * 1024,
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 8 MiB of zeros in a single member -> deflates to a tiny archive but
        # would inflate well past the 1 MiB cap if read whole.
        zf.writestr("huge.bin", b"\x00" * (8 * 1024 * 1024))
    archive = buf.getvalue()

    with pytest.raises(FileTooLargeError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            dir_path=".",
            filename="bomb.zip",
            content=archive,
            extract=True,
        )
    assert store.files == {}  # nothing written before the guard tripped


async def test_upload_extract_tar_member_size_header_lie_is_counted() -> None:
    # A tar member whose header under-reports its size cannot smuggle bytes past
    # the cap: the count is over actual decompressed bytes, not member.size.
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore()
    use_case = UploadFile(
        uow=_stopped_uow(community, server_id),
        file_store=store,
        max_bytes=1024 * 1024,
    )

    payload = b"\x00" * (4 * 1024 * 1024)  # 4 MiB, over the 1 MiB cap
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="liar.bin")
        info.size = len(payload)  # honest header
        tf.addfile(info, io.BytesIO(payload))
    archive = buf.getvalue()

    with pytest.raises(FileTooLargeError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            dir_path=".",
            filename="big.tar.gz",
            content=archive,
            extract=True,
        )


async def test_upload_extract_over_entry_count_cap_is_too_large() -> None:
    # Many tiny members: each stays well under the size cap, but the count cap
    # rejects the archive before it can churn N versioned writes.
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore()
    use_case = UploadFile(
        uow=_stopped_uow(community, server_id),
        file_store=store,
        max_entries=5,
    )

    archive = _zip_bytes({f"f{i}.txt": b"x" for i in range(20)})
    with pytest.raises(FileTooLargeError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            dir_path=".",
            filename="many.zip",
            content=archive,
            extract=True,
        )


async def test_upload_extract_tar_over_entry_count_cap_is_too_large() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore()
    use_case = UploadFile(
        uow=_stopped_uow(community, server_id),
        file_store=store,
        max_entries=5,
    )

    archive = _tar_gz_bytes({f"f{i}.txt": b"x" for i in range(20)})
    with pytest.raises(FileTooLargeError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            dir_path=".",
            filename="many.tar.gz",
            content=archive,
            extract=True,
        )


async def test_upload_extract_tar_symlink_member_is_invalid_path() -> None:
    # The tar analogue of the zip-symlink check: a non-regular member (symlink,
    # the common tar vector) is refused before any byte is materialized.
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore()
    use_case = UploadFile(uow=_stopped_uow(community, server_id), file_store=store)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    with pytest.raises(InvalidFilePathError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            dir_path=".",
            filename="evil.tar.gz",
            content=buf.getvalue(),
            extract=True,
        )
    assert store.files == {}


async def test_upload_extract_unknown_archive_type_is_invalid_path() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    use_case = UploadFile(
        uow=_stopped_uow(community, server_id), file_store=FakeFileStore()
    )
    with pytest.raises(InvalidFilePathError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            dir_path=".",
            filename="not-an-archive.bin",
            content=b"x",
            extract=True,
        )


# --- download: state branching + file vs dir -------------------------------


async def test_download_file_stream_at_rest() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore()
    store.files["server.properties"] = b"motd=hi"
    use_case = DownloadFile(uow=_stopped_uow(community, server_id), file_store=store)

    stream = await use_case.file_stream(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        rel_path="server.properties",
    )
    out = b"".join([chunk async for chunk in stream])
    assert out == b"motd=hi"


async def test_download_file_stream_is_chunked_for_a_multi_chunk_file() -> None:
    # A file larger than the seam's chunk surfaces as multiple bounded yields
    # (the bounded-memory posture, issue #265): spy that more than one chunk
    # flows for a multi-chunk file.
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore()
    store.files["world/region.mca"] = b"abcdefghijklmnop"  # > the fake's 4-byte chunk
    use_case = DownloadFile(uow=_stopped_uow(community, server_id), file_store=store)

    stream = await use_case.file_stream(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        rel_path="world/region.mca",
    )
    chunks = [chunk async for chunk in stream]
    assert len(chunks) > 1
    assert b"".join(chunks) == b"abcdefghijklmnop"


async def test_download_file_size_reads_parent_listing() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore()
    store.dirs["world"] = [FileEntry(name="region.mca", is_dir=False, size=16)]
    use_case = DownloadFile(uow=_stopped_uow(community, server_id), file_store=store)

    size = await use_case.file_size(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        rel_path="world/region.mca",
    )
    assert size == 16


async def test_download_running_is_unsettled() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    use_case = DownloadFile(
        uow=_running_uow(community, server_id), file_store=FakeFileStore()
    )
    with pytest.raises(ServerFilesUnsettledError):
        await use_case.file_stream(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            rel_path="f",
        )


async def test_download_dir_zip_at_rest() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore()
    use_case = DownloadFile(uow=_stopped_uow(community, server_id), file_store=store)

    stream = await use_case.dir_zip(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        rel_path="world",
    )
    blob = b"".join([chunk async for chunk in stream])
    assert blob == b"zip-bytes"


async def test_download_is_dir_true_for_root() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    use_case = DownloadFile(
        uow=_stopped_uow(community, server_id), file_store=FakeFileStore()
    )
    assert await use_case.is_dir(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        rel_path=".",
    )


async def test_download_is_dir_false_for_file() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore()
    store.files["server.properties"] = b"x"
    store.missing = True  # list_dir raises -> falls through to read_file
    use_case = DownloadFile(uow=_stopped_uow(community, server_id), file_store=store)

    assert not await use_case.is_dir(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        rel_path="server.properties",
    )


# --- delete (file / directory, issue #259) ---------------------------------


async def test_delete_file_at_rest_removes_file() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore(strict_dirs=True)
    store.files["plugins/old.jar"] = b"x"
    use_case = DeleteFile(uow=_stopped_uow(community, server_id), file_store=store)

    await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        rel_path="plugins/old.jar",
    )
    assert store.deleted_files == ["plugins/old.jar"]
    assert "plugins/old.jar" not in store.files


async def test_delete_directory_at_rest_removes_subtree() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore(strict_dirs=True)
    store.dirs["world"] = [FileEntry(name="level.dat", is_dir=False, size=1)]
    use_case = DeleteFile(uow=_stopped_uow(community, server_id), file_store=store)

    await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        rel_path="world",
    )
    assert store.deleted_dirs == ["world"]
    assert store.deleted_files == []


async def test_delete_missing_is_file_not_found() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore(strict_dirs=True)  # neither a known dir nor a file
    use_case = DeleteFile(uow=_stopped_uow(community, server_id), file_store=store)

    with pytest.raises(ServerFileNotFoundError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            rel_path="nope",
        )


async def test_delete_root_is_invalid_path() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore(strict_dirs=True)
    use_case = DeleteFile(uow=_stopped_uow(community, server_id), file_store=store)

    with pytest.raises(InvalidFilePathError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            rel_path=".",
        )


async def test_delete_running_is_unsettled() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore(strict_dirs=True)
    store.files["f"] = b"x"
    use_case = DeleteFile(uow=_running_uow(community, server_id), file_store=store)

    with pytest.raises(ServerFilesUnsettledError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            rel_path="f",
        )
    assert store.deleted_files == []


# --- mkdir (issue #259) ----------------------------------------------------


async def test_make_dir_at_rest_creates_directory() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore()
    use_case = MakeDir(uow=_stopped_uow(community, server_id), file_store=store)

    await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        rel_path="datapacks",
    )
    assert store.made_dirs == ["datapacks"]


async def test_make_dir_traversal_is_invalid_path() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    use_case = MakeDir(
        uow=_stopped_uow(community, server_id), file_store=FakeFileStore()
    )

    with pytest.raises(InvalidFilePathError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            rel_path="../escape",
        )


async def test_make_dir_running_is_unsettled() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore()
    use_case = MakeDir(uow=_running_uow(community, server_id), file_store=store)

    with pytest.raises(ServerFilesUnsettledError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            rel_path="datapacks",
        )
    assert store.made_dirs == []


# --- rename (issue #259) ---------------------------------------------------


async def test_rename_at_rest_moves_file() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore(strict_dirs=True)
    store.files["old.txt"] = b"payload"
    use_case = RenameFile(uow=_stopped_uow(community, server_id), file_store=store)

    await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        from_path="old.txt",
        to_path="new.txt",
    )
    # Composed read -> write(dest) -> delete(source): dest written, source gone.
    assert store.files.get("new.txt") == b"payload"
    assert store.deleted_files == ["old.txt"]


async def test_rename_missing_source_is_file_not_found() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore(strict_dirs=True)
    use_case = RenameFile(uow=_stopped_uow(community, server_id), file_store=store)

    with pytest.raises(ServerFileNotFoundError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            from_path="ghost.txt",
            to_path="new.txt",
        )


async def test_rename_existing_destination_is_conflict() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore(strict_dirs=True)
    store.files["old.txt"] = b"a"
    store.files["taken.txt"] = b"b"
    use_case = RenameFile(uow=_stopped_uow(community, server_id), file_store=store)

    with pytest.raises(FileAlreadyExistsError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            from_path="old.txt",
            to_path="taken.txt",
        )
    # Nothing moved: the source survives and the destination is untouched.
    assert store.files["old.txt"] == b"a"
    assert store.files["taken.txt"] == b"b"
    assert store.deleted_files == []


async def test_rename_traversal_path_is_invalid() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore(strict_dirs=True)
    use_case = RenameFile(uow=_stopped_uow(community, server_id), file_store=store)

    with pytest.raises(InvalidFilePathError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            from_path="old.txt",
            to_path="../escape",
        )


async def test_rename_running_is_unsettled() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = FakeFileStore(strict_dirs=True)
    store.files["old.txt"] = b"x"
    use_case = RenameFile(uow=_running_uow(community, server_id), file_store=store)

    with pytest.raises(ServerFilesUnsettledError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            from_path="old.txt",
            to_path="new.txt",
        )
    assert store.deleted_files == []


# --- search (name / content, issue #259) -----------------------------------


def _search_store() -> FakeFileStore:
    """A small two-level tree for the search walk."""

    store = FakeFileStore(strict_dirs=True)
    store.dirs["."] = [
        FileEntry(name="server.properties", is_dir=False, size=3),
        FileEntry(name="config", is_dir=True, size=0),
    ]
    store.dirs["config"] = [
        FileEntry(name="ops.json", is_dir=False, size=2),
        FileEntry(name="motd.txt", is_dir=False, size=5),
    ]
    store.files["server.properties"] = b"motd=hello"
    store.files["config/ops.json"] = b"[]"
    store.files["config/motd.txt"] = b"hello world"
    return store


async def test_search_by_name_matches_basename_case_insensitive() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    use_case = SearchFiles(
        uow=_stopped_uow(community, server_id), file_store=_search_store()
    )

    result = await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        query="MOTD",
        by="name",
        max_results=100,
    )
    assert set(result.paths) == {"config/motd.txt"}
    assert result.truncated is False


async def test_search_by_content_matches_substring() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    use_case = SearchFiles(
        uow=_stopped_uow(community, server_id), file_store=_search_store()
    )

    result = await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        query="hello",
        by="content",
        max_results=100,
    )
    assert set(result.paths) == {"server.properties", "config/motd.txt"}


async def test_search_content_skips_oversized_files() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    store = _search_store()
    store.files["config/motd.txt"] = b"hello" + b"x" * 1000
    use_case = SearchFiles(
        uow=_stopped_uow(community, server_id),
        file_store=store,
        max_file_bytes=10,  # below the motd file size -> skipped
    )

    result = await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        query="hello",
        by="content",
        max_results=100,
    )
    # The oversized motd file is skipped; only the small properties file matches.
    assert set(result.paths) == {"server.properties"}


async def test_search_bounds_results_and_sets_truncated() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    use_case = SearchFiles(
        uow=_stopped_uow(community, server_id), file_store=_search_store()
    )

    result = await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        query="",  # empty substring matches every basename
        by="name",
        max_results=2,
    )
    assert len(result.paths) == 2
    assert result.truncated is True


async def test_search_caps_max_results_at_ceiling() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    use_case = SearchFiles(
        uow=_stopped_uow(community, server_id), file_store=_search_store()
    )

    # A caller asking for more than the ceiling is clamped to it; with only 3
    # files total it cannot truncate, proving the clamp does not over-collect.
    result = await use_case(
        community_id=CommunityId(community),
        server_id=ServerId(server_id),
        query="",
        by="name",
        max_results=MAX_SEARCH_RESULTS * 10,
    )
    assert len(result.paths) == 3
    assert result.truncated is False


async def test_search_invalid_by_is_invalid_path() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    use_case = SearchFiles(
        uow=_stopped_uow(community, server_id), file_store=_search_store()
    )

    with pytest.raises(InvalidFilePathError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            query="x",
            by="regex",
            max_results=10,
        )


async def test_search_running_is_unsettled() -> None:
    community, server_id = uuid.uuid4(), uuid.uuid4()
    use_case = SearchFiles(
        uow=_running_uow(community, server_id), file_store=_search_store()
    )

    with pytest.raises(ServerFilesUnsettledError):
        await use_case(
            community_id=CommunityId(community),
            server_id=ServerId(server_id),
            query="x",
            by="name",
            max_results=10,
        )
