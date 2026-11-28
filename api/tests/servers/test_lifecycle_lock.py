"""Lock-adoption tests for the per-server lifecycle lock (issue #827, #1222).

The at-rest-gated use cases (RestoreBackup, the file mutations, UpdateServer,
DeleteServer, DeleteBackup, and the group file-sync helpers) check
``is_at_rest()`` in one transaction, mutate Storage over seconds-to-minutes, then
commit a second transaction. A start committed in that window operates on data
being mutated underneath it. These tests pin that each gated use case — and
StartServer's desired-state flip — takes the shared per-server
:class:`LifecycleLock` AROUND its work, so the lock serializes a start against a
gated operation.

The serialization itself (a start blocked until the gated op releases) is pinned
at the integration layer against a real PostgreSQL advisory lock in
``tests/integration/test_lifecycle_lock_concurrency.py``; here we use a recording
fake to assert the lock is taken at all and spans the right scope.
"""

from __future__ import annotations

import datetime as dt
import io
import tarfile
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from mc_server_dashboard_api.config import DatabaseSettings, Settings
from mc_server_dashboard_api.dependencies import (
    get_create_server,
    get_delete_server,
    get_import_server,
    get_restore_backup,
    get_upload_backup,
)
from mc_server_dashboard_api.fleet.adapters.real_time_events import (
    InProcessRealTimeEvents,
)
from mc_server_dashboard_api.servers.adapters.lifecycle_lock import PgLifecycleLock
from mc_server_dashboard_api.servers.application.backups import (
    CreateBackup,
    DeleteBackup,
    PruneScheduledBackups,
    RestoreBackup,
    UploadBackup,
)
from mc_server_dashboard_api.servers.application.files import (
    DeleteFile,
    MakeDir,
    RenameFile,
    RollbackFile,
    UploadFile,
    WriteFile,
)
from mc_server_dashboard_api.servers.application.groups import (
    AddPlayer,
    AttachGroup,
    DeleteGroup,
    DetachGroup,
    RemovePlayer,
)
from mc_server_dashboard_api.servers.application.lifecycle import StartServer
from mc_server_dashboard_api.servers.application.manage_server import (
    CreateServer,
    DeleteServer,
    UpdateServer,
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
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    BackupRestoreIncompleteError,
    LifecycleLockLostError,
    WorkingSetSeedFailedError,
)
from mc_server_dashboard_api.servers.domain.groups import (
    GroupId,
    GroupKind,
    GroupName,
    Player,
    PlayerGroup,
)
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)
from tests.audit.fakes import RecordingAuditRecorder
from tests.servers.fakes import (
    FakeBackupArchiveStore,
    FakeBackupRepository,
    FakeClock,
    FakeControlPlane,
    FakeFileStore,
    FakeJarProvisioner,
    FakeLifecycleLock,
    FakeServerRepository,
    FakeStoreGenerationReader,
    FakeUnitOfWork,
    FakeVersionValidator,
)

_NOW = dt.datetime(2026, 6, 11, 12, 0, tzinfo=dt.timezone.utc)
_COMMUNITY = CommunityId(uuid.uuid4())


def _assert_around(
    events: list[tuple[ServerId, str]], server_id: ServerId, *work: str
) -> None:
    """Assert each ``work`` event fell strictly inside this server's lock hold.

    The property these tests are NAMED for -- the gated op does its work AROUND
    the lock, not merely takes and drops it (issue #2546, extending #2515). It
    holds only when the collaborating fake's ``events`` list is aliased onto the
    FakeLifecycleLock's, so the acquire, the recorded work, and the release all
    land on ONE timeline; asserting ``events == [acquire, release]`` alone can
    never see it -- the work could run before acquire or after release and stay
    green. Uses the ``events.index(...)`` idiom of
    ``tests/storage/test_crash_safety.py``: a work event that moved outside the
    hold (or an ordering reversed) reddens here, where the endpoint-only assert
    could not.
    """

    acquire_at = events.index((server_id, "acquire"))
    release_at = events.index((server_id, "release"))
    for label in work:
        assert (server_id, label) in events, (label, events)
        at = events.index((server_id, label))
        assert acquire_at < at < release_at, (label, events)


def _assert_checked_right_before(
    events: list[tuple[ServerId, str]], server_id: ServerId, *work: str
) -> None:
    """Assert the held lock was confirmed immediately before each ``work`` event.

    The lifecycle lock can be lost while its holder runs (issue #3927), so a
    holder confirms it right before each write that relies on it: nothing else
    recorded on the timeline may come between the check and the write.
    """

    for label in work:
        at = events.index((server_id, label))
        assert events[at - 1] == (server_id, "check"), (label, events)


def _assert_checked_right_after(
    events: list[tuple[ServerId, str]], server_id: ServerId, label: str
) -> None:
    """Assert the held lock was confirmed immediately after the ``label`` event."""

    at = events.index((server_id, label))
    assert events[at + 1] == (server_id, "check"), (label, events)


def _at_rest() -> Server:
    return Server(
        id=ServerId(uuid.uuid4()),
        community_id=_COMMUNITY,
        name=ServerName("srv"),
        mc_edition="java",
        mc_version="1.21",
        server_type=ServerType.VANILLA,
        config={},
        game_port=25565,
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=_NOW,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _backup(server_id: ServerId) -> Backup:
    return Backup(
        id=BackupId.new(),
        server_id=server_id,
        storage_ref="ref",
        size_bytes=None,
        source=BackupSource.MANUAL,
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=_NOW,
    )


async def test_restore_takes_lock_around_its_work() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    backup = _backup(server.id)
    backups.seed(backup)
    archive = FakeBackupArchiveStore()
    archive.archives.add("ref")
    uow = FakeUnitOfWork(servers=repo, backups=backups)
    lock = FakeLifecycleLock()
    # One timeline for the lock and the archive republish (issue #2546).
    archive.events = lock.events

    await RestoreBackup(uow=uow, backup_store=archive, lifecycle_lock=lock)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )

    _assert_around(lock.events, server.id, "restore")


