"""``integrity_sweep_cli --delete-dangling`` against PostgreSQL + FsStorage (#4107).

The unit tests (``tests/servers/test_delete_dangling_backups.py``,
``tests/test_integrity_sweep_cli.py``) pin the pass and the command against
fakes. These run the command's real wiring end to end: the HEAD of a real
archive and of a missing one, the row delete and the ``audit_log`` row, the
store owner marker check against the database's installation id, and the real
advisory lock a delete in flight holds. DB-gated: skipped unless
``MCD_TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from mc_server_dashboard_api import integrity_sweep_cli
from mc_server_dashboard_api.config import load_settings
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.core.adapters.installation import read_installation_id
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
    DeleteDanglingBackups,
)
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.errors import StoreNotOwnedError
from mc_server_dashboard_api.servers.domain.value_objects import CommunityId, ServerId
from mc_server_dashboard_api.storage.adapters import ownership
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from tests.audit.fakes import RecordingAuditRecorder
from tests.integration.test_integrity_sweep import (  # noqa: F401
    _ParkedDeleteStore,
    _put_backup,
    _seed_server,
    engine,
)
from tests.storage.helpers import healthy_region_bytes
from tests.store_ownership import FakeStoreOwnership

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 10, 15, 12, 19, 36, tzinfo=dt.timezone.utc)
_WORLD = {"world/region/r.0.0.mca": healthy_region_bytes()}


def _row(server: ServerId, ref: str) -> Backup:
    return Backup(
        id=BackupId.new(),
        server_id=server,
        storage_ref=ref,
        size_bytes=None,
        source=BackupSource.SCHEDULED,
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=_NOW,
    )


async def _seed(
    engine: AsyncEngine,  # noqa: F811
    tmp_path: Path,
) -> tuple[CommunityId, ServerId, Backup, Backup]:
    """A server with a row whose archive exists and a row whose archive does not."""

    community, server = await _seed_server(engine)
    ref = await _put_backup(FsStorage(tmp_path), community, server, _WORLD)
    sound, dangling = _row(server, ref), _row(server, "no-such-archive")
    async with ServersUnitOfWork(create_session_factory(engine)) as uow:
        await uow.backups.add(sound)
        await uow.backups.add(dangling)
        await uow.commit()
    return community, server, sound, dangling


async def _row_ids(engine: AsyncEngine) -> set[BackupId]:  # noqa: F811
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT id FROM backup"))
        return {BackupId(row_id) for (row_id,) in result}


def _point_the_command_at(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert _DB_URL is not None
    monkeypatch.setenv("MCD_API_DATABASE__URL", _DB_URL)
    monkeypatch.setenv("MCD_API_STORAGE__BACKEND", "fs")
    monkeypatch.setenv("MCD_API_STORAGE__FS__ROOT", str(tmp_path))
    # The real marker, in place of the suite's stand-in (tests/conftest.py).
    monkeypatch.setattr(
        "mc_server_dashboard_api.app.FsStoreOwnership", ownership.FsStoreOwnership
    )


# The archive write fsyncs on the fs backend, at 10 s each plus 30 s (#3651).
@pytest.mark.timeout(6 * 10 + 30)
async def test_a_dry_run_lists_the_dangling_row_and_deletes_nothing(
    engine: AsyncEngine,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No owner marker: a dry run deletes nothing, so it does not ask for one.
    _, server, sound, dangling = await _seed(engine, tmp_path)
    _point_the_command_at(monkeypatch, tmp_path)

    found = await integrity_sweep_cli.run_delete_dangling(
        load_settings(None), server_id=server, apply=False
    )

    assert [b.id for b in found.dangling] == [dangling.id]
    assert await _row_ids(engine) == {sound.id, dangling.id}


@pytest.mark.timeout(6 * 10 + 30)
async def test_apply_deletes_the_dangling_row_and_audits_it(
    engine: AsyncEngine,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    community, server, sound, dangling = await _seed(engine, tmp_path)
    installation = await read_installation_id(create_session_factory(engine))
    (tmp_path / ownership.OWNER_MARKER).write_text(
        json.dumps({"installation_id": str(installation)})
    )
    _point_the_command_at(monkeypatch, tmp_path)

    found = await integrity_sweep_cli.run_delete_dangling(
        load_settings(None), server_id=None, apply=True
    )

    assert [b.id for b in found.dangling] == [dangling.id]
    assert await _row_ids(engine) == {sound.id}
    async with engine.connect() as conn:
        audited = (
            await conn.execute(
                text(
                    "SELECT operation, actor_id, community_id, target_type, "
                    "target_id FROM audit_log"
                )
            )
        ).all()
    assert audited == [
        (
            "backup:delete_dangling",
            None,
            community.value,
            "backup",
            dangling.id.value,
        )
    ]


@pytest.mark.timeout(6 * 10 + 30)
async def test_apply_refuses_a_store_this_database_has_not_marked(
    engine: AsyncEngine,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, sound, dangling = await _seed(engine, tmp_path)
    _point_the_command_at(monkeypatch, tmp_path)

    with pytest.raises(StoreNotOwnedError):
        await integrity_sweep_cli.run_delete_dangling(
            load_settings(None), server_id=None, apply=True
        )

    assert await _row_ids(engine) == {sound.id, dangling.id}


async def test_a_backup_whose_delete_is_between_its_removals_is_skipped_busy(
    engine: AsyncEngine,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A delete removes the archive before the row and holds the server's real
    # advisory lock across both (issue #3220). The pass must not judge the row
    # in between: it waits on the lock, and past the budget skips it as busy.
    monkeypatch.setattr(lifecycle_lock_mod, "_ACQUIRE_BUDGET_SECONDS", 0.5)
    community, server = await _seed_server(engine)
    factory = create_session_factory(engine)
    storage = FsStorage(tmp_path)
    row = _row(server, await _put_backup(storage, community, server, _WORLD))
    async with ServersUnitOfWork(factory) as uow:
        await uow.backups.add(row)
        await uow.commit()
    lock = PgLifecycleLock(engine=engine)
    parked = _ParkedDeleteStore(storage=storage)
    delete = DeleteBackup(
        uow=ServersUnitOfWork(factory), backup_store=parked, lifecycle_lock=lock
    )
    audit = RecordingAuditRecorder()
    delete_dangling = DeleteDanglingBackups(
        uow=ServersUnitOfWork(factory),
        backup_store=StorageBackupStoreAdapter(storage=storage),
        audit=audit,
        ownership=FakeStoreOwnership(),
        lifecycle_lock=lock,
    )

    delete_task = asyncio.create_task(
        delete(community_id=community, server_id=server, backup_id=row.id)
    )
    await parked.archive_removed.wait()
    try:
        found = await delete_dangling(apply=True, server_id=server)
    finally:
        parked.release.set()
    await delete_task

    assert [b.id for b in found.skipped_busy] == [row.id]
    assert found.dangling == ()
    assert audit.events == []
