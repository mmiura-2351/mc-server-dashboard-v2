"""A backup's archive is deleted only while its row's COMMIT is unsent (#4086).

A create or an upload writes its archive first, then records its row. A COMMIT
whose answer is lost with its connection raises although it applied, so the row
stands: deleting the archive then would leave the very row issue #4086 removes,
and the archive is kept (review r1 of PR #4119). The row's INSERT is flushed in a
savepoint before the COMMIT, so a refused INSERT still raises before any COMMIT
is sent, and its archive is deleted.

The fakes cannot show where the real adapter sends the INSERT, so this runs on
real PostgreSQL over the fs storage adapter. DB-gated: skipped unless
``MCD_TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.servers.adapters.backup_store import (
    StorageBackupStoreAdapter,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.backups import (
    CreateBackup,
    UploadBackup,
)
from mc_server_dashboard_api.servers.application.snapshot_scheduler import (
    SnapshotServer,
)
from mc_server_dashboard_api.servers.domain.backup import Backup, BackupSource
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from tests.integration.test_held_report_frame import _first_commit_answer_lost
from tests.integration.test_integrity_sweep import (  # noqa: F401
    _publish,
    _seed_server,
    engine,
)
from tests.servers.fakes import FakeClock, FakeControlPlane
from tests.storage.helpers import healthy_region_bytes, region_targz

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 11, 22, 12, 0, tzinfo=dt.timezone.utc)
_WORLD = {"world/region/r.0.0.mca": healthy_region_bytes()}

_KINDS = pytest.mark.parametrize("kind", ["create", "upload"])


class _DeletesTheServerAfterTheWrite(StorageBackupStoreAdapter):
    """Writes the archive, then deletes the server's row, so the backup's INSERT
    breaks its foreign key."""

    def __init__(self, *, storage: FsStorage, engine: AsyncEngine) -> None:  # noqa: F811
        super().__init__(storage=storage)
        self._engine = engine

    async def _delete_server(self, server_id: ServerId) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM server WHERE id = :id"), {"id": server_id.value}
            )

    async def create_from_current(
        self, *, community_id: CommunityId, server_id: ServerId, storage_ref: str
    ) -> None:
        await super().create_from_current(
            community_id=community_id, server_id=server_id, storage_ref=storage_ref
        )
        await self._delete_server(server_id)

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
        await self._delete_server(server_id)


async def _back_up(
    kind: str,
    uow_engine: AsyncEngine,
    store: StorageBackupStoreAdapter,
    community: CommunityId,
    server: ServerId,
) -> Backup:
    factory = create_session_factory(uow_engine)
    if kind == "create":
        return await CreateBackup(
            uow=ServersUnitOfWork(factory),
            backup_store=store,
            snapshot_server=SnapshotServer(
                uow=ServersUnitOfWork(factory), control_plane=FakeControlPlane()
            ),
            clock=FakeClock(_NOW),
        )(community_id=community, server_id=server, source=BackupSource.MANUAL)
    return await UploadBackup(
        uow=ServersUnitOfWork(factory), backup_store=store, clock=FakeClock(_NOW)
    )(community_id=community, server_id=server, content=region_targz(_WORLD))


async def _row_refs(engine: AsyncEngine) -> list[str]:  # noqa: F811
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT storage_ref FROM backup"))
        return [ref for (ref,) in result]


async def _archive_refs(
    storage: FsStorage, community: CommunityId, server: ServerId
) -> list[str]:
    return await StorageBackupStoreAdapter(storage=storage).list_archive_refs(
        community_id=community, server_id=server
    )


@_KINDS
async def test_a_backup_whose_commit_answer_is_lost_keeps_its_row_and_archive(
    engine: AsyncEngine,  # noqa: F811
    tmp_path: Path,
    kind: str,
) -> None:
    community, server = await _seed_server(engine)
    storage = FsStorage(tmp_path)
    await _publish(storage, community, server, _WORLD)

    async with _first_commit_answer_lost() as (proxy, proxied):
        with pytest.raises(DBAPIError):
            await _back_up(
                kind,
                proxied,
                StorageBackupStoreAdapter(storage=storage),
                community,
                server,
            )
        assert proxy.cuts == 1

    (row_ref,) = await _row_refs(engine)
    assert await _archive_refs(storage, community, server) == [row_ref]


@_KINDS
async def test_a_backup_whose_insert_is_refused_deletes_its_archive(
    engine: AsyncEngine,  # noqa: F811
    tmp_path: Path,
    kind: str,
) -> None:
    community, server = await _seed_server(engine)
    storage = FsStorage(tmp_path)
    await _publish(storage, community, server, _WORLD)
    store = _DeletesTheServerAfterTheWrite(storage=storage, engine=engine)

    with pytest.raises(IntegrityError):
        await _back_up(kind, engine, store, community, server)

    assert await _row_refs(engine) == []
    assert await _archive_refs(storage, community, server) == []
