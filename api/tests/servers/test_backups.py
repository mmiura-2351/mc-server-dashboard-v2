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

import asyncio
import datetime as dt
import gzip
import io
import logging
import tarfile
import threading
import tracemalloc
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from mc_server_dashboard_api.servers.application import backups as backups_module
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
    BackupStorageUnavailableError,
    BackupUnreadableError,
    BackupUnsettledError,
    FileTooLargeError,
    InvalidBackupArchiveError,
    InvalidFilePathError,
    ServerAssignedError,
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
from tests import gzip_shapes
from tests.servers import archive_cap_shapes
from tests.servers.executor_starvation import (
    IN_FLIGHT,
    InFlight,
    assert_default_executor_free,
)
from tests.servers.fakes import (
    DatabaseOutageError,
    FakeBackupArchiveStore,
    FakeBackupRepository,
    FakeClock,
    FakeControlPlane,
    FakeServerRepository,
    FakeUnitOfWork,
    HangingSizeStore,
    OutageUnitOfWork,
    SessionTrackingUnitOfWork,
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


# A stop holds its Worker until the final snapshot settles (issue #3998): an
# archive of ``current`` taken in that window lacks the snapshot still in flight,
# and a restore made in it is overwritten by that snapshot. Both refuse the row
# ``server_assigned`` in every observed state the row reads at rest.
_AT_REST_OBSERVED = [
    ObservedState.STOPPED,
    ObservedState.CRASHED,
    ObservedState.UNKNOWN,
]


@pytest.mark.parametrize("observed", _AT_REST_OBSERVED)
async def test_create_at_rest_refuses_a_server_a_worker_still_holds(
    observed: ObservedState,
) -> None:
    server = _server(desired=DesiredState.STOPPED, observed=observed, worker=_WORKER)
    repo = FakeServerRepository()
    repo.seed(server)
    uow = FakeUnitOfWork(servers=repo)
    control_plane = FakeControlPlane()
    archive = FakeBackupArchiveStore()
    create = _make_create(uow, control_plane, archive)

    with pytest.raises(ServerAssignedError):
        await create(
            community_id=_COMMUNITY,
            server_id=server.id,
            source=BackupSource.MANUAL,
        )

    assert archive.created == []
    assert await uow.backups.list_for_server(server.id) == []
    assert control_plane.dispatched == []


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
    # The backfill committed the write in a unit of work of its own (the fake makes
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


@pytest.mark.parametrize(
    "read",
    [
        pytest.param(ListBackups, id="list"),
        pytest.param(ServerBackupStatistics, id="statistics"),
    ],
)
async def test_a_hanging_size_probe_holds_no_unit_of_work_open(
    read: type[ListBackups] | type[ServerBackupStatistics],
) -> None:
    # The backfill's size probe is a storage HEAD, which can hang for as long as
    # the store's timeouts allow. It runs with no unit of work open, so a hanging
    # HEAD keeps no pool connection checked out (issue #4107); the size is then
    # written in a short unit of work of its own.
    server = _at_rest()
    uow = SessionTrackingUnitOfWork()
    uow.servers.seed(server)
    archive = HangingSizeStore()
    backup = _seed_null_size_row(
        uow.backups, archive, server.id, storage_ref="legacy", archive_bytes=b"x" * 42
    )

    reading = asyncio.create_task(
        read(uow=uow, backup_store=archive)(
            community_id=_COMMUNITY, server_id=server.id
        )
    )
    try:
        await archive.probing.wait()
        open_while_probing = uow.open
    finally:
        archive.release.set()
    await reading

    assert open_while_probing is False
    persisted = await uow.backups.get_by_id(backup.id)
    assert persisted is not None
    assert persisted.size_bytes == 42
    assert uow.commits == 1


async def test_list_fails_when_the_store_is_unavailable() -> None:
    # The decision of issue #2405 (stated in _backfill_null_sizes' docstring): an
    # object-store outage fails the listing instead of degrading it to null sizes.
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    archive = FakeBackupArchiveStore()
    backup = _seed_null_size_row(
        backups, archive, server.id, storage_ref="legacy", archive_bytes=b"x" * 9
    )
    archive.size_error = BackupStorageUnavailableError(str(server.id.value))
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    with pytest.raises(BackupStorageUnavailableError):
        await ListBackups(uow=uow, backup_store=archive)(
            community_id=_COMMUNITY, server_id=server.id
        )

    # Nothing was written: the row is still NULL for the next, healthier read.
    persisted = await uow.backups.get_by_id(backup.id)
    assert persisted is not None
    assert persisted.size_bytes is None
    assert uow.commits == 0


async def test_list_does_not_swallow_a_programming_error_in_the_backfill() -> None:
    # The handler tolerates BackupNotFoundError and nothing else (issue #2405): a
    # bug inside the backfill itself must surface, not hide behind a 200 whose null
    # sizes look exactly like legitimately unrecorded ones.
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    archive = FakeBackupArchiveStore()
    _seed_null_size_row(
        backups, archive, server.id, storage_ref="legacy", archive_bytes=b"x" * 9
    )
    archive.size_error = TypeError("size() got an unexpected keyword argument")
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    with pytest.raises(TypeError):
        await ListBackups(uow=uow, backup_store=archive)(
            community_id=_COMMUNITY, server_id=server.id
        )


async def test_server_statistics_fails_when_the_store_is_unavailable() -> None:
    # The statistics endpoint shares the backfill, so it shares the decision: a
    # partial total computed during an outage is indistinguishable from an honest
    # one, so the outage is reported instead of averaged away (issue #2405).
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
    archive.size_error = BackupStorageUnavailableError(str(server.id.value))
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    with pytest.raises(BackupStorageUnavailableError):
        await ServerBackupStatistics(uow=uow, backup_store=archive)(
            community_id=_COMMUNITY, server_id=server.id
        )

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


@pytest.mark.parametrize("observed", _AT_REST_OBSERVED)
async def test_restore_refuses_a_server_a_worker_still_holds(
    observed: ObservedState,
) -> None:
    server = _server(desired=DesiredState.STOPPED, observed=observed, worker=_WORKER)
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

    with pytest.raises(ServerAssignedError):
        await RestoreBackup(uow=uow, backup_store=archive)(
            community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
        )

    assert archive.restored == []
    assert archive.restore_calls == []


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


@pytest.mark.parametrize("force", [False, True])
async def test_restore_unreadable_backup_is_refused_without_reading(
    force: bool,
) -> None:
    """An UNREADABLE backup cannot be restored, with or without force (#2374).

    Its archive's bytes are gone, so the operator override (#703) that publishes a
    corrupt world has nothing to publish. The refusal comes from the recorded
    verdict, before the archive is read, and leaves the verdict as it was: only a
    sweep that reads the archive back in full revises it.
    """

    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups, backup = _restore_fixture(server, health=BackupHealth.UNREADABLE)
    archive = FakeBackupArchiveStore()
    archive.archives.add("ref")
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    with pytest.raises(BackupUnreadableError):
        await RestoreBackup(uow=uow, backup_store=archive)(
            community_id=_COMMUNITY,
            server_id=server.id,
            backup_id=backup.id,
            force=force,
        )
    assert archive.restore_calls == []
    persisted = await backups.get_by_id(backup.id)
    assert persisted is not None
    assert persisted.health is BackupHealth.UNREADABLE


@pytest.mark.parametrize("force", [False, True])
async def test_restore_that_finds_the_archive_unreadable_marks_it_unreadable(
    force: bool,
) -> None:
    """A restore can be the first read to find an archive's bytes gone (#3230): a
    backup recorded HEALTHY whose archive will not extract. It is refused with
    :class:`BackupUnreadableError`, ``force`` or not, and the verdict it reached is
    recorded — the same one the sweep would have written — so the next attempt is
    refused up front instead of re-reading the archive."""

    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups, backup = _restore_fixture(server)
    archive = FakeBackupArchiveStore()
    archive.archives.add("ref")
    archive.unreadable_refs.add("ref")
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    with pytest.raises(BackupUnreadableError):
        await RestoreBackup(uow=uow, backup_store=archive)(
            community_id=_COMMUNITY,
            server_id=server.id,
            backup_id=backup.id,
            force=force,
        )
    assert archive.restored == []
    persisted = await backups.get_by_id(backup.id)
    assert persisted is not None
    assert persisted.health is BackupHealth.UNREADABLE


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
    # One timeline for the archive delete and the metadata-row delete (issue
    # #2546): both-gone is the end state a swapped implementation also reaches,
    # so only their position on a shared timeline can pin "archive first".
    archive.events = backups.events

    await DeleteBackup(uow=uow, backup_store=archive)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )
    # Both the archive and the metadata row are gone.
    assert archive.deleted == [(server.id, "ref")]
    assert "ref" not in archive.archives
    assert await uow.backups.get_by_id(backup.id) is None
    # ...and the archive was removed BEFORE the row: a crash between the two must
    # leave a dangling row (re-deletable), never an orphaned archive with no row
    # to find it by (module docstring). Endpoint-only asserts stay green under a
    # swap; the shared timeline reddens.
    assert archive.events.index((server.id, "delete-archive")) < archive.events.index(
        (server.id, "delete-row")
    )


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

    use_case = DownloadBackup(uow=uow, backup_store=archive)
    backup_row = await use_case.resolve(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )
    size_bytes = await use_case.archive_size(
        community_id=_COMMUNITY, server_id=server.id, backup=backup_row
    )
    stream = use_case.archive_stream(
        community_id=_COMMUNITY, server_id=server.id, backup=backup_row
    )
    blob = b"".join([chunk async for chunk in stream])
    assert blob == b"x" * 5
    assert size_bytes == 5