async def test_delete_server_takes_lock_around_its_work() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    uow = FakeUnitOfWork(servers=repo)
    archive = FakeBackupArchiveStore()
    lock = FakeLifecycleLock()
    # One timeline for the lock, the working-set pack, and the row delete.
    repo.events = lock.events
    archive.events = lock.events

    await DeleteServer(uow=uow, backup_store=archive, lifecycle_lock=lock)(
        community_id=_COMMUNITY, server_id=server.id
    )

    _assert_around(lock.events, server.id, "prune-final", "delete-server")


async def test_delete_backup_takes_lock_around_its_work() -> None:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    backup = _backup(server.id)
    backups.seed(backup)
    archive = FakeBackupArchiveStore()
    archive.archives.add("ref")
    uow = FakeUnitOfWork(servers=repo, backups=backups)
    lock = FakeLifecycleLock()
    # One timeline for the lock, the archive delete, and the row delete.
    archive.events = lock.events
    backups.events = lock.events

    await DeleteBackup(uow=uow, backup_store=archive, lifecycle_lock=lock)(
        community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
    )

    _assert_around(lock.events, server.id, "delete-archive", "delete-row")


async def test_start_takes_lock_around_its_flip() -> None:
    server = _at_rest()
    worker = uuid.uuid4()
    repo = FakeServerRepository()
    repo.seed(server)
    uow = FakeUnitOfWork(servers=repo)
    cp = FakeControlPlane(place_to=WorkerId(worker))
    lock = FakeLifecycleLock()
    # One timeline for the lock and the desired-state flip (issue #2546).
    repo.events = lock.events

    await StartServer(
        uow=uow,
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
        file_store=FakeFileStore(seed_eula=True),
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id)

    # The desired-state flip (the compare-and-set) happens strictly inside the
    # hold: the release fires once it has committed, before the post-commit
    # dispatch. A flip that ran outside the lock would still acquire/release.
    _assert_around(lock.events, server.id, "flip")


async def test_start_and_restore_take_the_same_keyed_lock() -> None:
    # Both the start and the restore must take the SAME per-server lock for the
    # serialization to hold; assert each records an acquire/release on the same
    # lock keyed by the server id. (The actual blocking — start waits until the
    # restore releases — is pinned against a real PostgreSQL advisory lock in
    # tests/integration/test_lifecycle_lock_concurrency.py, where the lock can
    # genuinely block across connections.)
    server = _at_rest()
    worker = uuid.uuid4()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    backup = _backup(server.id)
    backups.seed(backup)
    archive = FakeBackupArchiveStore()
    archive.archives.add("ref")
    lock = FakeLifecycleLock()

    await RestoreBackup(
        uow=FakeUnitOfWork(servers=repo, backups=backups),
        backup_store=archive,
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id)
    await StartServer(
        uow=FakeUnitOfWork(servers=repo),
        control_plane=FakeControlPlane(place_to=WorkerId(worker)),
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
        file_store=FakeFileStore(seed_eula=True),
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id)

    # The holds alone: the checks inside them are pinned below (issue #3927).
    assert [e for e in lock.events if e[1] != "check"] == [
        (server.id, "acquire"),
        (server.id, "release"),
        (server.id, "acquire"),
        (server.id, "release"),
    ]


async def test_prune_and_restore_take_the_same_keyed_lock() -> None:
    # The retention prune (issue #1841) must serialize with a restore of a
    # candidate backup: both take the SAME per-server lock, so a backup can
    # never be deleted while a restore of it is in flight. (The actual blocking
    # is pinned against a real PostgreSQL advisory lock in
    # tests/integration/test_lifecycle_lock_concurrency.py.)
    server = _at_rest()
    server.backup_retention = {"keep_last": 1}
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    backup = _backup(server.id)
    backups.seed(backup)
    archive = FakeBackupArchiveStore()
    archive.archives.add("ref")
    lock = FakeLifecycleLock()

    await RestoreBackup(
        uow=FakeUnitOfWork(servers=repo, backups=backups),
        backup_store=archive,
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id)
    await PruneScheduledBackups(
        uow=FakeUnitOfWork(servers=repo, backups=backups),
        backup_store=archive,
        audit=RecordingAuditRecorder(),
        clock=FakeClock(_NOW),
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id)

    # The holds alone: the checks inside them are pinned below (issue #3927).
    assert [e for e in lock.events if e[1] != "check"] == [
        (server.id, "acquire"),
        (server.id, "release"),
        (server.id, "acquire"),
        (server.id, "release"),
    ]


@pytest.mark.parametrize("force", [False, True])
async def test_restore_releases_lock_on_corrupt(force: bool) -> None:
    # The lock must release even when the gated op raises mid-flight.
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    backup = _backup(server.id)
    backups.seed(backup)
    archive = FakeBackupArchiveStore()
    archive.archives.add("ref")
    archive.corrupt_refs.add("ref")
    uow = FakeUnitOfWork(servers=repo, backups=backups)
    lock = FakeLifecycleLock()

    if force:
        await RestoreBackup(uow=uow, backup_store=archive, lifecycle_lock=lock)(
            community_id=_COMMUNITY,
            server_id=server.id,
            backup_id=backup.id,
            force=True,
        )
    else:
        from mc_server_dashboard_api.servers.domain.errors import BackupCorruptError

        with pytest.raises(BackupCorruptError):
            await RestoreBackup(uow=uow, backup_store=archive, lifecycle_lock=lock)(
                community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id
            )

    assert lock.events[-1] == (server.id, "release")


