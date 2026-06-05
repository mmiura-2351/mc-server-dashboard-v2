"""Use-case tests for backup management with state branching (Section 6.9, 6.11).

Exercises :mod:`servers.application.backups` against fakes (no DB, no real
Storage), per TESTING.md Section 4. Verifies:

- the at-rest create path (archive directly from Storage, no save-all/snapshot);
- the running create path orchestration (save-all RCON -> on-demand snapshot ->
  archive), with the snapshot hook faked;
- a transitional server -> BackupUnsettledError on create;
- nothing-to-archive -> BackupNotFoundError;
- list is community-scoped and newest-first;
- restore requires the server at rest (409 running), round-trips a known ref, and
  404s an unknown / cross-server backup;
- delete ordering: the archive is removed before the metadata row, and an unknown
  backup 404s.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

import pytest

from mc_server_dashboard_api.servers.application.backups import (
    CreateBackup,
    DeleteBackup,
    DownloadBackup,
    GlobalBackupStatistics,
    ListBackups,
    RestoreBackup,
    ServerBackupStatistics,
    UploadBackup,
)
from mc_server_dashboard_api.servers.application.snapshot_scheduler import (
    SnapshotServer,
)
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    BackupNotFoundError,
    BackupUnsettledError,
    CommandDispatchError,
    FileTooLargeError,
    InvalidBackupArchiveError,
    ServerNotFoundError,
    ServerNotStoppedError,
)
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
from tests.servers.fakes import (
    FakeBackupArchiveStore,
    FakeBackupRepository,
    FakeClock,
    FakeControlPlane,
    FakeServerRepository,
    FakeUnitOfWork,
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_COMMUNITY = CommunityId(uuid.uuid4())
_WORKER = WorkerId(uuid.uuid4())


def _server(
    *,
    desired: DesiredState,
    observed: ObservedState,
    worker: WorkerId | None,
    server_id: ServerId | None = None,
    community_id: CommunityId = _COMMUNITY,
) -> Server:
    return Server(
        id=server_id or ServerId.new(),
        community_id=community_id,
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        execution_backend=ExecutionBackend.HOST_PROCESS,
        config={},
        desired_state=desired,
        observed_state=observed,
        observed_at=None,
        assigned_worker_id=worker,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _at_rest(*, server_id: ServerId | None = None) -> Server:
    return _server(
        desired=DesiredState.STOPPED,
        observed=ObservedState.STOPPED,
        worker=None,
        server_id=server_id,
    )


def _running(*, server_id: ServerId | None = None) -> Server:
    return _server(
        desired=DesiredState.RUNNING,
        observed=ObservedState.RUNNING,
        worker=_WORKER,
        server_id=server_id,
    )


def _make_create(
    uow: FakeUnitOfWork,
    control_plane: FakeControlPlane,
    archive: FakeBackupArchiveStore,
) -> CreateBackup:
    return CreateBackup(
        uow=uow,
        control_plane=control_plane,
        backup_store=archive,
        snapshot_server=SnapshotServer(uow=uow, control_plane=control_plane),
        clock=FakeClock(_NOW),
    )


async def test_create_at_rest_archives_without_save_all_or_snapshot() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    uow = FakeUnitOfWork(servers=repo)
    control_plane = FakeControlPlane()
    archive = FakeBackupArchiveStore()
    create = _make_create(uow, control_plane, archive)

    backup = await create(
        community_id=_COMMUNITY,
        server_id=server.id,
        source=BackupSource.MANUAL,
        created_by=uuid.uuid4(),
    )

    assert backup.source is BackupSource.MANUAL
    assert archive.created == [server.id]
    # No RCON save-all and no snapshot trigger on the at-rest path.
    assert [kind for kind, *_ in control_plane.dispatched] == []
    # The metadata row was persisted.
    assert await uow.backups.get_by_id(backup.id) is not None


async def test_create_running_save_all_then_snapshot_then_archive() -> None:
    server = _running()
    repo = FakeServerRepository()
    repo.seed(server)
    uow = FakeUnitOfWork(servers=repo)
    control_plane = FakeControlPlane()
    archive = FakeBackupArchiveStore()
    create = _make_create(uow, control_plane, archive)

    backup = await create(
        community_id=_COMMUNITY,
        server_id=server.id,
        source=BackupSource.MANUAL,
        created_by=uuid.uuid4(),
    )

    kinds = [kind for kind, *_ in control_plane.dispatched]
    # save-all (an RCON command) is dispatched, then the snapshot, then archive.
    assert kinds == ["command", "snapshot"]
    assert archive.created == [server.id]
    assert backup.storage_ref in archive.archives


async def test_create_running_save_all_failure_fails_create() -> None:
    server = _running()
    repo = FakeServerRepository()
    repo.seed(server)
    uow = FakeUnitOfWork(servers=repo)
    control_plane = FakeControlPlane(
        outcomes={"command": CommandOutcome(status=CommandStatus.INVALID_STATE)}
    )
    archive = FakeBackupArchiveStore()
    create = _make_create(uow, control_plane, archive)

    with pytest.raises(CommandDispatchError):
        await create(
            community_id=_COMMUNITY,
            server_id=server.id,
            source=BackupSource.MANUAL,
        )
    # No snapshot and no archive when save-all failed.
    assert [kind for kind, *_ in control_plane.dispatched] == ["command"]
    assert archive.created == []


async def test_create_running_save_all_failure_logs_warning_with_server_and_kind(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A failed save-all dispatch turns into a CommandDispatchError; the Worker's
    # message is logged at WARN with server_id and command kind context so the
    # failure is diagnosable, while the raw message stays out of the HTTP body
    # (issue #200).
    server = _running()
    repo = FakeServerRepository()
    repo.seed(server)
    uow = FakeUnitOfWork(servers=repo)
    control_plane = FakeControlPlane(
        outcomes={
            "command": CommandOutcome(
                status=CommandStatus.INVALID_STATE, message="rcon refused"
            )
        }
    )
    archive = FakeBackupArchiveStore()
    create = _make_create(uow, control_plane, archive)

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(CommandDispatchError),
    ):
        await create(
            community_id=_COMMUNITY,
            server_id=server.id,
            source=BackupSource.MANUAL,
        )

    record = next(r for r in caplog.records if r.levelno == logging.WARNING)
    message = record.getMessage()
    assert "rcon refused" in message
    assert "SaveAll" in message
    assert str(server.id.value) in message


async def test_create_transitional_server_is_unsettled() -> None:
    server = _server(
        desired=DesiredState.RUNNING, observed=ObservedState.STARTING, worker=_WORKER
    )
    repo = FakeServerRepository()
    repo.seed(server)
    uow = FakeUnitOfWork(servers=repo)
    create = _make_create(uow, FakeControlPlane(), FakeBackupArchiveStore())

    with pytest.raises(BackupUnsettledError):
        await create(
            community_id=_COMMUNITY,
            server_id=server.id,
            source=BackupSource.MANUAL,
        )


async def test_create_with_nothing_published_raises_not_found() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    uow = FakeUnitOfWork(servers=repo)
    archive = FakeBackupArchiveStore(missing=True)
    create = _make_create(uow, FakeControlPlane(), archive)

    with pytest.raises(BackupNotFoundError):
        await create(
            community_id=_COMMUNITY,
            server_id=server.id,
            source=BackupSource.MANUAL,
        )


async def test_create_unknown_server_is_not_found() -> None:
    uow = FakeUnitOfWork()
    create = _make_create(uow, FakeControlPlane(), FakeBackupArchiveStore())
    with pytest.raises(ServerNotFoundError):
        await create(
            community_id=_COMMUNITY,
            server_id=ServerId.new(),
            source=BackupSource.MANUAL,
        )


async def test_list_is_community_scoped_and_newest_first() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    older = Backup(
        id=BackupId.new(),
        server_id=server.id,
        storage_ref="a",
        size_bytes=None,
        source=BackupSource.MANUAL,
        created_by=None,
        created_at=_NOW,
    )
    newer = Backup(
        id=BackupId.new(),
        server_id=server.id,
        storage_ref="b",
        size_bytes=None,
        source=BackupSource.SCHEDULED,
        created_by=None,
        created_at=_NOW + dt.timedelta(hours=1),
    )
    backups.seed(older)
    backups.seed(newer)
    uow = FakeUnitOfWork(servers=repo, backups=backups)
    listed = await ListBackups(uow=uow)(community_id=_COMMUNITY, server_id=server.id)
    assert [b.id for b in listed] == [newer.id, older.id]


async def test_list_unknown_server_is_not_found() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(ServerNotFoundError):
        await ListBackups(uow=uow)(community_id=_COMMUNITY, server_id=ServerId.new())


async def test_restore_requires_stopped_server() -> None:
    server = _running()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    backup = Backup(
        id=BackupId.new(),
        server_id=server.id,
        storage_ref="ref",
        size_bytes=None,
        source=BackupSource.MANUAL,
        created_by=None,
        created_at=_NOW,
    )
    backups.seed(backup)
    archive = FakeBackupArchiveStore()
    archive.archives.add("ref")
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    with pytest.raises(ServerNotStoppedError):
        await RestoreBackup(uow=uow, backup_store=archive)(
            community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
        )
    assert archive.restored == []


async def test_restore_at_rest_republishes_known_ref() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    backup = Backup(
        id=BackupId.new(),
        server_id=server.id,
        storage_ref="ref",
        size_bytes=None,
        source=BackupSource.MANUAL,
        created_by=None,
        created_at=_NOW,
    )
    backups.seed(backup)
    archive = FakeBackupArchiveStore()
    archive.archives.add("ref")
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    await RestoreBackup(uow=uow, backup_store=archive)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )
    assert archive.restored == [(server.id, "ref")]


async def test_restore_unknown_backup_is_not_found() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    uow = FakeUnitOfWork(servers=repo)
    with pytest.raises(BackupNotFoundError):
        await RestoreBackup(uow=uow, backup_store=FakeBackupArchiveStore())(
            community_id=_COMMUNITY, server_id=server.id, backup_id=BackupId.new()
        )


async def test_restore_backup_of_other_server_is_not_found() -> None:
    server = _at_rest()
    other = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    repo.seed(other)
    backups = FakeBackupRepository()
    backup = Backup(
        id=BackupId.new(),
        server_id=other.id,
        storage_ref="ref",
        size_bytes=None,
        source=BackupSource.MANUAL,
        created_by=None,
        created_at=_NOW,
    )
    backups.seed(backup)
    uow = FakeUnitOfWork(servers=repo, backups=backups)
    with pytest.raises(BackupNotFoundError):
        await RestoreBackup(uow=uow, backup_store=FakeBackupArchiveStore())(
            community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
        )


async def test_delete_removes_archive_before_metadata_row() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    backup = Backup(
        id=BackupId.new(),
        server_id=server.id,
        storage_ref="ref",
        size_bytes=None,
        source=BackupSource.MANUAL,
        created_by=None,
        created_at=_NOW,
    )
    backups.seed(backup)
    archive = FakeBackupArchiveStore()
    archive.archives.add("ref")
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    await DeleteBackup(uow=uow, backup_store=archive)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )
    # Both the archive and the metadata row are gone.
    assert archive.deleted == [(server.id, "ref")]
    assert "ref" not in archive.archives
    assert await uow.backups.get_by_id(backup.id) is None


async def test_delete_unknown_backup_is_not_found() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    uow = FakeUnitOfWork(servers=repo)
    with pytest.raises(BackupNotFoundError):
        await DeleteBackup(uow=uow, backup_store=FakeBackupArchiveStore())(
            community_id=_COMMUNITY, server_id=server.id, backup_id=BackupId.new()
        )


# --- download / upload / statistics (issue #281) ---------------------------


def _targz(files: dict[str, bytes]) -> bytes:
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _hostile_targz() -> bytes:
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"pwned"
        info = tarfile.TarInfo(name="../../etc/escape")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _seed_backup(
    backups: FakeBackupRepository,
    archive: FakeBackupArchiveStore,
    server_id: ServerId,
    *,
    storage_ref: str,
    size_bytes: int | None,
    created_at: dt.datetime = _NOW,
) -> Backup:
    backup = Backup(
        id=BackupId.new(),
        server_id=server_id,
        storage_ref=storage_ref,
        size_bytes=size_bytes,
        source=BackupSource.MANUAL,
        created_by=None,
        created_at=created_at,
    )
    backups.seed(backup)
    archive.archives.add(storage_ref)
    archive.bytes_by_ref[storage_ref] = b"x" * (size_bytes or 0)
    return backup


async def test_download_streams_archive_for_known_backup() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    archive = FakeBackupArchiveStore()
    backup = _seed_backup(backups, archive, server.id, storage_ref="ref", size_bytes=5)
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    stream = await DownloadBackup(uow=uow, backup_store=archive)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )
    blob = b"".join([chunk async for chunk in stream])
    assert blob == b"x" * 5


async def test_download_unknown_backup_is_not_found() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    uow = FakeUnitOfWork(servers=repo)
    with pytest.raises(BackupNotFoundError):
        await DownloadBackup(uow=uow, backup_store=FakeBackupArchiveStore())(
            community_id=_COMMUNITY, server_id=server.id, backup_id=BackupId.new()
        )


async def test_download_cross_server_backup_is_not_found() -> None:
    server = _at_rest()
    other = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    repo.seed(other)
    backups = FakeBackupRepository()
    archive = FakeBackupArchiveStore()
    foreign = _seed_backup(backups, archive, other.id, storage_ref="ref", size_bytes=3)
    uow = FakeUnitOfWork(servers=repo, backups=backups)
    with pytest.raises(BackupNotFoundError):
        await DownloadBackup(uow=uow, backup_store=archive)(
            community_id=_COMMUNITY, server_id=server.id, backup_id=foreign.id
        )


async def test_upload_validates_stores_and_records_uploaded_row() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    archive = FakeBackupArchiveStore()
    uow = FakeUnitOfWork(servers=repo, backups=backups)
    content = _targz({"server.properties": b"k=v", "world/level.dat": b"w"})
    actor = uuid.uuid4()

    backup = await UploadBackup(uow=uow, backup_store=archive, clock=FakeClock(_NOW))(
        community_id=_COMMUNITY,
        server_id=server.id,
        content=content,
        created_by=actor,
    )

    assert backup.source is BackupSource.UPLOADED
    assert backup.created_by == actor
    assert backup.size_bytes == len(content)
    assert archive.stored == [server.id]
    # The stored bytes are the uploaded archive verbatim (no recompression).
    assert archive.bytes_by_ref[backup.storage_ref] == content
    assert await uow.backups.get_by_id(backup.id) is not None


async def test_upload_rejects_non_archive_before_storing() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    archive = FakeBackupArchiveStore()
    uow = FakeUnitOfWork(servers=repo)
    with pytest.raises(InvalidBackupArchiveError):
        await UploadBackup(uow=uow, backup_store=archive, clock=FakeClock(_NOW))(
            community_id=_COMMUNITY,
            server_id=server.id,
            content=b"not a tar.gz",
            created_by=uuid.uuid4(),
        )
    assert archive.stored == []


async def test_upload_rejects_traversal_member_before_storing() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    archive = FakeBackupArchiveStore()
    uow = FakeUnitOfWork(servers=repo)
    with pytest.raises(InvalidBackupArchiveError):
        await UploadBackup(uow=uow, backup_store=archive, clock=FakeClock(_NOW))(
            community_id=_COMMUNITY,
            server_id=server.id,
            content=_hostile_targz(),
            created_by=uuid.uuid4(),
        )
    assert archive.stored == []


async def test_upload_over_cap_is_rejected_before_storing() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    archive = FakeBackupArchiveStore()
    uow = FakeUnitOfWork(servers=repo)
    content = _targz({"big": b"x" * 4096})
    with pytest.raises(FileTooLargeError):
        await UploadBackup(
            uow=uow, backup_store=archive, clock=FakeClock(_NOW), max_bytes=16
        )(
            community_id=_COMMUNITY,
            server_id=server.id,
            content=content,
            created_by=uuid.uuid4(),
        )
    assert archive.stored == []


async def test_server_statistics_aggregates_count_bytes_and_bounds() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    archive = FakeBackupArchiveStore()
    older = _NOW - dt.timedelta(days=1)
    _seed_backup(
        backups, archive, server.id, storage_ref="a", size_bytes=10, created_at=older
    )
    _seed_backup(
        backups, archive, server.id, storage_ref="b", size_bytes=20, created_at=_NOW
    )
    # A legacy NULL-size row: excluded from total_bytes, counted as unknown.
    _seed_backup(
        backups, archive, server.id, storage_ref="c", size_bytes=None, created_at=_NOW
    )
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    stats = await ServerBackupStatistics(uow=uow)(
        community_id=_COMMUNITY, server_id=server.id
    )
    assert stats.count == 3
    assert stats.total_bytes == 30
    assert stats.unknown_size_count == 1
    assert stats.newest == _NOW
    assert stats.oldest == older


async def test_server_statistics_empty_is_zero() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    uow = FakeUnitOfWork(servers=repo)
    stats = await ServerBackupStatistics(uow=uow)(
        community_id=_COMMUNITY, server_id=server.id
    )
    assert stats.count == 0
    assert stats.total_bytes == 0
    assert stats.unknown_size_count == 0
    assert stats.newest is None and stats.oldest is None


async def test_server_statistics_unknown_server_is_not_found() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(ServerNotFoundError):
        await ServerBackupStatistics(uow=uow)(
            community_id=_COMMUNITY, server_id=ServerId.new()
        )


async def test_global_statistics_aggregates_across_servers() -> None:
    backups = FakeBackupRepository()
    archive = FakeBackupArchiveStore()
    s1, s2 = ServerId.new(), ServerId.new()
    _seed_backup(backups, archive, s1, storage_ref="a", size_bytes=10)
    _seed_backup(backups, archive, s2, storage_ref="b", size_bytes=5)
    uow = FakeUnitOfWork(backups=backups)
    stats = await GlobalBackupStatistics(uow=uow)()
    assert stats.count == 2
    assert stats.total_bytes == 15
    assert stats.unknown_size_count == 0