async def test_download_resolves_the_backup_row_once() -> None:
    # The size probe and the stream both read the same immutable storage_ref off
    # one row, so a full download loads the metadata row exactly once (issue #2456).
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    archive = FakeBackupArchiveStore()
    backup = _seed_backup(backups, archive, server.id, storage_ref="ref", size_bytes=5)
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    use_case = DownloadBackup(uow=uow, backup_store=archive)
    backup_row = await use_case.resolve(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )
    await use_case.archive_size(
        community_id=_COMMUNITY, server_id=server.id, backup=backup_row
    )
    stream = use_case.archive_stream(
        community_id=_COMMUNITY, server_id=server.id, backup=backup_row
    )
    _ = b"".join([chunk async for chunk in stream])
    assert backups.get_by_id_calls == [backup.id]


async def test_download_streams_only_the_requested_byte_range() -> None:
    # A resumed download asks for the archive's tail (issue #2372): the range
    # reaches the store, which reads only those bytes.
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    archive = FakeBackupArchiveStore()
    backup = _seed_backup(backups, archive, server.id, storage_ref="ref", size_bytes=10)
    archive.bytes_by_ref["ref"] = b"0123456789"
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    use_case = DownloadBackup(uow=uow, backup_store=archive)
    backup_row = await use_case.resolve(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )
    stream = use_case.archive_stream(
        community_id=_COMMUNITY,
        server_id=server.id,
        backup=backup_row,
        byte_range=(4, 6),
    )
    assert b"".join([chunk async for chunk in stream]) == b"456"


