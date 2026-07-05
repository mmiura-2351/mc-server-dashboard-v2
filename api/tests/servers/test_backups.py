"""Use-case tests for backup management with state branching (Section 6.9, 6.11).

Exercises :mod:`servers.application.backups` against fakes (no DB, no real
Storage), per TESTING.md Section 4. Verifies:

- the at-rest create path (archive directly from Storage, no save-all/snapshot);
- the running create path orchestration (on-demand snapshot -> archive), with the
  snapshot hook faked;
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
import io
import logging
import tarfile
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
    _validate_backup_archive,
)
from mc_server_dashboard_api.servers.application.snapshot_scheduler import (
    SnapshotServer,
)
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.backup_author_directory import (
    BackupAuthorDirectory,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    BackupCorruptError,
    BackupNotFoundError,
    BackupUnsettledError,
    FileTooLargeError,
    InvalidBackupArchiveError,
    ServerNotFoundError,
    ServerNotStoppedError,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
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


class _FakeAuthorDirectory(BackupAuthorDirectory):
    def __init__(self, usernames: dict[uuid.UUID, str] | None = None) -> None:
        self._usernames = usernames or {}
        self.calls: list[list[uuid.UUID]] = []

    async def usernames_for(self, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        self.calls.append(list(user_ids))
        return {uid: self._usernames[uid] for uid in user_ids if uid in self._usernames}


def _make_create(
    uow: FakeUnitOfWork,
    control_plane: FakeControlPlane,
    archive: FakeBackupArchiveStore,
) -> CreateBackup:
    return CreateBackup(
        uow=uow,
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
    # Healthy by construction: the gated create path archived a sound working set.
    assert backup.health is BackupHealth.HEALTHY
    assert archive.created == [server.id]
    # No RCON save-all and no snapshot trigger on the at-rest path.
    assert [kind for kind, *_ in control_plane.dispatched] == []
    # The metadata row was persisted with its health recorded.
    persisted = await uow.backups.get_by_id(backup.id)
    assert persisted is not None
    assert persisted.health is BackupHealth.HEALTHY


async def test_create_running_snapshot_then_archive() -> None:
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
    # The snapshot (whose worker path quiesces safely) is dispatched, then archive.
    assert kinds == ["snapshot"]
    assert archive.created == [server.id]
    assert backup.storage_ref in archive.archives


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
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=_NOW,
    )
    newer = Backup(
        id=BackupId.new(),
        server_id=server.id,
        storage_ref="b",
        size_bytes=None,
        source=BackupSource.SCHEDULED,
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=_NOW + dt.timedelta(hours=1),
    )
    backups.seed(older)
    backups.seed(newer)
    uow = FakeUnitOfWork(servers=repo, backups=backups)
    listed = await ListBackups(uow=uow, backup_store=FakeBackupArchiveStore())(
        community_id=_COMMUNITY, server_id=server.id
    )
    assert [b.backup.id for b in listed] == [newer.id, older.id]


def _seed_authored(
    backups: FakeBackupRepository,
    server_id: ServerId,
    *,
    created_by: uuid.UUID | None,
) -> Backup:
    backup = Backup(
        id=BackupId.new(),
        server_id=server_id,
        storage_ref="ref",
        size_bytes=1,
        source=BackupSource.MANUAL,
        health=BackupHealth.HEALTHY,
        created_by=created_by,
        created_at=_NOW,
    )
    backups.seed(backup)
    return backup


async def test_list_resolves_author_username() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    author = uuid.uuid4()
    _seed_authored(backups, server.id, created_by=author)
    uow = FakeUnitOfWork(servers=repo, backups=backups)
    users = _FakeAuthorDirectory({author: "alice"})

    listed = await ListBackups(
        uow=uow, backup_store=FakeBackupArchiveStore(), users=users
    )(community_id=_COMMUNITY, server_id=server.id)

    assert listed[0].created_by_username == "alice"


async def test_list_falls_back_to_none_when_author_deleted() -> None:
    # The author id no longer resolves (the user was deleted): the listing still
    # succeeds and the username is None so the client shows the raw id.
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    _seed_authored(backups, server.id, created_by=uuid.uuid4())
    uow = FakeUnitOfWork(servers=repo, backups=backups)
    users = _FakeAuthorDirectory({})  # resolves nothing

    listed = await ListBackups(
        uow=uow, backup_store=FakeBackupArchiveStore(), users=users
    )(community_id=_COMMUNITY, server_id=server.id)

    assert listed[0].created_by_username is None


async def test_list_null_author_is_not_resolved() -> None:
    # A scheduled backup has no actor (created_by is None): no username, and the
    # null id is never sent to the directory.
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    _seed_authored(backups, server.id, created_by=None)
    uow = FakeUnitOfWork(servers=repo, backups=backups)
    users = _FakeAuthorDirectory({})

    listed = await ListBackups(
        uow=uow, backup_store=FakeBackupArchiveStore(), users=users
    )(community_id=_COMMUNITY, server_id=server.id)

    assert listed[0].created_by_username is None
    assert users.calls == [[]]


async def test_list_resolves_authors_in_a_single_batch() -> None:
    # The page's distinct author ids are resolved in one directory call, never one
    # lookup per row (no N+1).
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    alice = uuid.uuid4()
    bob = uuid.uuid4()
    _seed_authored(backups, server.id, created_by=alice)
    _seed_authored(backups, server.id, created_by=bob)
    _seed_authored(backups, server.id, created_by=alice)  # repeated author
    uow = FakeUnitOfWork(servers=repo, backups=backups)
    users = _FakeAuthorDirectory({alice: "alice", bob: "bob"})

    listed = await ListBackups(
        uow=uow, backup_store=FakeBackupArchiveStore(), users=users
    )(community_id=_COMMUNITY, server_id=server.id)

    assert {b.created_by_username for b in listed} == {"alice", "bob"}
    # One batch call; the repeated author id is deduplicated.
    assert len(users.calls) == 1
    assert set(users.calls[0]) == {alice, bob}


async def test_list_unknown_server_is_not_found() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(ServerNotFoundError):
        await ListBackups(uow=uow, backup_store=FakeBackupArchiveStore())(
            community_id=_COMMUNITY, server_id=ServerId.new()
        )


def _seed_null_size_row(
    backups: FakeBackupRepository,
    archive: FakeBackupArchiveStore,
    server_id: ServerId,
    *,
    storage_ref: str,
    archive_bytes: bytes | None,
) -> Backup:
    """Seed a legacy NULL-size backup row, optionally with an existing archive.

    ``archive_bytes is None`` models a row whose archive is gone (no backfill
    possible); otherwise the archive exists with that body so ``size`` resolves.
    """

    backup = Backup(
        id=BackupId.new(),
        server_id=server_id,
        storage_ref=storage_ref,
        size_bytes=None,
        source=BackupSource.MANUAL,
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=_NOW,
    )
    backups.seed(backup)
    if archive_bytes is not None:
        archive.archives.add(storage_ref)
        archive.bytes_by_ref[storage_ref] = archive_bytes
    return backup


async def test_list_backfills_null_size_when_archive_exists() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    archive = FakeBackupArchiveStore()
    backup = _seed_null_size_row(
        backups, archive, server.id, storage_ref="legacy", archive_bytes=b"x" * 42
    )
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    listed = await ListBackups(uow=uow, backup_store=archive)(
        community_id=_COMMUNITY, server_id=server.id
    )

    # The returned row carries the computed size, and it is persisted to the row.
    assert listed[0].backup.size_bytes == 42
    persisted = await uow.backups.get_by_id(backup.id)
    assert persisted is not None
    assert persisted.size_bytes == 42
    # The backfill committed the write inside the open uow (the fake makes
    # update_size visible regardless, so only the commit count proves the durability).
    assert uow.commits == 1


async def test_list_leaves_null_size_when_archive_missing() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    archive = FakeBackupArchiveStore()
    backup = _seed_null_size_row(
        backups, archive, server.id, storage_ref="gone", archive_bytes=None
    )
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    listed = await ListBackups(uow=uow, backup_store=archive)(
        community_id=_COMMUNITY, server_id=server.id
    )

    # The archive is gone: the listing still succeeds and the row stays unknown.
    assert listed[0].backup.size_bytes is None
    persisted = await uow.backups.get_by_id(backup.id)
    assert persisted is not None
    assert persisted.size_bytes is None
    # Nothing was backfilled, so the read did not commit.
    assert uow.commits == 0


async def test_list_does_not_recall_store_for_backfilled_rows() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    archive = FakeBackupArchiveStore()
    _seed_null_size_row(
        backups, archive, server.id, storage_ref="legacy", archive_bytes=b"x" * 7
    )
    uow = FakeUnitOfWork(servers=repo, backups=backups)
    list_backups = ListBackups(uow=uow, backup_store=archive)

    await list_backups(community_id=_COMMUNITY, server_id=server.id)
    # First listing backfilled the row; the second must not call size again.
    await list_backups(community_id=_COMMUNITY, server_id=server.id)
    assert archive.size_calls == ["legacy"]


async def test_server_statistics_backfills_null_size_into_total() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    archive = FakeBackupArchiveStore()
    _seed_backup(
        backups, archive, server.id, storage_ref="a", size_bytes=10, created_at=_NOW
    )
    _seed_null_size_row(
        backups, archive, server.id, storage_ref="legacy", archive_bytes=b"x" * 5
    )
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    stats = await ServerBackupStatistics(uow=uow, backup_store=archive)(
        community_id=_COMMUNITY, server_id=server.id
    )
    # The legacy row's size was backfilled, so it joins the total and is no
    # longer counted as unknown.
    assert stats.total_bytes == 15
    assert stats.unknown_size_count == 0


async def test_list_survives_store_failure_and_leaves_row_null(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A non-404 store probe failure (object-store outage, connection error, fs
    # OSError) must not fail the listing (these were pure DB reads before #661):
    # the row stays NULL, is excluded from the total, and the failure is WARN
    # logged so the degraded backfill is diagnosable.
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    archive = FakeBackupArchiveStore()
    backup = _seed_null_size_row(
        backups, archive, server.id, storage_ref="legacy", archive_bytes=b"x" * 9
    )
    archive.size_error = RuntimeError("object store unavailable")
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    with caplog.at_level(logging.WARNING):
        listed = await ListBackups(uow=uow, backup_store=archive)(
            community_id=_COMMUNITY, server_id=server.id
        )

    # The listing succeeded, the row stays unknown, and nothing was committed.
    assert listed[0].backup.size_bytes is None
    persisted = await uow.backups.get_by_id(backup.id)
    assert persisted is not None
    assert persisted.size_bytes is None
    assert uow.commits == 0

    record = next(r for r in caplog.records if r.levelno == logging.WARNING)
    message = record.getMessage()
    assert str(backup.id.value) in message
    assert "object store unavailable" in message


async def test_server_statistics_survives_store_failure() -> None:
    # The statistics endpoint shares the same best-effort backfill: a store
    # failure on a legacy NULL row keeps the listing alive, the failed row stays
    # unknown and out of the total, while a sized row is still aggregated.
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    archive = FakeBackupArchiveStore()
    _seed_backup(
        backups, archive, server.id, storage_ref="a", size_bytes=10, created_at=_NOW
    )
    _seed_null_size_row(
        backups, archive, server.id, storage_ref="legacy", archive_bytes=b"x" * 5
    )
    archive.size_error = RuntimeError("object store unavailable")
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    stats = await ServerBackupStatistics(uow=uow, backup_store=archive)(
        community_id=_COMMUNITY, server_id=server.id
    )

    assert stats.total_bytes == 10
    assert stats.unknown_size_count == 1
    assert uow.commits == 0


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
        health=BackupHealth.HEALTHY,
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
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=_NOW,
    )
    backups.seed(backup)
    archive = FakeBackupArchiveStore()
    archive.archives.add("ref")
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    result = await RestoreBackup(uow=uow, backup_store=archive)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )
    assert archive.restored == [(server.id, "ref")]
    # A healthy restore is not forced-corrupt, force defaults to False, and the
    # backup's health is unchanged.
    assert result.forced_corrupt is False
    assert archive.restore_calls == [(server.id, "ref", False)]
    persisted = await backups.get_by_id(backup.id)
    assert persisted is not None
    assert persisted.health is BackupHealth.HEALTHY


def _restore_fixture(
    server: Server, *, health: BackupHealth = BackupHealth.HEALTHY
) -> tuple[FakeBackupRepository, Backup]:
    backups = FakeBackupRepository()
    backup = Backup(
        id=BackupId.new(),
        server_id=server.id,
        storage_ref="ref",
        size_bytes=None,
        source=BackupSource.MANUAL,
        health=health,
        created_by=None,
        created_at=_NOW,
    )
    backups.seed(backup)
    return backups, backup


async def test_restore_corrupt_backup_without_force_quarantines_and_raises() -> None:
    """A corrupt backup without force is refused, quarantined, not published (#743).

    The default restore is fail-closed: the integrity gate raises
    :class:`BackupCorruptError`, the use case marks that backup ``QUARANTINED`` so
    an operator does not unknowingly retry it, and ``current`` is untouched (the
    archive seam never recorded a publish).
    """

    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups, backup = _restore_fixture(server)
    archive = FakeBackupArchiveStore()
    archive.archives.add("ref")
    archive.corrupt_refs.add("ref")
    archive.corrupt_count = 4
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    with pytest.raises(BackupCorruptError) as excinfo:
        await RestoreBackup(uow=uow, backup_store=archive)(
            community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
        )
    assert excinfo.value.corrupt_count == 4
    # Refused: nothing published, force was False, the backup is now quarantined.
    assert archive.restored == []
    assert archive.restore_calls == [(server.id, "ref", False)]
    persisted = await backups.get_by_id(backup.id)
    assert persisted is not None
    assert persisted.health is BackupHealth.QUARANTINED


async def test_restore_corrupt_backup_with_force_publishes_and_quarantines() -> None:
    """``force=True`` publishes a known-corrupt backup and quarantines it (#743).

    The operator override (#703) proceeds despite corruption; the backup is
    marked ``QUARANTINED`` (it IS known-corrupt) and the result flags a forced
    corrupt restore so the edge can audit who forced it.
    """

    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups, backup = _restore_fixture(server)
    archive = FakeBackupArchiveStore()
    archive.archives.add("ref")
    archive.corrupt_refs.add("ref")
    archive.corrupt_count = 2
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    result = await RestoreBackup(uow=uow, backup_store=archive)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id, force=True
    )

    assert archive.restored == [(server.id, "ref")]
    assert archive.restore_calls == [(server.id, "ref", True)]
    assert result.forced_corrupt is True
    assert result.corrupt_count == 2
    persisted = await backups.get_by_id(backup.id)
    assert persisted is not None
    assert persisted.health is BackupHealth.QUARANTINED


async def test_restore_healthy_backup_with_force_stays_healthy() -> None:
    """Forcing a healthy backup restores normally and leaves it HEALTHY (#743)."""

    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups, backup = _restore_fixture(server)
    archive = FakeBackupArchiveStore()
    archive.archives.add("ref")
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    result = await RestoreBackup(uow=uow, backup_store=archive)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id, force=True
    )

    assert result.forced_corrupt is False
    assert archive.restored == [(server.id, "ref")]
    persisted = await backups.get_by_id(backup.id)
    assert persisted is not None
    assert persisted.health is BackupHealth.HEALTHY


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
        health=BackupHealth.HEALTHY,
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
        health=BackupHealth.HEALTHY,
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
        health=BackupHealth.HEALTHY,
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
    # An uploaded archive bypasses the create gate, so its health is unknown.
    assert backup.health is BackupHealth.UNKNOWN
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


# --- hostile-member validation branches (_validate_backup_archive, #287) ----


def _targz_with_member(info: "tarfile.TarInfo", data: bytes = b"") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.addfile(info, io.BytesIO(data) if data else None)
    return buf.getvalue()


def test_validate_rejects_absolute_member_name() -> None:
    import tarfile

    info = tarfile.TarInfo(name="/etc/passwd")
    info.size = 1
    content = _targz_with_member(info, b"x")
    with pytest.raises(InvalidBackupArchiveError):
        _validate_backup_archive(content, max_entries=10)


def test_validate_rejects_symlink_member() -> None:
    import tarfile

    info = tarfile.TarInfo(name="link")
    info.type = tarfile.SYMTYPE
    info.linkname = "/etc/passwd"
    content = _targz_with_member(info)
    with pytest.raises(InvalidBackupArchiveError):
        _validate_backup_archive(content, max_entries=10)


def test_validate_rejects_hardlink_member() -> None:
    import tarfile

    info = tarfile.TarInfo(name="hard")
    info.type = tarfile.LNKTYPE
    info.linkname = "world/level.dat"
    content = _targz_with_member(info)
    with pytest.raises(InvalidBackupArchiveError):
        _validate_backup_archive(content, max_entries=10)


def test_validate_rejects_device_member() -> None:
    import tarfile

    info = tarfile.TarInfo(name="dev/sda")
    info.type = tarfile.CHRTYPE
    info.devmajor = 1
    info.devminor = 3
    content = _targz_with_member(info)
    with pytest.raises(InvalidBackupArchiveError):
        _validate_backup_archive(content, max_entries=10)


def test_validate_rejects_too_many_entries() -> None:
    content = _targz({f"f{i}": b"x" for i in range(5)})
    with pytest.raises(InvalidBackupArchiveError):
        _validate_backup_archive(content, max_entries=3)


def test_validate_rejects_decompression_bomb_member() -> None:
    # A single member whose decompressed body exceeds the cap is refused before any
    # store, even though the compressed archive is tiny (gzip-bomb amplification).
    content = _targz({"bomb": b"\x00" * 4096})
    with pytest.raises(InvalidBackupArchiveError):
        _validate_backup_archive(content, max_entries=10, max_decompressed_bytes=1024)


def test_validate_accepts_safe_archive_within_caps() -> None:
    content = _targz({"server.properties": b"k=v", "world/level.dat": b"w"})
    # No raise: a benign archive within both the entry and decompressed caps.
    _validate_backup_archive(content, max_entries=10, max_decompressed_bytes=1024)


async def test_upload_rejects_decompression_bomb_before_storing() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    archive = FakeBackupArchiveStore()
    uow = FakeUnitOfWork(servers=repo)
    content = _targz({"bomb": b"\x00" * 4096})
    with pytest.raises(InvalidBackupArchiveError):
        await UploadBackup(
            uow=uow,
            backup_store=archive,
            clock=FakeClock(_NOW),
            max_decompressed_bytes=1024,
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
    # A legacy NULL-size row whose archive is gone: stays unknown (no backfill
    # possible), so it is excluded from total_bytes and counted as unknown.
    backups.seed(
        Backup(
            id=BackupId.new(),
            server_id=server.id,
            storage_ref="c",
            size_bytes=None,
            source=BackupSource.MANUAL,
            health=BackupHealth.HEALTHY,
            created_by=None,
            created_at=_NOW,
        )
    )
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    stats = await ServerBackupStatistics(uow=uow, backup_store=archive)(
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
    statistics = ServerBackupStatistics(uow=uow, backup_store=FakeBackupArchiveStore())
    stats = await statistics(community_id=_COMMUNITY, server_id=server.id)
    assert stats.count == 0
    assert stats.total_bytes == 0
    assert stats.unknown_size_count == 0
    assert stats.newest is None and stats.oldest is None


async def test_server_statistics_unknown_server_is_not_found() -> None:
    uow = FakeUnitOfWork()
    with pytest.raises(ServerNotFoundError):
        await ServerBackupStatistics(uow=uow, backup_store=FakeBackupArchiveStore())(
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
