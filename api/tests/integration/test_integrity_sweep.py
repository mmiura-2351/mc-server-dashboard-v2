"""End-to-end integrity sweep against PostgreSQL + a real ``FsStorage`` (#744).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service); skipped
otherwise (TESTING.md Section 5). The fast unit tests
(``tests/servers/test_integrity_sweep.py``) cover the sweep logic against fakes;
this integration test proves the whole pass end to end — the real
``update_health`` writing the ``health`` column, the real extract-and-fsck of
backup archives, and the real ``current`` snapshot fsck — so the DB column
genuinely reflects the on-disk verdict.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.audit.adapters.clock import SystemClock as AuditSystemClock
from mc_server_dashboard_api.audit.adapters.recorder import LoggingAuditRecorder
from mc_server_dashboard_api.audit.adapters.writer import SqlAlchemyAuditWriter
from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as CommunityUnitOfWork,
)
from mc_server_dashboard_api.community.domain.entities import Community
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId as CommunityCommunityId,
)
from mc_server_dashboard_api.community.domain.value_objects import CommunityName
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.servers.adapters import (
    lifecycle_lock as lifecycle_lock_mod,
)
from mc_server_dashboard_api.servers.adapters.backup_store import (
    StorageBackupStoreAdapter,
)
from mc_server_dashboard_api.servers.adapters.lifecycle_lock import PgLifecycleLock
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.backups import CreateBackup
from mc_server_dashboard_api.servers.application.integrity_sweep import IntegritySweep
from mc_server_dashboard_api.servers.application.manage_server import CreateServer
from mc_server_dashboard_api.servers.application.snapshot_scheduler import (
    SnapshotServer,
)
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from tests.audit.fakes import RecordingAuditRecorder
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.fakes import (
    FakeClock,
    FakeControlPlane,
    FakeFileStore,
    FakeVersionValidator,
)
from tests.storage.helpers import (
    healthy_region_bytes,
    mode_invariant_corrupt_region_bytes,
    region_targz,
    tar_stream,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 9, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    eng = create_async_engine(_DB_URL)
    try:
        yield eng
    finally:
        await eng.dispose()
        await downgrade_base(_DB_URL)


async def _seed_server(engine: AsyncEngine) -> tuple[CommunityId, ServerId]:
    community = Community(
        id=CommunityCommunityId(uuid.uuid4()),
        name=CommunityName("guild"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    factory = create_session_factory(engine)
    async with CommunityUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.commit()
    server = await CreateServer(
        uow=ServersUnitOfWork(factory),
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=PortRange(start=25565, end=25664),
    )(
        community_id=CommunityId(community.id.value),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        config={},
    )
    return CommunityId(community.id.value), server.id


async def _publish(
    storage: FsStorage,
    community: CommunityId,
    server: ServerId,
    files: dict[str, bytes],
) -> None:
    handle = await storage.begin_snapshot(
        StorageCommunityId(community.value), StorageServerId(server.value)
    )
    await storage.write_snapshot(handle, tar_stream(files))
    await storage.commit_snapshot(handle)


async def _put_backup(
    storage: FsStorage,
    community: CommunityId,
    server: ServerId,
    files: dict[str, bytes],
) -> str:
    async def _stream() -> AsyncIterator[bytes]:
        yield region_targz(files)

    key = await storage.put_backup(
        StorageCommunityId(community.value), StorageServerId(server.value), _stream()
    )
    return key.value


def _backup(server_id: ServerId, ref: str) -> Backup:
    return Backup(
        id=BackupId.new(),
        server_id=server_id,
        storage_ref=ref,
        size_bytes=None,
        # The sweep targets legacy/uploaded UNKNOWN-health rows; ``source`` is
        # irrelevant to it (it reads ``storage_ref`` and writes ``health`` only), so
        # MANUAL is used to match the documented downgrade-safe enum (the
        # ``ck_backup_source`` check the test teardown re-applies, mirroring the
        # sibling backup-repository integration tests).
        source=BackupSource.MANUAL,
        health=BackupHealth.UNKNOWN,
        created_by=None,
        created_at=_NOW,
    )


async def test_sweep_classifies_backups_and_flags_a_corrupt_snapshot(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    community, server = await _seed_server(engine)
    factory = create_session_factory(engine)
    storage = FsStorage(tmp_path)

    # Publish a healthy snapshot, then tamper it in place (a published-then-torn
    # world that predates the create gate).
    await _publish(
        storage, community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )
    server_root = (
        tmp_path / "communities" / str(community.value) / "servers" / str(server.value)
    )
    live = server_root / os.readlink(server_root / "current")
    (live / "world" / "region" / "r.0.0.mca").write_bytes(
        mode_invariant_corrupt_region_bytes()
    )

    good_ref = await _put_backup(
        storage, community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )
    bad_ref = await _put_backup(
        storage,
        community,
        server,
        {"world/region/r.0.0.mca": mode_invariant_corrupt_region_bytes()},
    )
    healthy_row = _backup(server, good_ref)
    corrupt_row = _backup(server, bad_ref)
    async with ServersUnitOfWork(factory) as uow:
        await uow.backups.add(healthy_row)
        await uow.backups.add(corrupt_row)
        await uow.commit()

    sweep = IntegritySweep(
        uow=ServersUnitOfWork(factory),
        backup_store=StorageBackupStoreAdapter(storage=storage),
        audit=LoggingAuditRecorder(
            SqlAlchemyAuditWriter(factory, clock=AuditSystemClock())
        ),
    )
    summary = await sweep()

    async with ServersUnitOfWork(factory) as uow:
        fetched_good = await uow.backups.get_by_id(healthy_row.id)
        fetched_bad = await uow.backups.get_by_id(corrupt_row.id)
    assert fetched_good is not None and fetched_good.health is BackupHealth.HEALTHY
    assert fetched_bad is not None and fetched_bad.health is BackupHealth.QUARANTINED
    assert summary.backups_healthy == 1
    assert summary.backups_quarantined == 1
    assert summary.snapshots_scanned == 1
    assert summary.snapshots_flagged == 1


async def test_sweep_is_idempotent_on_the_health_column(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    community, server = await _seed_server(engine)
    factory = create_session_factory(engine)
    storage = FsStorage(tmp_path)
    bad_ref = await _put_backup(
        storage,
        community,
        server,
        {"world/region/r.0.0.mca": mode_invariant_corrupt_region_bytes()},
    )
    corrupt_row = _backup(server, bad_ref)
    async with ServersUnitOfWork(factory) as uow:
        await uow.backups.add(corrupt_row)
        await uow.commit()

    sweep = IntegritySweep(
        uow=ServersUnitOfWork(factory),
        backup_store=StorageBackupStoreAdapter(storage=storage),
        audit=LoggingAuditRecorder(
            SqlAlchemyAuditWriter(factory, clock=AuditSystemClock())
        ),
    )
    first = await sweep()
    second = await sweep()

    async with ServersUnitOfWork(factory) as uow:
        fetched = await uow.backups.get_by_id(corrupt_row.id)
    assert fetched is not None and fetched.health is BackupHealth.QUARANTINED
    assert (first.backups_quarantined, first.backups_healthy) == (
        second.backups_quarantined,
        second.backups_healthy,
    )


async def test_sweep_persists_unreadable_for_a_row_with_no_archive(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """A row whose archive is gone is written ``unreadable`` through the real
    ``ck_backup_health`` CHECK (issue #2374), not folded into ``quarantined``."""

    community, server = await _seed_server(engine)
    factory = create_session_factory(engine)
    dangling_row = _backup(server, "no-such-archive")
    async with ServersUnitOfWork(factory) as uow:
        await uow.backups.add(dangling_row)
        await uow.commit()

    sweep = IntegritySweep(
        uow=ServersUnitOfWork(factory),
        backup_store=StorageBackupStoreAdapter(storage=FsStorage(tmp_path)),
        audit=LoggingAuditRecorder(
            SqlAlchemyAuditWriter(factory, clock=AuditSystemClock())
        ),
    )
    summary = await sweep()

    async with ServersUnitOfWork(factory) as uow:
        fetched = await uow.backups.get_by_id(dangling_row.id)
    assert fetched is not None and fetched.health is BackupHealth.UNREADABLE
    assert summary.backups_dangling == 1


class _GatedArchiveStore(StorageBackupStoreAdapter):
    """Parks ``CreateBackup`` after its metadata commit, before the archive write.

    That is the publication window of issue #3220: the ``healthy`` row is visible
    while its archive does not exist yet.
    """

    def __init__(self, *, storage: FsStorage) -> None:
        super().__init__(storage=storage)
        self.row_committed = asyncio.Event()
        self.release = asyncio.Event()

    async def create_from_current(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        self.row_committed.set()
        await self.release.wait()
        await super().create_from_current(
            community_id=community_id, server_id=server_id, storage_ref=storage_ref
        )


async def test_sweep_waits_out_a_backup_publication_instead_of_condemning_it(
    engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep that starts inside CreateBackup's row-before-archive window must
    not judge the pending archive missing (issue #3220). Both take the server's
    real advisory lock, so the sweep waits for the publication and then finds the
    archive sound: the persisted health stays ``healthy``, agreeing with the
    Backup the creation returned, and no condemnation audit is emitted."""

    # Pin the ordering, not the wait budget: a slow host must not turn the waiting
    # sweep into a busy skip (which has its own unit test).
    monkeypatch.setattr(lifecycle_lock_mod, "_ACQUIRE_BUDGET_SECONDS", 600.0)
    community, server = await _seed_server(engine)
    factory = create_session_factory(engine)
    storage = FsStorage(tmp_path)
    await _publish(
        storage, community, server, {"world/region/r.0.0.mca": healthy_region_bytes()}
    )
    lock = PgLifecycleLock(engine=engine)
    gated = _GatedArchiveStore(storage=storage)
    create = CreateBackup(
        uow=ServersUnitOfWork(factory),
        backup_store=gated,
        snapshot_server=SnapshotServer(
            uow=ServersUnitOfWork(factory), control_plane=FakeControlPlane()
        ),
        clock=FakeClock(_NOW),
        lifecycle_lock=lock,
    )
    audit = RecordingAuditRecorder()
    sweep = IntegritySweep(
        uow=ServersUnitOfWork(factory),
        backup_store=StorageBackupStoreAdapter(storage=storage),
        audit=audit,
        lifecycle_lock=lock,
    )

    create_task = asyncio.create_task(
        create(community_id=community, server_id=server, source=BackupSource.MANUAL)
    )
    await gated.row_committed.wait()
    sweep_task = asyncio.create_task(sweep(server_id=server))
    # The sweep must be waiting on the lock, not judging the row. Load only
    # lengthens the wait, so this window cannot turn red on a slow host.
    await asyncio.sleep(0.5)
    assert not sweep_task.done()

    gated.release.set()
    created = await create_task
    summary = await sweep_task

    async with ServersUnitOfWork(factory) as uow:
        persisted = await uow.backups.get_by_id(created.id)
    assert created.health is BackupHealth.HEALTHY
    assert persisted is not None and persisted.health is BackupHealth.HEALTHY
    assert summary.backups_healthy == 1
    assert summary.backups_dangling == 0
    assert audit.events == []