async def test_download_size_comes_from_the_archive_store_not_the_row() -> None:
    # ``Backup.size_bytes`` is nullable (legacy rows, #661), so the declared
    # Content-Length must come from the store instead (issue #2312).
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    archive = FakeBackupArchiveStore()
    backup = _seed_null_size_row(
        backups, archive, server.id, storage_ref="ref", archive_bytes=b"x" * 7
    )
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    use_case = DownloadBackup(uow=uow, backup_store=archive)
    backup_row = await use_case.resolve(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )
    size_bytes = await use_case.archive_size(
        community_id=_COMMUNITY, server_id=server.id, backup=backup_row
    )
    stream = use_case.archive_stream(
        community_id=_COMMUNITY, server_id=server.id, backup=backup_row
    )
    assert backup.size_bytes is None
    assert size_bytes == 7
    assert b"".join([chunk async for chunk in stream]) == b"x" * 7


async def test_download_unknown_backup_is_not_found() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    uow = FakeUnitOfWork(servers=repo)
    with pytest.raises(BackupNotFoundError):
        await DownloadBackup(uow=uow, backup_store=FakeBackupArchiveStore()).resolve(
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
        await DownloadBackup(uow=uow, backup_store=archive).resolve(
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


def test_validate_counts_the_zeros_past_the_end_marker_toward_the_cap() -> None:
    # The read on to the gzip end decompresses whatever follows the end-of-archive
    # marker, so it counts toward the same cap as member data (issue #3778).
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name="server.properties")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"k=v"))
    content = gzip.compress(buf.getvalue() + bytes(64 * 1024))
    with pytest.raises(InvalidBackupArchiveError):
        _validate_backup_archive(
            content, max_entries=10, max_decompressed_bytes=32 * 1024
        )


def test_validate_accepts_safe_archive_within_caps() -> None:
    content = _targz({"server.properties": b"k=v", "world/level.dat": b"w"})
    # No raise: a benign archive within both the entry and decompressed caps. The
    # cap covers the zeros past the end-of-archive marker too (issue #3778), and
    # ``tarfile`` pads its archive to a 10 KiB record.
    _validate_backup_archive(content, max_entries=10, max_decompressed_bytes=10 * 1024)


@pytest.mark.parametrize("build", archive_cap_shapes.OVER_CAP)
def test_validate_counts_what_tarfile_reads_outside_member_data(
    build: Callable[[], bytes],
) -> None:
    # Skipped sparse data, PAX data and a GNU long name are decompressed as much
    # as member data is, so they count toward the same cap (issue #3810).
    with pytest.raises(InvalidBackupArchiveError, match="exceeds cap"):
        _validate_backup_archive(
            build(), max_entries=10, max_decompressed_bytes=archive_cap_shapes.CAP
        )


@pytest.mark.parametrize("build", archive_cap_shapes.OVER_THE_BUDGET)
def test_validate_refuses_extended_headers_over_the_budget(
    build: Callable[[], bytes],
) -> None:
    # tarfile holds an extended header's data whole, so what the walk reads
    # between two members is bounded (issue #3810). The cap is the production
    # one: only that bound can refuse this archive.
    with pytest.raises(InvalidBackupArchiveError) as refused:
        _validate_backup_archive(build(), max_entries=10)
    assert str(refused.value) == archive_cap_shapes.OVER_THE_BUDGET_DETAIL


@pytest.mark.parametrize(("build", "detail"), archive_cap_shapes.OVER_WHAT_MEMBERS_HOLD)
def test_validate_refuses_members_that_hold_too_much(
    build: Callable[[], bytes], detail: str
) -> None:
    # tarfile keeps every member, with its names and a copy of the global PAX
    # records, so what they hold is bounded (issue #3810). The cap is the
    # production one: only that bound can refuse this archive.
    with pytest.raises(InvalidBackupArchiveError) as refused:
        _validate_backup_archive(build(), max_entries=10_000)
    assert str(refused.value) == detail


@pytest.mark.parametrize("build", archive_cap_shapes.EXTENDED_HEADER_CHAINS)
def test_validate_refuses_a_chain_of_extended_headers(
    build: Callable[[], bytes],
) -> None:
    # tarfile recurses once per extended header, so a long chain of them raised
    # RecursionError, a 500 (issue #3810).
    with pytest.raises(InvalidBackupArchiveError, match="not a gzip tar"):
        _validate_backup_archive(build(), max_entries=10)


@pytest.mark.parametrize("slack", [0, -1], ids=["at-the-cap", "one-byte-under"])
def test_validate_holds_an_archive_to_its_cap_to_the_byte(slack: int) -> None:
    # The cap is the stream's own length: header, data and padding.
    content = _targz({"a.txt": b"A"})
    cap = len(gzip.decompress(content)) + slack

    if slack:
        with pytest.raises(InvalidBackupArchiveError, match="exceeds cap"):
            _validate_backup_archive(
                content, max_entries=10, max_decompressed_bytes=cap
            )
    else:
        _validate_backup_archive(content, max_entries=10, max_decompressed_bytes=cap)


