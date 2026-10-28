"""An upload's publication window is serialized with the deletes (issue #3219).

``UploadBackup`` commits its backup row BEFORE it writes the archive (#1707). A
``DeleteServer`` or ``DeleteBackup`` that lands between the two would remove the
row, then the upload would write its archive anyway and report success for a
backup that no longer exists: an archive no delete saw, under a deleted server
or a deleted backup. The upload holds the per-server lifecycle lock across its
server check, the row commit and the archive write, so a delete arriving in that
window waits for the publication and then handles the published backup, and a
delete that already holds the lock turns the upload into a typed not-found.

Each delete is launched at the entry to ``backup_store.store`` (or the upload at
the entry to the delete's prune) and given ``_WINDOW_SECONDS`` to run. Unserialized,
it finishes inside that window, which is the race; serialized, it is still waiting
on the lock when the window closes, so the outcome asserted below never depends on
the window's length.

Runs on real PostgreSQL (the advisory lock and the cascading row delete) over the
fs storage adapter. DB-gated: skipped unless ``MCD_TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import os
import tarfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

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
from mc_server_dashboard_api.servers.application.backups import (
    DeleteBackup,
    UploadBackup,
)
from mc_server_dashboard_api.servers.application.manage_server import (
    CreateServer,
    DeleteServer,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import ServerNotFoundError
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
)
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.fakes import FakeClock, FakeFileStore, FakeVersionValidator
from tests.storage.helpers import tar_stream

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_WINDOW_SECONDS = 1.0


class _HookedBackupStore(StorageBackupStoreAdapter):
    """The real fs adapter with a hook at the entry to ``store`` and the prune.

    Records what each ``list_archive_refs`` returned, which is the set of archives
    ``DeleteServer`` saw when it pruned the server's storage.
    """

    def __init__(self, *, storage: FsStorage) -> None:
        super().__init__(storage=storage)
        self.on_store: Callable[[str], Awaitable[None]] | None = None
        self.on_prune: Callable[[], Awaitable[None]] | None = None
        self.listed: list[list[str]] = []

    async def store(
        self,
        *,
        community_id: CommunityId,
        server_id: ServerId,
        stream: AsyncIterator[bytes],
        storage_ref: str,
    ) -> None:
        if self.on_store is not None:
            await self.on_store(storage_ref)
        await super().store(
            community_id=community_id,
            server_id=server_id,
            stream=stream,
            storage_ref=storage_ref,
        )

    async def prune_to_final_snapshot(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> None:
        if self.on_prune is not None:
            await self.on_prune()
        await super().prune_to_final_snapshot(
            community_id=community_id, server_id=server_id
        )

    async def list_archive_refs(
        self, *, community_id: CommunityId, server_id: ServerId
    ) -> list[str]:
        refs = await super().list_archive_refs(
            community_id=community_id, server_id=server_id
        )
        self.listed.append(sorted(refs))
        return refs


@pytest.fixture
async def engine(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncEngine]:
    # The waiting delete must outlast the held lock rather than give up with
    # ServerBusyError; the suite-wide pytest-timeout is the hang guard.
    monkeypatch.setattr(lifecycle_lock_mod, "_ACQUIRE_BUDGET_SECONDS", 600.0)
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    eng = create_async_engine(_DB_URL)
    try:
        yield eng
    finally:
        await eng.dispose()
        await downgrade_base(_DB_URL)


async def _create_server(factory: async_sessionmaker[AsyncSession]) -> Server:
    community = Community(
        id=CommunityCommunityId(uuid.uuid4()),
        name=CommunityName("guild"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    async with CommunityUnitOfWork(factory) as community_uow:
        await community_uow.communities.add(community)
        await community_uow.commit()
    return await CreateServer(
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


async def _publish_working_set(storage: FsStorage, server: Server) -> None:
    community = StorageCommunityId(server.community_id.value)
    server_id = StorageServerId(server.id.value)
    base = await storage.current_generation(community, server_id)
    handle = await storage.begin_snapshot(community, server_id)
    await storage.write_snapshot(handle, tar_stream({"level.dat": b"world"}))
    await storage.commit_snapshot(handle, expected_base=base)


def _archive() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="server.properties")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"k=v"))
    return buf.getvalue()


async def _setup(
    engine: AsyncEngine, tmp_path: Path
) -> tuple[
    async_sessionmaker[AsyncSession], Server, _HookedBackupStore, PgLifecycleLock
]:
    factory = create_session_factory(engine)
    server = await _create_server(factory)
    storage = FsStorage(tmp_path)
    await _publish_working_set(storage, server)
    return (
        factory,
        server,
        _HookedBackupStore(storage=storage),
        PgLifecycleLock(engine=engine),
    )


def _upload(
    factory: async_sessionmaker[AsyncSession],
    store: _HookedBackupStore,
    lock: PgLifecycleLock,
) -> UploadBackup:
    return UploadBackup(
        uow=ServersUnitOfWork(factory),
        backup_store=store,
        clock=FakeClock(_NOW),
        lifecycle_lock=lock,
    )


async def test_server_delete_in_the_upload_window_waits_for_the_publication(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    factory, server, store, lock = await _setup(engine, tmp_path)
    delete = DeleteServer(
        uow=ServersUnitOfWork(factory), backup_store=store, lifecycle_lock=lock
    )
    deletes: list[asyncio.Task[None]] = []

    async def _delete_server_in_window(_storage_ref: str) -> None:
        task = asyncio.create_task(
            delete(community_id=server.community_id, server_id=server.id)
        )
        deletes.append(task)
        await asyncio.wait({task}, timeout=_WINDOW_SECONDS)

    store.on_store = _delete_server_in_window

    backup = await _upload(factory, store, lock)(
        community_id=server.community_id, server_id=server.id, content=_archive()
    )
    await deletes[0]

    # The delete ran after the publication: its storage prune saw the uploaded
    # archive, so it was a backup the delete knew about, not one written behind it.
    assert store.listed == [[backup.storage_ref]]
    # The row went with the server; the archive is the newest backup, which the
    # server delete retains by design (#777).
    async with ServersUnitOfWork(factory) as uow:
        assert await uow.servers.get_by_id(server.id) is None
        assert await uow.backups.get_by_id(backup.id) is None
    remaining = await store.list_archive_refs(
        community_id=server.community_id, server_id=server.id
    )
    assert remaining == [backup.storage_ref]


async def test_backup_delete_in_the_upload_window_removes_the_published_archive(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    factory, server, store, lock = await _setup(engine, tmp_path)
    delete = DeleteBackup(
        uow=ServersUnitOfWork(factory), backup_store=store, lifecycle_lock=lock
    )
    deletes: list[asyncio.Task[None]] = []

    async def _delete_backup_in_window(storage_ref: str) -> None:
        async with ServersUnitOfWork(factory) as uow:
            rows = await uow.backups.list_for_server(server.id)
        (pending,) = [row for row in rows if row.storage_ref == storage_ref]
        task = asyncio.create_task(
            delete(
                community_id=server.community_id,
                server_id=server.id,
                backup_id=pending.id,
            )
        )
        deletes.append(task)
        await asyncio.wait({task}, timeout=_WINDOW_SECONDS)

    store.on_store = _delete_backup_in_window

    backup = await _upload(factory, store, lock)(
        community_id=server.community_id, server_id=server.id, content=_archive()
    )
    await deletes[0]

    # The delete waited for the publication and then removed both halves: no
    # archive is left behind without the row that would find it.
    async with ServersUnitOfWork(factory) as uow:
        assert await uow.servers.get_by_id(server.id) is not None
        assert await uow.backups.get_by_id(backup.id) is None
    remaining = await store.list_archive_refs(
        community_id=server.community_id, server_id=server.id
    )
    assert remaining == []


async def test_upload_behind_a_server_delete_is_refused_as_not_found(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    factory, server, store, lock = await _setup(engine, tmp_path)
    in_prune = asyncio.Event()
    release_prune = asyncio.Event()

    async def _park_prune() -> None:
        in_prune.set()
        await release_prune.wait()

    store.on_prune = _park_prune
    delete_task = asyncio.create_task(
        DeleteServer(
            uow=ServersUnitOfWork(factory), backup_store=store, lifecycle_lock=lock
        )(community_id=server.community_id, server_id=server.id)
    )
    await in_prune.wait()

    upload_task = asyncio.create_task(
        _upload(factory, store, lock)(
            community_id=server.community_id,
            server_id=server.id,
            content=_archive(),
        )
    )
    await asyncio.wait({upload_task}, timeout=_WINDOW_SECONDS)
    release_prune.set()
    await delete_task

    with pytest.raises(ServerNotFoundError):
        await upload_task
    remaining = await store.list_archive_refs(
        community_id=server.community_id, server_id=server.id
    )
    assert remaining == []
