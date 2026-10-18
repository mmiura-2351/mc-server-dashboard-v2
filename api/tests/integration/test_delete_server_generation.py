"""A failed DeleteServer commit must not lower the store generation (issue #3434).

``DeleteServer`` prunes the working set (``prune_to_final_snapshot``) BEFORE it
commits the row delete. When that commit fails the row survives the prune, so the
generation the prune leaves behind is what every later start compares a Worker's
held scratch against: had it gone down, the next publish would mint a generation at
or below one a Worker already holds, and that Worker's start would skip the hydrate
and boot its stale scratch over the newly published world.

Runs on real PostgreSQL (the row delete's commit is the fault seam) against each
storage backend the delete can be wired to: the fs adapter and the object adapter
over the in-memory S3 fake.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path

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
from mc_server_dashboard_api.servers.adapters.backup_store import (
    StorageBackupStoreAdapter,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.manage_server import (
    CreateServer,
    DeleteServer,
)
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from mc_server_dashboard_api.storage.domain.port import Storage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.fakes import FakeClock, FakeFileStore, FakeVersionValidator
from tests.storage.fake_s3 import FakeS3Store, fake_s3_factory
from tests.storage.helpers import tar_stream

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


class _CommitFailed(Exception):
    """The injected failure of the row delete's commit."""


class _FailingCommitUnitOfWork(ServersUnitOfWork):
    """A servers unit of work whose every commit fails before reaching the DB."""

    async def commit(self) -> None:
        raise _CommitFailed


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


def _fs_storage(tmp_path: Path) -> Storage:
    return FsStorage(tmp_path)


def _object_storage(tmp_path: Path) -> Storage:
    del tmp_path
    return ObjectStorage(fake_s3_factory(FakeS3Store()))


async def _publish(
    storage: Storage, community: StorageCommunityId, server: StorageServerId
) -> int:
    # The data-plane publish's shape: the commit re-checks the generation the guard
    # read before the upload (issue #899).
    base = await storage.current_generation(community, server)
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream({"f": uuid.uuid4().bytes}))
    return await storage.commit_snapshot(handle, expected_base=base)


@pytest.mark.parametrize(
    "make_storage", [_fs_storage, _object_storage], ids=["fs", "object"]
)
async def test_failed_delete_commit_never_lowers_the_store_generation(
    engine: AsyncEngine,
    tmp_path: Path,
    make_storage: Callable[[Path], Storage],
) -> None:
    factory = create_session_factory(engine)
    community = Community(
        id=CommunityCommunityId(uuid.uuid4()),
        name=CommunityName("guild"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    async with CommunityUnitOfWork(factory) as community_uow:
        await community_uow.communities.add(community)
        await community_uow.commit()
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
    storage = make_storage(tmp_path)
    scope = (StorageCommunityId(community.id.value), StorageServerId(server.id.value))
    # Several publishes, so a reset-and-republish lands visibly BELOW the held value.
    for _ in range(3):
        held = await _publish(storage, *scope)

    with pytest.raises(_CommitFailed):
        await DeleteServer(
            uow=_FailingCommitUnitOfWork(factory),
            backup_store=StorageBackupStoreAdapter(storage=storage),
        )(community_id=server.community_id, server_id=server.id)

    # The commit failed, so the row survives the prune.
    async with ServersUnitOfWork(factory) as uow:
        assert await uow.servers.get_by_id(server.id) is not None
    # A Worker may still hold the pre-delete scratch at ``held``. The next publish
    # must mint a generation above it, or that Worker's start skips the hydrate.
    assert await _publish(storage, *scope) > held