def test_validate_counts_a_sparse_members_holes_toward_the_cap() -> None:
    # An old-GNU sparse member stores 512 bytes and declares a real size of 8 MiB.
    # Restore writes the holes too, so they count though nothing decompresses them.
    header = archive_cap_shapes.old_gnu_sparse_header(
        "sparse.bin",
        stored=512,
        regions=((0, 512),),
        realsize=8 * archive_cap_shapes.MIB,
    )

    with pytest.raises(InvalidBackupArchiveError, match="exceeds cap"):
        _validate_backup_archive(
            gzip.compress(header + bytes(512) + bytes(1024)),
            max_entries=10,
            max_decompressed_bytes=archive_cap_shapes.MIB,
        )


def test_validate_refuses_an_archive_that_seeks_back() -> None:
    # A seek back would decompress the stream again from its start, once per
    # sparse member here, so it is refused (issues #3798, #3810). The cap is the
    # production one. The refusal is EndCheckedReader's: a walk that read on
    # from where it did not seek back would be refused too, later.
    with pytest.raises(InvalidBackupArchiveError, match="not a gzip tar") as refused:
        _validate_backup_archive(
            archive_cap_shapes.sparse_maps_past_stored_data(), max_entries=10_000
        )
    assert str(refused.value.__cause__) == "a member's data runs past the member"


def test_validate_counts_directory_members_toward_the_entry_cap() -> None:
    with pytest.raises(InvalidBackupArchiveError, match="too many entries"):
        _validate_backup_archive(archive_cap_shapes.directories(5), max_entries=5)


def test_validate_keeps_no_member_it_walked_past() -> None:
    # ``tarfile`` keeps every member it reads, with its PAX records, unless it
    # is opened not to (issue #3931). 500 members holding 30 MiB of PAX
    # comments, 7.5 Mi characters within the gate's limits, are walked one
    # member at a time, in under 12 MiB.
    content = archive_cap_shapes.commented_directories(500, "\U0001f600")

    tracemalloc.start()
    try:
        _validate_backup_archive(content, max_entries=10_000)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 12 * archive_cap_shapes.MIB


# --- no directory at the root server.properties path (issue #2869) ---------
#
# An uploaded archive is restorable through the SAME restore flow as a created
# one, and the restore republishes its members verbatim as ``current/`` (member
# names are already server-root-relative: the packer uses ``arcname=child.name``).
# So a member named ``server.properties/x`` plants the directory #2812 describes,
# and the restore's own platform-key re-apply is what then fails on it. The
# extraction lives in Storage, below this seam, so the archive is refused HERE --
# before it is stored -- the way every other hostile shape is.


def test_validate_rejects_member_under_root_properties() -> None:
    content = _targz({"server.properties/notes.txt": b"X"})
    with pytest.raises(InvalidFilePathError) as caught:
        _validate_backup_archive(content, max_entries=10)
    assert caught.value.reason == "platform_managed_path"


def test_validate_rejects_a_directory_member_at_root_properties() -> None:
    import tarfile

    info = tarfile.TarInfo(name="server.properties/")
    info.type = tarfile.DIRTYPE
    content = _targz_with_member(info)
    with pytest.raises(InvalidFilePathError) as caught:
        _validate_backup_archive(content, max_entries=10)
    assert caught.value.reason == "platform_managed_path"


def test_validate_accepts_a_member_under_a_nested_properties_dir() -> None:
    # Only the ROOT name is guarded: a ``server.properties`` directory deeper in
    # the tree is ordinary user data and must still restore.
    content = _targz({"backups/server.properties/notes.txt": b"X"})
    # No raise.
    _validate_backup_archive(content, max_entries=10)


# --- the generation marker's name is reserved at the root (issue #3433) -----
#
# A restore republishes the members verbatim, so a member at the Worker's
# generation-marker name would plant a Worker-private entry in the store -- as a
# directory, one that fails every hydrate. Refused before the archive is stored,
# with the same 422 ``reserved_name`` the files-API doors give.


@pytest.mark.parametrize(
    "member", [".mcsd_generation", ".mcsd_generation/stray.txt", "./.mcsd_generation"]
)
def test_validate_rejects_a_member_at_the_reserved_marker_name(member: str) -> None:
    content = _targz({member: b"X"})
    with pytest.raises(InvalidFilePathError) as caught:
        _validate_backup_archive(content, max_entries=10)
    assert caught.value.reason == "reserved_name"


def test_validate_rejects_a_directory_member_at_the_reserved_marker_name() -> None:
    import tarfile

    info = tarfile.TarInfo(name=".mcsd_generation/")
    info.type = tarfile.DIRTYPE
    content = _targz_with_member(info)
    with pytest.raises(InvalidFilePathError) as caught:
        _validate_backup_archive(content, max_entries=10)
    assert caught.value.reason == "reserved_name"


def test_validate_accepts_a_nested_marker_name() -> None:
    content = _targz({"world/.mcsd_generation/stray.txt": b"X"})
    # No raise.
    _validate_backup_archive(content, max_entries=10)


async def test_upload_rejects_member_under_root_properties_before_storing() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    archive = FakeBackupArchiveStore()
    uow = FakeUnitOfWork(servers=repo)
    with pytest.raises(InvalidFilePathError) as caught:
        await UploadBackup(uow=uow, backup_store=archive, clock=FakeClock(_NOW))(
            community_id=_COMMUNITY,
            server_id=server.id,
            content=_targz({"server.properties/notes.txt": b"X"}),
            created_by=uuid.uuid4(),
        )
    assert caught.value.reason == "platform_managed_path"
    assert archive.stored == []


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


# --- the archive is validated off the event loop (issue #3801) -------------
#
# Validation decompresses up to the 8 GiB cap, so on the event loop it would
# stall every other request on the process. Each fake validator below blocks
# until the test's own coroutine releases it: only a loop left free while
# validation runs can do that. The timeout turns a blocked loop into a failure
# rather than a hang.