def _seeded() -> tuple[Server, FakeServerRepository, FakeLifecycleLock]:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    return server, repo, FakeLifecycleLock()


async def test_write_file_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.events = lock.events
    await WriteFile(
        uow=FakeUnitOfWork(servers=repo),
        control_plane=FakeControlPlane(),
        file_store=store,
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, rel_path="a.txt", content=b"x")

    _assert_around(lock.events, server.id, "write-file")
    _assert_checked_right_before(lock.events, server.id, "write-file")


async def test_rollback_file_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.events = lock.events
    await RollbackFile(
        uow=FakeUnitOfWork(servers=repo),
        file_store=store,
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, rel_path="a.txt", version_id="v1")

    _assert_around(lock.events, server.id, "rollback")
    _assert_checked_right_before(lock.events, server.id, "rollback")


async def test_upload_file_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.events = lock.events
    await UploadFile(
        uow=FakeUnitOfWork(servers=repo),
        file_store=store,
        lifecycle_lock=lock,
    )(
        community_id=_COMMUNITY,
        server_id=server.id,
        dir_path="",
        filename="a.txt",
        content=b"x",
        extract=False,
    )

    _assert_around(lock.events, server.id, "write-file")
    _assert_checked_right_before(lock.events, server.id, "write-file")


async def test_delete_file_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    # Seed a member under "d" so it IS a directory and the delete lands on
    # delete_dir; the mutation is what must fall inside the hold, whichever branch
    # dispatches it. (The fake's list_dir used to answer every path, so this
    # landed on delete_dir for a path that was nothing at all; issue #2867.)
    store.files["d/a.txt"] = b"x"
    store.events = lock.events
    await DeleteFile(
        uow=FakeUnitOfWork(servers=repo),
        file_store=store,
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, rel_path="d")

    _assert_around(lock.events, server.id, "delete-dir")
    _assert_checked_right_before(lock.events, server.id, "delete-dir")


async def test_make_dir_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.events = lock.events
    await MakeDir(
        uow=FakeUnitOfWork(servers=repo),
        file_store=store,
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, rel_path="d")

    _assert_around(lock.events, server.id, "make-dir")
    _assert_checked_right_before(lock.events, server.id, "make-dir")


async def test_rename_file_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.files["a.txt"] = b"x"
    store.events = lock.events
    # Rename onto itself: a no-op that only reads the source, so the lock scope is
    # what this asserts, not the move semantics. The work it reduces to is that
    # at-rest resolve read (list_dir via _path_is_dir, which misses on the seeded
    # FILE and confirms it from the stream), which must fall inside the hold or a
    # start could flip state under it.
    await RenameFile(
        uow=FakeUnitOfWork(servers=repo),
        file_store=store,
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, from_path="a.txt", to_path="a.txt")

    _assert_around(lock.events, server.id, "list-dir")


async def test_update_server_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    repo.events = lock.events

    async def _allow(_perm: str) -> bool:
        return True

    await UpdateServer(
        uow=FakeUnitOfWork(servers=repo),
        clock=FakeClock(_NOW),
        file_store=FakeFileStore(),
        port_range=PortRange(start=25565, end=25664),
        lifecycle_lock=lock,
    )(
        community_id=_COMMUNITY,
        server_id=server.id,
        name="renamed",
        authorize=_allow,
    )

    _assert_around(lock.events, server.id, "update-row")
    _assert_checked_right_before(lock.events, server.id, "update-row")


async def test_create_backup_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    archive = FakeBackupArchiveStore()
    archive.events = lock.events
    uow = FakeUnitOfWork(servers=repo, backups=FakeBackupRepository())
    await CreateBackup(
        uow=uow,
        backup_store=archive,
        snapshot_server=SnapshotServer(uow=uow, control_plane=FakeControlPlane()),
        clock=FakeClock(_NOW),
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, source=BackupSource.MANUAL)

    _assert_around(lock.events, server.id, "create-archive")


def _targz() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="server.properties")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"k=v"))
    return buf.getvalue()


async def test_upload_backup_takes_lock_around_its_work() -> None:
    # Issue #3219: the row commit and the archive publication run under the lock,
    # so a DeleteServer / DeleteBackup cannot remove the row between the two and
    # leave the publication writing into a deleted backup (or server).
    server, repo, lock = _seeded()
    archive = FakeBackupArchiveStore()
    archive.events = lock.events
    backups = FakeBackupRepository()
    backups.events = lock.events
    await UploadBackup(
        uow=FakeUnitOfWork(servers=repo, backups=backups),
        backup_store=archive,
        clock=FakeClock(_NOW),
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, content=_targz())

    _assert_around(lock.events, server.id, "add-row", "store-archive")


def test_upload_backup_factory_injects_the_shared_lifecycle_lock() -> None:
    # The production wiring (issue #3219): a NullLifecycleLock default here would
    # leave the upload unserialized against DeleteServer in the deployed app.
    engine = create_async_engine("postgresql+asyncpg://u:p@localhost/db")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=engine)))

    upload = get_upload_backup(request, FakeBackupArchiveStore())  # type: ignore[arg-type]

    assert isinstance(upload.lifecycle_lock, PgLifecycleLock)


def test_restore_and_server_delete_factories_inject_the_shared_lifecycle_lock() -> None:
    # The production wiring (issue #3904): the lifecycle lock is what keeps a
    # server delete's prune out of a restore's lock-free copy, so a
    # NullLifecycleLock on either side lets the restore flip onto a pruned prefix.
    engine = create_async_engine("postgresql+asyncpg://u:p@localhost/db")
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                engine=engine,
                settings=SimpleNamespace(server=SimpleNamespace(public_base_url=None)),
                worker_registry=None,
                control_plane=None,
                real_time_events=InProcessRealTimeEvents(),
            )
        )
    )

    restore = get_restore_backup(
        request,  # type: ignore[arg-type]
        FakeBackupArchiveStore(),
        FakeFileStore(),
    )
    delete = get_delete_server(request, FakeBackupArchiveStore())  # type: ignore[arg-type]

    assert isinstance(restore.lifecycle_lock, PgLifecycleLock)
    assert isinstance(delete.lifecycle_lock, PgLifecycleLock)


