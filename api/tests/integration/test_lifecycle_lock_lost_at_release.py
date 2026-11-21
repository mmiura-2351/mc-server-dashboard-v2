"""An operation whose lock session ends only after its last check finishes (#3927).

Every write the operation made ran after a check that passed, so a loss that only
the release finds is logged, not raised: the operation answers as finished and
runs what follows its hold. The lock is the real :class:`PgLifecycleLock`; its
session is ended with ``pg_terminate_backend`` once the body returns and before
the release. Everything else is the in-memory fakes.

DB-gated: skipped unless ``MCD_TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import os
import tarfile
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.servers.adapters.lifecycle_lock import PgLifecycleLock
from mc_server_dashboard_api.servers.application.backups import (
    CreateBackup,
    UploadBackup,
)
from mc_server_dashboard_api.servers.application.export_import import ImportServer
from mc_server_dashboard_api.servers.application.lifecycle import StartServer
from mc_server_dashboard_api.servers.application.manage_server import DeleteServer
from mc_server_dashboard_api.servers.application.snapshot_scheduler import (
    SnapshotServer,
)
from mc_server_dashboard_api.servers.domain.backup import BackupSource
from mc_server_dashboard_api.servers.domain.deleted_server_reclaim import (
    DeletedServerReclaim,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.lifecycle_lock import (
    HeldLifecycleLock,
    LifecycleLock,
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
from tests.integration.test_lifecycle_lock_lost_session import terminate_lock_session
from tests.servers.fakes import (
    FakeBackupArchiveStore,
    FakeBackupRepository,
    FakeClock,
    FakeControlPlane,
    FakeFileStore,
    FakeJarProvisioner,
    FakeServerRepository,
    FakeStoreGenerationReader,
    FakeUnitOfWork,
)
from tests.servers.test_export_import import (
    _BEDROCK_RANGE,
    _create_server,
    _export_archive,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 11, 12, 0, tzinfo=dt.timezone.utc)
_COMMUNITY = CommunityId(uuid.uuid4())


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    eng = create_async_engine(_DB_URL)
    try:
        yield eng
    finally:
        await eng.dispose()


class _SessionEndsBeforeRelease(LifecycleLock):
    """The real lock, whose session ends once the body returns, before release."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._inner = PgLifecycleLock(engine=engine)
        self.ended = 0

    @contextlib.asynccontextmanager
    async def hold(self, server_id: ServerId) -> AsyncIterator[HeldLifecycleLock]:
        async with self._inner.hold(server_id) as held:
            yield held
            await terminate_lock_session(self._engine, server_id)
            self.ended += 1


class _RecordingReclaim(DeletedServerReclaim):
    def __init__(self) -> None:
        self.reclaimed: list[ServerId] = []

    async def reclaim(self, server_id: ServerId) -> None:
        self.reclaimed.append(server_id)


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


def _seeded() -> tuple[Server, FakeServerRepository]:
    server = _at_rest()
    repo = FakeServerRepository()
    repo.seed(server)
    return server, repo


async def test_a_start_launches_although_its_session_ended_after_the_flip(
    engine: AsyncEngine,
) -> None:
    # The flip committed under a check that passed; the launch after the hold
    # still runs, so the start is not left running-but-never-sent.
    server, repo = _seeded()
    cp = FakeControlPlane(place_to=WorkerId(uuid.uuid4()))
    lock = _SessionEndsBeforeRelease(engine)

    started = await StartServer(
        uow=FakeUnitOfWork(servers=repo),
        control_plane=cp,
        clock=FakeClock(_NOW),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
        file_store=FakeFileStore(seed_eula=True),
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id)

    assert lock.ended == 1
    assert started.desired_state is DesiredState.RUNNING
    assert [kind for kind, _, _ in cp.dispatched] == ["hydrate", "start"]


async def test_a_backup_is_returned_although_its_session_ended_after_the_archive(
    engine: AsyncEngine,
) -> None:
    # A 503 here would make a retry write a second backup.
    server, repo = _seeded()
    archive = FakeBackupArchiveStore()
    backups = FakeBackupRepository()
    uow = FakeUnitOfWork(servers=repo, backups=backups)
    lock = _SessionEndsBeforeRelease(engine)

    backup = await CreateBackup(
        uow=uow,
        backup_store=archive,
        snapshot_server=SnapshotServer(uow=uow, control_plane=FakeControlPlane()),
        clock=FakeClock(_NOW),
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, source=BackupSource.MANUAL)

    assert lock.ended == 1
    assert list(backups.by_id) == [backup.id]
    assert archive.archives == {backup.storage_ref}


async def test_an_upload_is_returned_although_its_session_ended_after_the_archive(
    engine: AsyncEngine,
) -> None:
    server, repo = _seeded()
    archive = FakeBackupArchiveStore()
    backups = FakeBackupRepository()
    lock = _SessionEndsBeforeRelease(engine)

    backup = await UploadBackup(
        uow=FakeUnitOfWork(servers=repo, backups=backups),
        backup_store=archive,
        clock=FakeClock(_NOW),
        lifecycle_lock=lock,
    )(community_id=_COMMUNITY, server_id=server.id, content=_targz())

    assert lock.ended == 1
    assert list(backups.by_id) == [backup.id]
    assert archive.archives == {backup.storage_ref}


async def test_a_delete_reclaims_although_its_session_ended_after_the_row(
    engine: AsyncEngine,
) -> None:
    # The row delete committed; what follows the hold still runs.
    server, repo = _seeded()
    reclaim = _RecordingReclaim()
    lock = _SessionEndsBeforeRelease(engine)

    await DeleteServer(
        uow=FakeUnitOfWork(servers=repo),
        backup_store=FakeBackupArchiveStore(),
        lifecycle_lock=lock,
        deleted_server_reclaim=reclaim,
    )(community_id=_COMMUNITY, server_id=server.id)

    assert lock.ended == 1
    assert server.id not in repo.by_id
    assert reclaim.reclaimed == [server.id]


async def test_an_import_is_returned_although_its_session_ended_after_the_plugins(
    engine: AsyncEngine,
) -> None:
    community = uuid.uuid4()
    archive = await _export_archive(community=community, mod_identifier="Geyser-Spigot")
    uow = FakeUnitOfWork()
    store = FakeFileStore()
    lock = _SessionEndsBeforeRelease(engine)

    server = await ImportServer(
        create_server=_create_server(uow, store),
        file_store=store,
        bedrock_port_range=_BEDROCK_RANGE,
        lifecycle_lock=lock,
    )(community_id=CommunityId(community), name="imported", content=archive)

    assert lock.ended == 1
    assert [p.server_id for p in uow.plugins.by_id.values()] == [server.id]


def _targz() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="server.properties")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"k=v"))
    return buf.getvalue()