@dataclass
class _BlockingValidator:
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)

    def __call__(self, content: bytes, **_caps: int) -> None:
        self.entered.set()
        try:
            if not self.release.wait(10):
                raise AssertionError("the event loop never ran during validation")
        finally:
            self.finished.set()


def _upload_task(
    archive: FakeBackupArchiveStore, uow: FakeUnitOfWork, server: Server
) -> asyncio.Task[Backup]:
    upload = UploadBackup(uow=uow, backup_store=archive, clock=FakeClock(_NOW))
    return asyncio.create_task(
        upload(
            community_id=_COMMUNITY,
            server_id=server.id,
            content=_valid_tar_gz(),
            created_by=uuid.uuid4(),
        )
    )


async def test_upload_leaves_the_event_loop_free_while_it_validates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    archive = FakeBackupArchiveStore()
    uow = FakeUnitOfWork(servers=repo)
    validator = _BlockingValidator()
    monkeypatch.setattr(backups_module, "_validate_backup_archive", validator)

    upload = _upload_task(archive, uow, server)
    assert await asyncio.to_thread(validator.entered.wait, 10)
    # This coroutine runs while validation is under way.
    validator.release.set()
    await upload

    assert archive.stored == [server.id]


async def test_cancelling_an_upload_mid_validation_stores_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    archive = FakeBackupArchiveStore()
    uow = FakeUnitOfWork(servers=repo)
    validator = _BlockingValidator()
    monkeypatch.setattr(backups_module, "_validate_backup_archive", validator)

    upload = _upload_task(archive, uow, server)
    try:
        assert await asyncio.to_thread(validator.entered.wait, 10)
        upload.cancel()
        # The request ends at once: it does not wait for the validating thread.
        with pytest.raises(asyncio.CancelledError):
            await upload
        assert not validator.finished.is_set()
    finally:
        validator.release.set()
    # The thread runs on to its own end, and its verdict reaches no one.
    assert await asyncio.to_thread(validator.finished.wait, 10)

    assert archive.stored == []
    assert await uow.backups.list_for_server(server.id) == []