def _create_server(
    lock: FakeLifecycleLock, store: FakeFileStore, repo: FakeServerRepository
) -> CreateServer:
    return CreateServer(
        uow=FakeUnitOfWork(servers=repo),
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=store,
        port_range=PortRange(start=25565, end=25664),
        lifecycle_lock=lock,
    )


async def _create(create: CreateServer) -> Server:
    return await create(
        community_id=_COMMUNITY,
        name="srv",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        config={},
        accept_eula=True,
    )


async def test_create_server_takes_lock_around_its_row_and_seed() -> None:
    # Issue #4045: each seed write publishes on its own, so a start admitted
    # between the row's commit and the last write would hydrate the seed half
    # written. The lock is taken before the row is added, so the start waits for
    # the whole seed, and confirmed right before the row's commit and each seed
    # write (#3927).
    lock = FakeLifecycleLock()
    store = FakeFileStore()
    store.events = lock.events
    repo = FakeServerRepository()
    repo.events = lock.events

    server = await _create(_create_server(lock, store, repo))

    _assert_around(lock.events, server.id, "add-server", "write-file")
    _assert_checked_right_after(lock.events, server.id, "add-server")
    writes = [i for i, e in enumerate(lock.events) if e == (server.id, "write-file")]
    assert len(writes) == 2, lock.events
    assert writes[-1] < lock.events.index((server.id, "release")), lock.events
    assert all(lock.events[i - 1] == (server.id, "check") for i in writes), lock.events


async def test_create_server_that_lost_the_lock_before_its_commit_makes_no_row() -> (
    None
):
    # A lock lost before the row's commit refuses the create (issue #3927): a
    # start could take the lost lock and hydrate the unseeded row. Nothing is
    # committed, so the loss is the lock's own 503 and a retry can take the name.
    lock = FakeLifecycleLock()
    lock.checks_before_loss = 0
    store = FakeFileStore()
    repo = FakeServerRepository()
    uow = FakeUnitOfWork(servers=repo)

    with pytest.raises(LifecycleLockLostError):
        await _create(replace(_create_server(lock, store, repo), uow=uow))

    assert uow.commits == 0
    assert store.writes == []


async def test_create_server_that_lost_the_lock_writes_no_seed() -> None:
    # A lock lost after the row's commit stops the seed (issue #3927). The row
    # already stands, so the loss is the seed failure a storage error is.
    lock = FakeLifecycleLock()
    lock.checks_before_loss = 1
    store = FakeFileStore()
    repo = FakeServerRepository()

    with pytest.raises(WorkingSetSeedFailedError) as exc_info:
        await _create(_create_server(lock, store, repo))

    assert isinstance(exc_info.value.__cause__, LifecycleLockLostError)
    assert store.writes == []
    assert len(repo.by_id) == 1


def test_create_and_import_factories_inject_the_shared_lifecycle_lock() -> None:
    # The production wiring (issue #4045): a NullLifecycleLock default on the
    # create leaves a start free to hydrate a creation half-written, and import
    # runs inside the lock of the create it composes.
    engine = create_async_engine("postgresql+asyncpg://u:p@localhost/db")
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                engine=engine,
                settings=Settings(database=DatabaseSettings(url="postgresql://x/y")),
                real_time_events=InProcessRealTimeEvents(),
            )
        )
    )

    create = get_create_server(
        request,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        FakeFileStore(),
    )
    imp = get_import_server(request, create, FakeFileStore())  # type: ignore[arg-type]

    assert isinstance(create.lifecycle_lock, PgLifecycleLock)
    assert imp.create_server is create


# --- Group file-sync use cases (issue #1222) --------------------------------


def _seed_group(uow: FakeUnitOfWork, *, kind: GroupKind = GroupKind.OP) -> PlayerGroup:
    group = PlayerGroup(
        id=GroupId.new(),
        community_id=_COMMUNITY,
        name=GroupName("admins"),
        kind=kind,
        players=[Player(uuid.uuid4(), "alice")],
    )
    uow.groups.seed(group)
    return group


async def test_attach_group_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.events = lock.events
    uow = FakeUnitOfWork(servers=repo)
    group = _seed_group(uow)
    await AttachGroup(uow=uow, file_store=store, lifecycle_lock=lock)(
        community_id=_COMMUNITY, group_id=group.id, server_id=server.id
    )

    _assert_around(lock.events, server.id, "write-file")
    _assert_checked_right_before(lock.events, server.id, "write-file")


async def test_detach_group_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.events = lock.events
    uow = FakeUnitOfWork(servers=repo)
    group = _seed_group(uow)
    await uow.groups.attach(group.id, server.id)
    await DetachGroup(uow=uow, file_store=store, lifecycle_lock=lock)(
        community_id=_COMMUNITY, group_id=group.id, server_id=server.id
    )

    _assert_around(lock.events, server.id, "write-file")


async def test_add_player_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.events = lock.events
    uow = FakeUnitOfWork(servers=repo)
    group = _seed_group(uow)
    await uow.groups.attach(group.id, server.id)
    await AddPlayer(uow=uow, file_store=store, lifecycle_lock=lock)(
        community_id=_COMMUNITY,
        group_id=group.id,
        player_uuid=uuid.uuid4(),
        username="bob",
    )

    _assert_around(lock.events, server.id, "write-file")


