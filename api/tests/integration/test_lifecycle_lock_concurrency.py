"""Concurrency test for the per-server lifecycle lock (issue #827).

Proves the PostgreSQL session-level advisory lock in :class:`PgLifecycleLock`
serializes a start against an at-rest-gated operation: while a restore holds the
lock and mutates Storage, a concurrent start blocks on the SAME lock until the
restore releases — it cannot flip ``desired=running`` into the middle of the
republish. Without the lock the start's compare-and-set would win the window and
the server would come up on data being mutated underneath it.

The restore's Storage step is replaced by a controllable fake whose ``restore``
parks on an event, so the test can hold the lock open across a deterministic
window and observe the start blocked, then released. The lock itself is the REAL
adapter over a real PostgreSQL connection (a session advisory lock genuinely
blocks across connections, which an in-memory fake cannot model).

DB-gated (TESTING.md Section 5): runs only when ``MCD_TEST_DATABASE_URL`` is set
(the CI Postgres service), skipped otherwise.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as CommunityUnitOfWork,
)
from mc_server_dashboard_api.community.domain.entities import Community
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId as CommunityCommunityId,
)
from mc_server_dashboard_api.community.domain.value_objects import CommunityName
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.servers.adapters.lifecycle_lock import PgLifecycleLock
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.backups import RestoreBackup
from mc_server_dashboard_api.servers.application.lifecycle import StartServer
from mc_server_dashboard_api.servers.application.manage_server import CreateServer
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ServerId,
    WorkerId,
)
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.fakes import (
    FakeBackupArchiveStore,
    FakeControlPlane,
    FakeFileStore,
    FakeJarProvisioner,
    FakeStoreGenerationReader,
    FakeVersionValidator,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 11, 12, 0, tzinfo=dt.timezone.utc)


class _FixedClock(Clock):
    def now(self) -> dt.datetime:
        return _NOW


class _BlockingBackupStore(FakeBackupArchiveStore):
    """A backup store whose ``restore`` parks until the test releases it.

    Lets the test hold the lifecycle lock open across a deterministic window (the
    restore body) so a concurrent start is observably blocked, then released.
    """

    def __init__(self) -> None:
        super().__init__()
        self.archives.add("ref")
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def restore(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        storage_ref: str,
        force: bool = False,
    ) -> int:
        self.entered.set()
        await self.release.wait()
        return await super().restore(
            community_id=community_id,
            server_id=server_id,
            storage_ref=storage_ref,
            force=force,
        )


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


async def _seed_community(engine: AsyncEngine) -> uuid.UUID:
    community = Community(
        id=CommunityCommunityId(uuid.uuid4()),
        name=CommunityName("guild"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    async with CommunityUnitOfWork(create_session_factory(engine)) as uow:
        await uow.communities.add(community)
        await uow.commit()
    return community.id.value


async def _create_at_rest_server(engine: AsyncEngine) -> Server:
    community_id = await _seed_community(engine)
    return await CreateServer(
        uow=ServersUnitOfWork(create_session_factory(engine)),
        clock=_FixedClock(),
        version_validator=FakeVersionValidator(),
        file_store=FakeFileStore(),
        port_range=PortRange(start=25565, end=25664),
    )(
        community_id=CommunityId(community_id),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="host_process",
        config={},
    )


async def _seed_backup(engine: AsyncEngine, server_id: ServerId) -> BackupId:
    backup_id = BackupId.new()
    async with ServersUnitOfWork(create_session_factory(engine)) as uow:
        await uow.backups.add(
            Backup(
                id=backup_id,
                server_id=server_id,
                storage_ref="ref",
                size_bytes=None,
                source=BackupSource.MANUAL,
                health=BackupHealth.HEALTHY,
                created_by=None,
                created_at=_NOW,
            )
        )
        await uow.commit()
    return backup_id


async def _load(engine: AsyncEngine, server_id: ServerId) -> Server | None:
    async with ServersUnitOfWork(create_session_factory(engine)) as uow:
        return await uow.servers.get_by_id(server_id)


async def test_start_blocks_until_restore_releases_the_lock(
    engine: AsyncEngine,
) -> None:
    server = await _create_at_rest_server(engine)
    backup_id = await _seed_backup(engine, server.id)
    lock = PgLifecycleLock(engine=engine)

    blocking_store = _BlockingBackupStore()
    restore = RestoreBackup(
        uow=ServersUnitOfWork(create_session_factory(engine)),
        backup_store=blocking_store,
        lifecycle_lock=lock,
    )
    start = StartServer(
        uow=ServersUnitOfWork(create_session_factory(engine)),
        control_plane=FakeControlPlane(place_to=WorkerId(uuid.uuid4())),
        clock=_FixedClock(),
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
        lifecycle_lock=lock,
    )

    restore_task = asyncio.create_task(
        restore(
            community_id=server.community_id,
            server_id=server.id,
            backup_id=backup_id,
        )
    )
    # Wait until the restore is inside its body holding the lock.
    await asyncio.wait_for(blocking_store.entered.wait(), timeout=5)

    start_task = asyncio.create_task(
        start(community_id=server.community_id, server_id=server.id)
    )
    # Give the start a chance to run: it must block on the advisory lock, so it does
    # NOT complete and the row stays at rest (desired=stopped) while restore holds.
    await asyncio.sleep(0.5)
    assert not start_task.done()
    blocked = await _load(engine, server.id)
    assert blocked is not None
    assert blocked.desired_state is DesiredState.STOPPED

    # Release the restore: it commits and drops the lock, then the start proceeds.
    blocking_store.release.set()
    await asyncio.wait_for(restore_task, timeout=5)
    started = await asyncio.wait_for(start_task, timeout=5)
    assert started.desired_state is DesiredState.RUNNING

    persisted = await _load(engine, server.id)
    assert persisted is not None
    assert persisted.desired_state is DesiredState.RUNNING