async def test_uploads_validating_together_leave_the_default_executor_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Validation runs on its own pool (issue #3843). On the default executor, a
    # few crafted uploads validating for minutes would take every worker from
    # the fs Storage calls, the sweep and everything else that runs there.
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    archive = FakeBackupArchiveStore()
    in_flight = InFlight()
    monkeypatch.setattr(
        backups_module,
        "_validate_backup_archive",
        lambda content, **_caps: in_flight.block(),
    )

    uploads = [
        _upload_task(archive, FakeUnitOfWork(servers=repo), server)
        for _ in range(IN_FLIGHT)
    ]
    try:
        await assert_default_executor_free(in_flight)
    finally:
        await asyncio.gather(*uploads)

    assert archive.stored == [server.id] * IN_FLIGHT


# --- gzip members and zero padding read as gzip reads them -----------------
#
# Concatenated gzip members, with zero padding between and after them, are part
# of the gzip format. Issue #3835 replaces the gzip layer to bound what they cost,
# so this pins what it must keep accepting.


def _raw_tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


_RAW_TAR = _raw_tar({"server.properties": b"k=v", "world/level.dat": b"w" * 2048})


def _in_gzip_members(raw: bytes, parts: int) -> bytes:
    """``raw`` cut into ``parts`` pieces, each compressed as its own gzip member."""

    step = -(-len(raw) // parts)
    return b"".join(
        gzip.compress(raw[at : at + step], mtime=0) for at in range(0, len(raw), step)
    )


def test_validate_accepts_a_tar_split_across_gzip_members_with_zero_padding() -> None:
    # The cuts fall inside records.
    head, tail = _RAW_TAR[:2000], _RAW_TAR[2000:]
    content = (
        _in_gzip_members(head, 2) + bytes(512) + _in_gzip_members(tail, 1) + bytes(1024)
    )

    # No raise.
    _validate_backup_archive(content, max_entries=10)


# The gzip layer gives one verdict at every gzip read site (issues #3835,
# #3847): at most 10,000 gzip members, nothing but zeros between or after them,
# and a member header whose CRC matches and whose reserved flag bits are clear.
_GZIP_ACCEPTED = gzip_shapes.accepted(_RAW_TAR)
_GZIP_REFUSED = gzip_shapes.refused(_RAW_TAR)


@pytest.mark.parametrize(
    "content", list(_GZIP_ACCEPTED.values()), ids=list(_GZIP_ACCEPTED)
)
def test_validate_accepts_a_gzip_stream_the_gzip_layer_takes(content: bytes) -> None:
    # No raise.
    _validate_backup_archive(content, max_entries=10)


@pytest.mark.parametrize(
    "content", list(_GZIP_REFUSED.values()), ids=list(_GZIP_REFUSED)
)
def test_validate_rejects_a_gzip_stream_the_gzip_layer_refuses(content: bytes) -> None:
    with pytest.raises(InvalidBackupArchiveError, match="not a gzip tar archive"):
        _validate_backup_archive(content, max_entries=10)


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


def _valid_tar_gz() -> bytes:
    """A tiny valid tar.gz for upload tests."""
    return _targz({"server.properties": b"motd=test"})


# --- Archive-before-row ordering (issue #4086) -------------------------------
#
# A backup row is recorded only once its archive exists, with its size; a failure
# after the archive and before the row's COMMIT is sent deletes it again (the
# tests at the end of this section pin what happens after). On 2026-10-15 a
# create that committed its row first left 21 rows with no archive behind: the
# store refused each archive and the database went down with it, so no row could
# be deleted again.


class _FailsAndTakesTheDatabaseDown(FakeBackupArchiveStore):
    """The store refuses the archive, and the database goes down with it."""

    def __init__(self, uow: OutageUnitOfWork) -> None:
        super().__init__()
        self._uow = uow

    async def create_from_current(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        self._uow.down = True
        raise BackupStorageUnavailableError(str(server_id.value))

    async def store(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        stream: AsyncIterator[bytes],
        storage_ref: str,
    ) -> None:
        self._uow.down = True
        raise BackupStorageUnavailableError(str(server_id.value))


class _WritesThenTakesTheDatabaseDown(FakeBackupArchiveStore):
    """The store writes the archive, then the database goes down."""

    def __init__(self, uow: OutageUnitOfWork) -> None:
        super().__init__()
        self._uow = uow

    async def create_from_current(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        await super().create_from_current(
            community_id=community_id, server_id=server_id, storage_ref=storage_ref
        )
        self._uow.down = True

    async def store(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        stream: AsyncIterator[bytes],
        storage_ref: str,
    ) -> None:
        await super().store(
            community_id=community_id,
            server_id=server_id,
            stream=stream,
            storage_ref=storage_ref,
        )
        self._uow.down = True


class _ArchiveDeleteFails(_WritesThenTakesTheDatabaseDown):
    async def delete(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        raise BackupStorageUnavailableError(str(server_id.value))


class _Hangs(FakeBackupArchiveStore):
    """The archive write (``hang_in="write"``) or the size read never returns."""

    def __init__(self, hang_in: str) -> None:
        super().__init__()
        self._hang_in = hang_in
        self.entered = asyncio.Event()

    async def _hang(self, at: str) -> None:
        if at == self._hang_in:
            self.entered.set()
            await asyncio.Event().wait()

    async def create_from_current(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        await self._hang("write")
        await super().create_from_current(
            community_id=community_id, server_id=server_id, storage_ref=storage_ref
        )

    async def store(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        stream: AsyncIterator[bytes],
        storage_ref: str,
    ) -> None:
        await self._hang("write")
        await super().store(
            community_id=community_id,
            server_id=server_id,
            stream=stream,
            storage_ref=storage_ref,
        )

    async def size(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> int:
        await self._hang("size")
        return await super().size(
            community_id=community_id, server_id=server_id, storage_ref=storage_ref
        )


async def _cancel_once_entered(
    task: asyncio.Task[Backup], entered: asyncio.Event
) -> None:
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _outage_uow(server: Server) -> OutageUnitOfWork:
    uow = OutageUnitOfWork()
    uow.servers.seed(server)
    return uow


def _upload_of(uow: FakeUnitOfWork, archive: FakeBackupArchiveStore) -> UploadBackup:
    return UploadBackup(uow=uow, backup_store=archive, clock=FakeClock(_NOW))


async def test_create_writes_its_archive_before_its_row() -> None:
    server = _at_rest()
    uow = _outage_uow(server)
    archive = FakeBackupArchiveStore()
    archive.events = uow.backups.events

    await _make_create(uow, FakeControlPlane(), archive)(
        community_id=_COMMUNITY, server_id=server.id, source=BackupSource.MANUAL
    )

    assert uow.backups.events == [
        (server.id, "create-archive"),
        (server.id, "add-row"),
    ]


async def test_create_inserts_its_row_with_the_archive_size() -> None:
    server = _at_rest()
    uow = _outage_uow(server)
    archive = FakeBackupArchiveStore()
    inserted: list[int | None] = []
    insert = uow.backups.add

    async def _recording_add(backup: Backup) -> None:
        inserted.append(backup.size_bytes)
        await insert(backup)

    uow.backups.add = _recording_add  # type: ignore[method-assign]

    await _make_create(uow, FakeControlPlane(), archive)(
        community_id=_COMMUNITY, server_id=server.id, source=BackupSource.MANUAL
    )

    # No reader ever sees the row without its size.
    assert inserted == [len(b"archive-bytes")]


async def test_create_whose_cleanup_cannot_reach_the_database_leaves_no_row() -> None:
    server = _at_rest()
    uow = _outage_uow(server)
    archive = _FailsAndTakesTheDatabaseDown(uow)

    with pytest.raises(BackupStorageUnavailableError):
        await _make_create(uow, FakeControlPlane(), archive)(
            community_id=_COMMUNITY,
            server_id=server.id,
            source=BackupSource.SCHEDULED,
        )

    assert uow.backups.by_id == {}
    assert archive.deleted == []


async def test_a_create_cancelled_mid_archive_leaves_no_row() -> None:
    server = _at_rest()
    uow = _outage_uow(server)
    archive = _Hangs("write")
    create = _make_create(uow, FakeControlPlane(), archive)

    await _cancel_once_entered(
        asyncio.create_task(
            create(
                community_id=_COMMUNITY,
                server_id=server.id,
                source=BackupSource.SCHEDULED,
            )
        ),
        archive.entered,
    )

    assert uow.backups.by_id == {}


async def test_a_create_cancelled_after_its_archive_deletes_it() -> None:
    server = _at_rest()
    uow = _outage_uow(server)
    archive = _Hangs("size")
    create = _make_create(uow, FakeControlPlane(), archive)

    await _cancel_once_entered(
        asyncio.create_task(
            create(
                community_id=_COMMUNITY,
                server_id=server.id,
                source=BackupSource.SCHEDULED,
            )
        ),
        archive.entered,
    )

    assert uow.backups.by_id == {}
    assert archive.created == [server.id]
    assert archive.archives == set()


async def test_create_whose_row_cannot_be_recorded_deletes_its_archive() -> None:
    server = _at_rest()
    uow = _outage_uow(server)
    archive = _WritesThenTakesTheDatabaseDown(uow)

    with pytest.raises(DatabaseOutageError):
        await _make_create(uow, FakeControlPlane(), archive)(
            community_id=_COMMUNITY, server_id=server.id, source=BackupSource.MANUAL
        )

    assert uow.backups.by_id == {}
    assert archive.created == [server.id]
    assert archive.archives == set()


async def test_an_archive_that_cannot_be_deleted_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = _at_rest()
    uow = _outage_uow(server)
    archive = _ArchiveDeleteFails(uow)

    # The failure that stopped the backup is the one raised, not the delete's.
    with pytest.raises(DatabaseOutageError):
        await _make_create(uow, FakeControlPlane(), archive)(
            community_id=_COMMUNITY, server_id=server.id, source=BackupSource.MANUAL
        )

    (record,) = [r for r in caplog.records if r.name == backups_module.__name__]
    assert record.levelno == logging.WARNING


async def test_upload_stores_its_archive_before_its_row() -> None:
    server = _at_rest()
    uow = _outage_uow(server)
    archive = FakeBackupArchiveStore()
    archive.events = uow.backups.events

    await _upload_of(uow, archive)(
        community_id=_COMMUNITY, server_id=server.id, content=_valid_tar_gz()
    )

    assert uow.backups.events == [
        (server.id, "store-archive"),
        (server.id, "add-row"),
    ]


async def test_upload_whose_cleanup_cannot_reach_the_database_leaves_no_row() -> None:
    server = _at_rest()
    uow = _outage_uow(server)
    archive = _FailsAndTakesTheDatabaseDown(uow)

    with pytest.raises(BackupStorageUnavailableError):
        await _upload_of(uow, archive)(
            community_id=_COMMUNITY, server_id=server.id, content=_valid_tar_gz()
        )

    assert uow.backups.by_id == {}
    assert archive.deleted == []


async def test_an_upload_cancelled_mid_store_leaves_no_row() -> None:
    server = _at_rest()
    uow = _outage_uow(server)
    archive = _Hangs("write")
    upload = _upload_of(uow, archive)

    await _cancel_once_entered(
        asyncio.create_task(
            upload(
                community_id=_COMMUNITY,
                server_id=server.id,
                content=_valid_tar_gz(),
            )
        ),
        archive.entered,
    )

    assert uow.backups.by_id == {}


async def test_upload_whose_row_cannot_be_recorded_deletes_its_archive() -> None:
    server = _at_rest()
    uow = _outage_uow(server)
    archive = _WritesThenTakesTheDatabaseDown(uow)

    with pytest.raises(DatabaseOutageError):
        await _upload_of(uow, archive)(
            community_id=_COMMUNITY, server_id=server.id, content=_valid_tar_gz()
        )

    assert uow.backups.by_id == {}
    assert archive.stored == [server.id]
    assert archive.archives == set()


# Once the row's COMMIT is sent, its outcome is unknown: a COMMIT can apply and
# still raise. Deleting the archive then could leave the row without it, so the
# archive is kept and a WARNING logged (review r1 of PR #4119).


class _CommitAnswerLost(FakeUnitOfWork):
    """The COMMIT applies, and its answer is lost with the connection."""

    async def commit(self) -> None:
        await super().commit()
        raise ConnectionError("connection lost after the COMMIT was sent")


class _HangsInCommit(FakeUnitOfWork):
    """The COMMIT applies, and its answer never arrives."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    async def commit(self) -> None:
        await super().commit()
        self.entered.set()
        await asyncio.Event().wait()


class _ExitFailsAfterCommit(FakeUnitOfWork):
    """The commit returns, and leaving its block (the rollback, the close) fails."""

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.commits:
            raise ConnectionError("connection lost while closing the session")


class _HangsInInsert(FakeBackupRepository):
    """The row's INSERT never returns, so its COMMIT is never sent."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    async def add(self, backup: Backup) -> None:
        self.entered.set()
        await asyncio.Event().wait()


async def _back_up(
    kind: str, uow: FakeUnitOfWork, archive: FakeBackupArchiveStore, server: Server
) -> Backup:
    if kind == "create":
        return await _make_create(uow, FakeControlPlane(), archive)(
            community_id=_COMMUNITY, server_id=server.id, source=BackupSource.MANUAL
        )
    return await _upload_of(uow, archive)(
        community_id=_COMMUNITY, server_id=server.id, content=_valid_tar_gz()
    )


_KINDS = pytest.mark.parametrize("kind", ["create", "upload"])


@_KINDS
async def test_a_backup_whose_commit_answer_is_lost_keeps_its_archive(
    kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    server = _at_rest()
    uow = _CommitAnswerLost()
    uow.servers.seed(server)
    archive = FakeBackupArchiveStore()

    with pytest.raises(ConnectionError):
        await _back_up(kind, uow, archive, server)

    (row,) = uow.backups.by_id.values()
    assert archive.archives == {row.storage_ref}
    (record,) = [r for r in caplog.records if r.name == backups_module.__name__]
    assert record.levelno == logging.WARNING
    assert [record.__dict__[k] for k in ("server_id", "storage_ref", "backup_id")] == [
        str(server.id.value),
        row.storage_ref,
        str(row.id.value),
    ]


@_KINDS
async def test_a_backup_cancelled_inside_its_commit_keeps_its_archive(
    kind: str,
) -> None:
    server = _at_rest()
    uow = _HangsInCommit()
    uow.servers.seed(server)
    archive = FakeBackupArchiveStore()

    await _cancel_once_entered(
        asyncio.create_task(_back_up(kind, uow, archive, server)), uow.entered
    )

    (row,) = uow.backups.by_id.values()
    assert archive.archives == {row.storage_ref}


@_KINDS
async def test_a_backup_whose_block_exit_fails_after_its_commit_keeps_its_archive(
    kind: str,
) -> None:
    server = _at_rest()
    uow = _ExitFailsAfterCommit()
    uow.servers.seed(server)
    archive = FakeBackupArchiveStore()

    with pytest.raises(ConnectionError):
        await _back_up(kind, uow, archive, server)

    (row,) = uow.backups.by_id.values()
    assert archive.archives == {row.storage_ref}


@_KINDS
async def test_a_backup_cancelled_before_its_commit_is_sent_deletes_its_archive(
    kind: str,
) -> None:
    server = _at_rest()
    backups = _HangsInInsert()
    uow = FakeUnitOfWork(backups=backups)
    uow.servers.seed(server)
    archive = FakeBackupArchiveStore()

    await _cancel_once_entered(
        asyncio.create_task(_back_up(kind, uow, archive, server)), backups.entered
    )

    assert backups.by_id == {}
    assert len(archive.deleted) == 1
    assert archive.archives == set()


# The delete of a cancelled backup's archive is shielded and bounded (issue
# #4109): a later cancellation stops only the wait, and a store that does not
# answer holds the backup, and the shutdown that cancelled it, no longer than
# the bound. After any other failure the delete is awaited as before #4109,
# with no bound. Every wait here is bounded too, so a regression fails in
# seconds rather than at the suite's timeout.

_WAIT_SECONDS = 5


async def _within_seconds(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), _WAIT_SECONDS)


class _DeleteWaitsForRelease(FakeBackupArchiveStore):
    """Its archive delete waits for ``release``; a base for the fakes below."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.deleting = asyncio.Event()
        self.release = asyncio.Event()
        self.done = asyncio.Event()

    async def delete(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        self.deleting.set()
        await self.release.wait()
        await super().delete(
            community_id=community_id, server_id=server_id, storage_ref=storage_ref
        )
        self.done.set()


class _DeleteWaitsAfterAnOutage(
    _DeleteWaitsForRelease, _WritesThenTakesTheDatabaseDown
):
    """Writes the archive, the database goes down, and the delete waits."""


def _backup_hanging_at_its_insert(
    kind: str, archive: FakeBackupArchiveStore
) -> tuple[asyncio.Task[Backup], asyncio.Event]:
    """A backup whose row's INSERT never answers, so its COMMIT is never sent."""

    server = _at_rest()
    backups = _HangsInInsert()
    uow = FakeUnitOfWork(backups=backups)
    uow.servers.seed(server)
    return asyncio.create_task(_back_up(kind, uow, archive, server)), backups.entered


def _backup_failing_after_its_write(
    kind: str,
) -> tuple[asyncio.Task[Backup], _DeleteWaitsAfterAnOutage]:
    server = _at_rest()
    uow = _outage_uow(server)
    archive = _DeleteWaitsAfterAnOutage(uow)
    return asyncio.create_task(_back_up(kind, uow, archive, server)), archive


def _backup_warnings(caplog: pytest.LogCaptureFixture) -> list[int]:
    return [r.levelno for r in caplog.records if r.name == backups_module.__name__]


@_KINDS
async def test_a_backup_cancelled_again_while_deleting_its_archive_deletes_it(
    kind: str,
) -> None:
    archive = _DeleteWaitsForRelease()
    backup, entered = _backup_hanging_at_its_insert(kind, archive)
    await _within_seconds(entered)
    backup.cancel()
    await _within_seconds(archive.deleting)

    backup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await backup
    archive.release.set()
    await _within_seconds(archive.done)

    assert archive.archives == set()


@_KINDS
async def test_a_cancelled_backup_whose_archive_delete_never_answers_ends_at_the_bound(
    kind: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(backups_module, "_DISCARD_TIMEOUT_SECONDS", 0.01)
    backup, entered = _backup_hanging_at_its_insert(kind, _DeleteWaitsForRelease())
    await _within_seconds(entered)

    backup.cancel()
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(_WAIT_SECONDS):
            await backup

    assert _backup_warnings(caplog) == [logging.WARNING]


@_KINDS
async def test_a_failed_backup_waits_for_its_archive_delete_past_the_bound(
    kind: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A slow store delays the failure's answer, as before issue #4109.
    monkeypatch.setattr(backups_module, "_DISCARD_TIMEOUT_SECONDS", 0.01)
    backup, archive = _backup_failing_after_its_write(kind)
    await _within_seconds(archive.deleting)
    # Twenty bounds.
    await asyncio.sleep(0.2)
    assert not backup.done()

    archive.release.set()
    with pytest.raises(DatabaseOutageError):
        async with asyncio.timeout(_WAIT_SECONDS):
            await backup

    assert archive.archives == set()
    assert _backup_warnings(caplog) == []


@_KINDS
async def test_a_failed_backup_cancelled_while_deleting_its_archive_ends_cancelled(
    kind: str,
) -> None:
    backup, archive = _backup_failing_after_its_write(kind)
    await _within_seconds(archive.deleting)

    backup.cancel()
    # The cancellation ends the backup, not the outage that began its cleanup.
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(_WAIT_SECONDS):
            await backup