async def test_remove_player_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.events = lock.events
    uow = FakeUnitOfWork(servers=repo)
    pid = uuid.uuid4()
    group = PlayerGroup(
        id=GroupId.new(),
        community_id=_COMMUNITY,
        name=GroupName("ops"),
        kind=GroupKind.OP,
        players=[Player(pid, "alice")],
    )
    uow.groups.seed(group)
    await uow.groups.attach(group.id, server.id)
    await RemovePlayer(uow=uow, file_store=store, lifecycle_lock=lock)(
        community_id=_COMMUNITY, group_id=group.id, player_uuid=pid
    )

    _assert_around(lock.events, server.id, "write-file")


async def test_delete_group_takes_lock_around_its_work() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.events = lock.events
    uow = FakeUnitOfWork(servers=repo)
    group = _seed_group(uow)
    await uow.groups.attach(group.id, server.id)
    await DeleteGroup(uow=uow, file_store=store, lifecycle_lock=lock)(
        community_id=_COMMUNITY, group_id=group.id
    )

    _assert_around(lock.events, server.id, "write-file")
    _assert_checked_right_before(lock.events, server.id, "write-file")


# --- A lost lock stops its holder before the guarded write (issue #3927) -----
#
# The lock lives on a database session, and the database releases it when that
# session ends without telling the holder. Each holder confirms it right before
# each write that relies on it; ``checks_before_loss`` loses it at a chosen check.


def _restore_case(
    *,
    file_store: FakeFileStore | None = None,
    server_type: ServerType = ServerType.VANILLA,
    uow: FakeUnitOfWork | None = None,
) -> tuple[Server, Backup, FakeBackupArchiveStore, RestoreBackup, FakeLifecycleLock]:
    server = _at_rest()
    server.server_type = server_type
    uow = uow or FakeUnitOfWork()
    uow.servers.seed(server)
    backup = _backup(server.id)
    uow.backups.seed(backup)
    archive = FakeBackupArchiveStore()
    archive.archives.add("ref")
    lock = FakeLifecycleLock()
    archive.events = lock.events
    restore = RestoreBackup(
        uow=uow,
        backup_store=archive,
        lifecycle_lock=lock,
        file_store=file_store,
    )
    return server, backup, archive, restore, lock


async def test_restore_confirms_the_lock_in_the_store_before_it_publishes() -> None:
    server, backup, archive, restore, lock = _restore_case()
    lock.checks_before_loss = 0

    with pytest.raises(LifecycleLockLostError):
        await restore(community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id)

    # The store's fence ran inside the restore call and refused the publish.
    assert lock.events.index((server.id, "restore")) < lock.events.index(
        (server.id, "check")
    )
    assert archive.restored == []


@pytest.mark.parametrize("verdict", [BackupHealth.QUARANTINED, BackupHealth.UNREADABLE])
async def test_a_refused_restore_that_lost_the_lock_records_no_verdict(
    verdict: BackupHealth,
) -> None:
    # The verdict is a write under the lock like the integrity sweep's, and is
    # confirmed the same way.
    uow = FakeUnitOfWork()
    server, backup, archive, restore, lock = _restore_case(uow=uow)
    if verdict is BackupHealth.QUARANTINED:
        archive.corrupt_refs.add("ref")
    else:
        archive.unreadable_refs.add("ref")
    lock.checks_before_loss = 0

    with pytest.raises(LifecycleLockLostError):
        await restore(community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id)

    assert uow.backups.by_id[backup.id].health is BackupHealth.HEALTHY


async def test_a_forced_corrupt_restore_that_lost_the_lock_records_no_quarantine() -> (
    None
):
    uow = FakeUnitOfWork()
    server, backup, archive, restore, lock = _restore_case(uow=uow)
    archive.corrupt_refs.add("ref")
    archive.corrupt_count = 2
    lock.checks_before_loss = 1  # the publish fence passes

    with pytest.raises(BackupRestoreIncompleteError) as exc_info:
        await restore(
            community_id=_COMMUNITY,
            server_id=server.id,
            backup_id=backup.id,
            force=True,
        )

    assert isinstance(exc_info.value.cause, LifecycleLockLostError)
    assert archive.restored == [(server.id, "ref")]
    assert uow.backups.by_id[backup.id].health is BackupHealth.HEALTHY


async def test_restore_that_loses_the_lock_after_publishing_writes_nothing_more() -> (
    None
):
    store = FakeFileStore()
    server, backup, archive, restore, lock = _restore_case(file_store=store)
    store.events = lock.events
    lock.checks_before_loss = 1  # the publish fence passes

    with pytest.raises(BackupRestoreIncompleteError) as exc_info:
        await restore(community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id)

    assert isinstance(exc_info.value.cause, LifecycleLockLostError)
    assert archive.restored == [(server.id, "ref")]
    # The server.properties re-apply is an in-place working-set write.
    assert (server.id, "write-file") not in lock.events


async def test_restore_checks_the_lock_again_before_committing_the_plugin_rows() -> (
    None
):
    # The reconcile lists the restored content directory and reads every jar in
    # it before its one commit, which can take long on a large modpack.
    def _case(
        uow: FakeUnitOfWork,
    ) -> tuple[Server, Backup, RestoreBackup, FakeLifecycleLock]:
        store = FakeFileStore()
        store.files["mods/ghost.jar"] = b"not-a-real-jar"
        server, backup, _archive, restore, lock = _restore_case(
            file_store=store, server_type=ServerType.FABRIC, uow=uow
        )
        return server, backup, restore, lock

    held_uow = FakeUnitOfWork()
    server, backup, restore, held_lock = _case(held_uow)
    held_uow.plugins.events = held_lock.events
    await restore(community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id)
    # After the jar reads, right before the commit: the ghost's row is staged
    # only once its jar was read.
    _assert_checked_right_after(held_lock.events, server.id, "add-plugin")
    lost_uow = FakeUnitOfWork()
    server, backup, restore, lock = _case(lost_uow)
    # The publish fence and the check before the properties re-apply pass.
    lock.checks_before_loss = 2

    with pytest.raises(BackupRestoreIncompleteError) as exc_info:
        await restore(community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id)

    assert isinstance(exc_info.value.cause, LifecycleLockLostError)
    assert lost_uow.commits == held_uow.commits - 1


def _delete_server_case() -> tuple[
    Server,
    FakeServerRepository,
    FakeBackupArchiveStore,
    DeleteServer,
    FakeLifecycleLock,
]:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    backups = FakeBackupRepository()
    backups.seed(_backup(server.id))
    archive = FakeBackupArchiveStore()
    archive.archives.update({"ref", "orphan-1", "orphan-2"})
    lock = FakeLifecycleLock()
    repo.events = lock.events
    archive.events = lock.events
    delete = DeleteServer(
        uow=FakeUnitOfWork(servers=repo, backups=backups),
        backup_store=archive,
        lifecycle_lock=lock,
    )
    return server, repo, archive, delete, lock


async def test_delete_server_confirms_the_lock_in_the_prune_and_before_each_write() -> (
    None
):
    server, _repo, _archive, delete, lock = _delete_server_case()

    await delete(community_id=_COMMUNITY, server_id=server.id)

    assert lock.events.index((server.id, "prune-final")) < lock.events.index(
        (server.id, "check")
    )
    # Each archive delete is confirmed on its own, as one may be slow.
    archive_deletes = [
        at
        for at, event in enumerate(lock.events)
        if event == (server.id, "delete-archive")
    ]
    assert len(archive_deletes) == 2
    for at in archive_deletes:
        assert lock.events[at - 1] == (server.id, "check"), lock.events
    _assert_checked_right_before(lock.events, server.id, "delete-server")


@pytest.mark.parametrize(
    ("checks_before_loss", "pruned", "archives_left"),
    [
        (0, False, {"ref", "orphan-1", "orphan-2"}),  # in the prune's fence
        (1, True, {"ref", "orphan-1", "orphan-2"}),  # before the first archive
        (2, True, {"ref", "orphan-2"}),  # before the second archive
        (3, True, {"ref"}),  # before the row goes
    ],
)
async def test_delete_server_that_loses_the_lock_stops_before_the_next_write(
    checks_before_loss: int, pruned: bool, archives_left: set[str]
) -> None:
    server, repo, archive, delete, lock = _delete_server_case()
    lock.checks_before_loss = checks_before_loss

    with pytest.raises(LifecycleLockLostError):
        await delete(community_id=_COMMUNITY, server_id=server.id)

    assert (archive.pruned == [server.id]) is pruned
    assert archive.archives == archives_left
    assert server.id in repo.by_id


async def test_delete_backup_that_lost_the_lock_deletes_nothing() -> None:
    server, repo, lock = _seeded()
    backups = FakeBackupRepository()
    backup = _backup(server.id)
    backups.seed(backup)
    archive = FakeBackupArchiveStore()
    archive.archives.add("ref")
    archive.events = lock.events
    backups.events = lock.events
    lock.checks_before_loss = 0

    with pytest.raises(LifecycleLockLostError):
        await DeleteBackup(
            uow=FakeUnitOfWork(servers=repo, backups=backups),
            backup_store=archive,
            lifecycle_lock=lock,
        )(community_id=_COMMUNITY, server_id=server.id, backup_id=backup.id)

    assert archive.archives == {"ref"}
    assert backup.id in backups.by_id


async def test_create_backup_that_lost_the_lock_writes_no_row_and_no_archive() -> None:
    server, repo, lock = _seeded()
    archive = FakeBackupArchiveStore()
    backups = FakeBackupRepository()
    archive.events = lock.events
    backups.events = lock.events
    uow = FakeUnitOfWork(servers=repo, backups=backups)
    lock.checks_before_loss = 0

    with pytest.raises(LifecycleLockLostError):
        await CreateBackup(
            uow=uow,
            backup_store=archive,
            snapshot_server=SnapshotServer(uow=uow, control_plane=FakeControlPlane()),
            clock=FakeClock(_NOW),
            lifecycle_lock=lock,
        )(community_id=_COMMUNITY, server_id=server.id, source=BackupSource.MANUAL)

    assert backups.by_id == {}
    assert (server.id, "create-archive") not in lock.events


async def test_create_backup_confirms_the_lock_right_before_its_archive_and_row() -> (
    None
):
    server, repo, lock = _seeded()
    archive = FakeBackupArchiveStore()
    backups = FakeBackupRepository()
    archive.events = lock.events
    backups.events = lock.events
    uow = FakeUnitOfWork(servers=repo, backups=backups)

    await CreateBackup(
        uow=uow,
        backup_store=archive,
        snapshot_server=SnapshotServer(uow=uow, control_plane=FakeControlPlane()),
        clock=FakeClock(_NOW),
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, source=BackupSource.MANUAL)

    # The archive write can take minutes, so the row behind it is checked again.
    _assert_checked_right_before(lock.events, server.id, "create-archive", "add-row")


async def test_create_backup_that_lost_the_lock_mid_archive_deletes_it() -> None:
    server, repo, lock = _seeded()
    archive = FakeBackupArchiveStore()
    backups = FakeBackupRepository()
    uow = FakeUnitOfWork(servers=repo, backups=backups)
    lock.checks_before_loss = 1

    with pytest.raises(LifecycleLockLostError):
        await CreateBackup(
            uow=uow,
            backup_store=archive,
            snapshot_server=SnapshotServer(uow=uow, control_plane=FakeControlPlane()),
            clock=FakeClock(_NOW),
            lifecycle_lock=lock,
        )(community_id=_COMMUNITY, server_id=server.id, source=BackupSource.MANUAL)

    assert backups.by_id == {}
    assert archive.created == [server.id]
    assert archive.archives == set()


async def test_upload_backup_that_lost_the_lock_writes_no_row_and_no_archive() -> None:
    server, repo, lock = _seeded()
    archive = FakeBackupArchiveStore()
    backups = FakeBackupRepository()
    archive.events = lock.events
    backups.events = lock.events
    lock.checks_before_loss = 0

    with pytest.raises(LifecycleLockLostError):
        await UploadBackup(
            uow=FakeUnitOfWork(servers=repo, backups=backups),
            backup_store=archive,
            clock=FakeClock(_NOW),
            lifecycle_lock=lock,
        )(community_id=_COMMUNITY, server_id=server.id, content=_targz())

    assert backups.by_id == {}
    assert (server.id, "store-archive") not in lock.events


async def test_upload_backup_confirms_the_lock_right_before_its_archive_and_row() -> (
    None
):
    server, repo, lock = _seeded()
    archive = FakeBackupArchiveStore()
    backups = FakeBackupRepository()
    archive.events = lock.events
    backups.events = lock.events

    await UploadBackup(
        uow=FakeUnitOfWork(servers=repo, backups=backups),
        backup_store=archive,
        clock=FakeClock(_NOW),
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, content=_targz())

    _assert_checked_right_before(lock.events, server.id, "store-archive", "add-row")


async def test_upload_backup_that_lost_the_lock_mid_store_deletes_it() -> None:
    server, repo, lock = _seeded()
    archive = FakeBackupArchiveStore()
    backups = FakeBackupRepository()
    lock.checks_before_loss = 1

    with pytest.raises(LifecycleLockLostError):
        await UploadBackup(
            uow=FakeUnitOfWork(servers=repo, backups=backups),
            backup_store=archive,
            clock=FakeClock(_NOW),
            lifecycle_lock=lock,
        )(community_id=_COMMUNITY, server_id=server.id, content=_targz())

    assert backups.by_id == {}
    assert archive.stored == [server.id]
    assert archive.archives == set()


async def _allow(_perm: str) -> bool:
    return True


async def test_update_server_that_lost_the_lock_commits_nothing() -> None:
    server, repo, lock = _seeded()
    repo.events = lock.events
    uow = FakeUnitOfWork(servers=repo)
    lock.checks_before_loss = 0

    with pytest.raises(LifecycleLockLostError):
        await UpdateServer(
            uow=uow,
            clock=FakeClock(_NOW),
            file_store=FakeFileStore(),
            port_range=PortRange(start=25565, end=25664),
            lifecycle_lock=lock,
        )(
            community_id=_COMMUNITY,
            server_id=server.id,
            name="renamed",
            authorize=_allow,
        )

    assert (server.id, "update-row") not in lock.events
    assert uow.commits == 0


async def test_update_server_confirms_the_lock_again_before_its_file_rewrite() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.files["server.properties"] = b"server-port=25565\n"
    repo.events = lock.events
    store.events = lock.events
    update = UpdateServer(
        uow=FakeUnitOfWork(servers=repo),
        clock=FakeClock(_NOW),
        file_store=store,
        port_range=PortRange(start=25565, end=25664),
        lifecycle_lock=lock,
    )

    await update(
        community_id=_COMMUNITY, server_id=server.id, game_port=25566, authorize=_allow
    )
    _assert_checked_right_before(lock.events, server.id, "update-row")
    rewrite = lock.events.index((server.id, "write-file"))
    assert (server.id, "check") in lock.events[
        lock.events.index((server.id, "update-row")) + 1 : rewrite
    ]

    lock.events.clear()
    lock.checks_before_loss = 1
    # The row is committed by then, so the loss is the post-commit seed failure
    # a failed rewrite is (503 seed_failed, audited as the change it made).
    with pytest.raises(WorkingSetSeedFailedError) as exc_info:
        await update(
            community_id=_COMMUNITY,
            server_id=server.id,
            game_port=25567,
            authorize=_allow,
        )
    assert isinstance(exc_info.value.__cause__, LifecycleLockLostError)
    assert (server.id, "update-row") in lock.events
    assert (server.id, "write-file") not in lock.events
    assert store.files["server.properties"] == b"server-port=25566\n"


def _start(
    server: Server, repo: FakeServerRepository, lock: FakeLifecycleLock
) -> tuple[StartServer, FakeFileStore, FakeControlPlane]:
    store = FakeFileStore()
    repo.events = lock.events
    store.events = lock.events
    cp = FakeControlPlane(place_to=WorkerId(uuid.uuid4()))
    start = StartServer(
        uow=FakeUnitOfWork(servers=repo),
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
        file_store=store,
        lifecycle_lock=lock,
    )
    return start, store, cp


async def test_start_confirms_the_lock_before_its_file_writes_and_its_flip() -> None:
    server, repo, lock = _seeded()
    start, _store, _cp = _start(server, repo, lock)

    await start(community_id=_COMMUNITY, server_id=server.id, accept_eula=True)

    _assert_checked_right_before(lock.events, server.id, "write-file", "flip")


@pytest.mark.parametrize(
    ("checks_before_loss", "wrote_eula"),
    [(0, False), (1, True)],
)
async def test_start_that_loses_the_lock_does_not_flip(
    checks_before_loss: int, wrote_eula: bool
) -> None:
    server, repo, lock = _seeded()
    start, store, cp = _start(server, repo, lock)
    lock.checks_before_loss = checks_before_loss

    with pytest.raises(LifecycleLockLostError):
        await start(community_id=_COMMUNITY, server_id=server.id, accept_eula=True)

    assert ("eula.txt" in store.files) is wrote_eula
    assert (server.id, "flip") not in lock.events
    assert repo.by_id[server.id].desired_state is DesiredState.STOPPED
    # A placement the flip never committed gives its reservation back.
    assert cp.released == cp.reserved


def _orphan() -> Server:
    server = _at_rest()
    server.desired_state = DesiredState.RUNNING
    return server


async def test_place_and_start_confirms_the_lock_before_its_flip() -> None:
    server = _orphan()
    repo = FakeServerRepository()
    repo.seed(server)
    lock = FakeLifecycleLock()
    start, _store, _cp = _start(server, repo, lock)

    await start.place_and_start(community_id=_COMMUNITY, server_id=server.id)

    _assert_checked_right_before(lock.events, server.id, "flip")


@pytest.mark.parametrize("checks_before_loss", [0, 1])
async def test_place_and_start_that_loses_the_lock_does_not_assign(
    checks_before_loss: int,
) -> None:
    server = _orphan()
    repo = FakeServerRepository()
    repo.seed(server)
    lock = FakeLifecycleLock()
    start, _store, cp = _start(server, repo, lock)
    lock.checks_before_loss = checks_before_loss

    with pytest.raises(LifecycleLockLostError):
        await start.place_and_start(community_id=_COMMUNITY, server_id=server.id)

    assert (server.id, "flip") not in lock.events
    assert repo.by_id[server.id].assigned_worker_id is None
    assert cp.released == cp.reserved


async def test_file_writes_that_lost_the_lock_change_nothing() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.files["a.txt"] = b"old"
    store.files["d/b.txt"] = b"old"
    store.events = lock.events
    uow = FakeUnitOfWork(servers=repo)
    lock.checks_before_loss = 0
    before = dict(store.files)

    writes: list[Callable[[], Awaitable[None]]] = [
        lambda: WriteFile(
            uow=uow,
            control_plane=FakeControlPlane(),
            file_store=store,
            lifecycle_lock=lock,
        )(community_id=_COMMUNITY, server_id=server.id, rel_path="a.txt", content=b"x"),
        lambda: RollbackFile(uow=uow, file_store=store, lifecycle_lock=lock)(
            community_id=_COMMUNITY,
            server_id=server.id,
            rel_path="a.txt",
            version_id="v1",
        ),
        lambda: UploadFile(uow=uow, file_store=store, lifecycle_lock=lock)(
            community_id=_COMMUNITY,
            server_id=server.id,
            dir_path="",
            filename="c.txt",
            content=b"x",
            extract=False,
        ),
        lambda: DeleteFile(uow=uow, file_store=store, lifecycle_lock=lock)(
            community_id=_COMMUNITY, server_id=server.id, rel_path="a.txt"
        ),
        lambda: DeleteFile(uow=uow, file_store=store, lifecycle_lock=lock)(
            community_id=_COMMUNITY, server_id=server.id, rel_path="d"
        ),
        lambda: MakeDir(uow=uow, file_store=store, lifecycle_lock=lock)(
            community_id=_COMMUNITY, server_id=server.id, rel_path="e"
        ),
        lambda: RenameFile(uow=uow, file_store=store, lifecycle_lock=lock)(
            community_id=_COMMUNITY,
            server_id=server.id,
            from_path="a.txt",
            to_path="f.txt",
        ),
        lambda: RenameFile(uow=uow, file_store=store, lifecycle_lock=lock)(
            community_id=_COMMUNITY, server_id=server.id, from_path="d", to_path="g"
        ),
    ]
    for write in writes:
        with pytest.raises(LifecycleLockLostError):
            await write()

    assert store.files == before
    assert not {
        "write-file",
        "rollback",
        "delete-file",
        "delete-dir",
        "make-dir",
        "rename-file",
        "rename-dir",
    } & {label for _, label in lock.events}


async def test_rename_checks_the_lock_right_before_it_moves() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.files["a.txt"] = b"x"
    store.files["d/b.txt"] = b"x"
    store.events = lock.events
    rename = RenameFile(
        uow=FakeUnitOfWork(servers=repo), file_store=store, lifecycle_lock=lock
    )

    await rename(
        community_id=_COMMUNITY, server_id=server.id, from_path="a.txt", to_path="f.txt"
    )
    await rename(
        community_id=_COMMUNITY, server_id=server.id, from_path="d", to_path="g"
    )

    _assert_checked_right_before(lock.events, server.id, "rename-file", "rename-dir")


async def test_delete_file_checks_the_lock_right_before_it_deletes() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.files["a.txt"] = b"x"
    store.events = lock.events

    await DeleteFile(
        uow=FakeUnitOfWork(servers=repo), file_store=store, lifecycle_lock=lock
    )(community_id=_COMMUNITY, server_id=server.id, rel_path="a.txt")

    _assert_checked_right_before(lock.events, server.id, "delete-file")


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


async def test_extracting_upload_checks_the_lock_before_each_member() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.events = lock.events
    upload = UploadFile(
        uow=FakeUnitOfWork(servers=repo), file_store=store, lifecycle_lock=lock
    )
    lock.checks_before_loss = 1

    with pytest.raises(LifecycleLockLostError):
        await upload(
            community_id=_COMMUNITY,
            server_id=server.id,
            dir_path="",
            filename="a.zip",
            content=_zip({"a.txt": b"a", "b.txt": b"b"}),
            extract=True,
        )

    assert store.files == {"a.txt": b"a"}
    _assert_checked_right_before(lock.events, server.id, "write-file")


async def test_group_sync_that_lost_the_lock_writes_nothing() -> None:
    server, repo, lock = _seeded()
    store = FakeFileStore()
    store.events = lock.events
    uow = FakeUnitOfWork(servers=repo)
    group = _seed_group(uow)
    lock.checks_before_loss = 0

    with pytest.raises(LifecycleLockLostError):
        await AttachGroup(uow=uow, file_store=store, lifecycle_lock=lock)(
            community_id=_COMMUNITY, group_id=group.id, server_id=server.id
        )

    assert (server.id, "write-file") not in lock.events
