"""The object backend's health-check spool under cancellation and local I/O faults.

Since issue #3237 the object ``check_backup_health`` writes the archive it reads to
a local temp spool and walks its members. That spool is local disk, not the object
store: a cancelled check must not leave it (or its fd) behind, and a transient
fault on it is a "storage unavailable" outcome, never a verdict about the archive.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import errno
import gzip
import os
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from mc_server_dashboard_api.servers.adapters.backup_store import (
    StorageBackupStoreAdapter,
)
from mc_server_dashboard_api.servers.application.integrity_sweep import IntegritySweep
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    BackupStorageUnavailableError,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)
from mc_server_dashboard_api.storage.adapters import fs, object_store
from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from mc_server_dashboard_api.storage.domain.errors import ArchiveUnreadableError
from mc_server_dashboard_api.storage.domain.value_objects import (
    BackupKey,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from tests.audit.fakes import RecordingAuditRecorder
from tests.servers.fakes import (
    FakeBackupRepository,
    FakeServerRepository,
    FakeUnitOfWork,
)
from tests.storage.fake_s3 import FakeS3Store, fake_s3_factory
from tests.storage.helpers import malicious_tar_with_escape, stream_of, tar_bytes

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)

_REAL_MKSTEMP = tempfile.mkstemp


def _server() -> Server:
    return Server(
        id=ServerId.new(),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=DesiredState.STOPPED,
        observed_state=ObservedState.STOPPED,
        observed_at=None,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


async def _stored_backup(storage: ObjectStorage, server: Server) -> Backup:
    key = await storage.put_backup(
        StorageCommunityId(server.community_id.value),
        StorageServerId(server.id.value),
        stream_of(gzip.compress(tar_bytes({"keep.txt": b"ok"}))),
    )
    return Backup(
        id=BackupId.new(),
        server_id=server.id,
        storage_ref=key.value,
        size_bytes=None,
        source=BackupSource.UPLOADED,
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=_NOW,
    )


def _spy_spools(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    """Record every temp file the check creates, so a leftover can be found."""

    created: list[tuple[int, str]] = []

    def _spy(*args: Any, **kwargs: Any) -> tuple[int, str]:
        fd, name = _REAL_MKSTEMP(*args, **kwargs)
        created.append((fd, name))
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", _spy)
    return created


def _fd_points_at(fd: int, name: str) -> bool:
    try:
        return os.readlink(f"/proc/self/fd/{fd}") == name
    except OSError:
        return False


async def test_a_check_cancelled_while_the_spool_is_created_leaves_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelled while the worker thread is still creating the spool: once the
    thread finishes, the file it made is removed and its fd is closed."""

    storage = ObjectStorage(fake_s3_factory(FakeS3Store()))
    server = _server()
    backup = await _stored_backup(storage, server)
    allocated = threading.Event()
    release = threading.Event()
    created: list[tuple[int, str]] = []

    def _paused(*args: Any, **kwargs: Any) -> tuple[int, str]:
        fd, name = _REAL_MKSTEMP(*args, **kwargs)
        created.append((fd, name))
        allocated.set()
        release.wait(timeout=10)
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", _paused)
    check = asyncio.ensure_future(
        storage.check_backup_health(
            StorageCommunityId(server.community_id.value),
            StorageServerId(server.id.value),
            BackupKey(backup.storage_ref),
        )
    )
    assert await asyncio.to_thread(allocated.wait, 10)

    check.cancel()
    with pytest.raises(asyncio.CancelledError):
        await check
    release.set()

    [(fd, name)] = created
    for _ in range(200):
        if not os.path.exists(name) and not _fd_points_at(fd, name):
            break
        await asyncio.sleep(0.01)
    assert not os.path.exists(name)
    assert not _fd_points_at(fd, name)


class _FailingWrites:
    def __init__(self, handle: Any) -> None:
        self._handle = handle

    def write(self, _data: bytes) -> int:
        raise OSError(errno.EIO, "injected write fault")

    def __enter__(self) -> _FailingWrites:
        return self

    def __exit__(self, *exc: object) -> None:
        self._handle.close()


def _inject_eio(monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    if stage == "create":

        def _failing_mkstemp(*_args: Any, **_kwargs: Any) -> tuple[int, str]:
            raise OSError(errno.EIO, "injected create fault")

        monkeypatch.setattr(tempfile, "mkstemp", _failing_mkstemp)
    elif stage == "write":
        monkeypatch.setattr(
            object_store,
            "open",
            lambda *a, **k: _FailingWrites(open(*a, **k)),  # noqa: SIM115
            raising=False,
        )
    else:
        assert stage == "read"

        def _failing_read(self: Any, size: int) -> bytes:
            raise OSError(errno.EIO, "injected read fault")

        # The low-level file ``tarfile`` stream mode reads a named archive through.
        monkeypatch.setattr("tarfile._LowLevelFile.read", _failing_read)


@pytest.mark.parametrize("stage", ["create", "write", "read"])
async def test_a_spool_io_fault_is_unavailable_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """A transient fault on the local spool is the store-unavailable outcome at
    the seam: the sweep stops without a verdict, the backup keeps its health,
    and no spool is left behind."""

    monkeypatch.setattr(object_store, "_REPROBE_BACKOFF_S", 0)
    storage = ObjectStorage(fake_s3_factory(FakeS3Store()))
    server = _server()
    backup = await _stored_backup(storage, server)
    servers = FakeServerRepository()
    servers.seed(server)
    backups = FakeBackupRepository()
    backups.seed(backup)
    sweep = IntegritySweep(
        uow=FakeUnitOfWork(servers=servers, backups=backups),
        backup_store=StorageBackupStoreAdapter(storage=storage),
        audit=RecordingAuditRecorder(),
    )
    created = _spy_spools(monkeypatch)
    _inject_eio(monkeypatch, stage)

    with pytest.raises(BackupStorageUnavailableError):
        await sweep()

    assert backups.by_id[backup.id].health is BackupHealth.HEALTHY
    assert all(not Path(name).exists() for _fd, name in created)


async def test_a_spool_is_removed_through_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelled after the spool exists, while the only executor worker is busy,
    then cancelled again while its removal is still queued: the removal runs
    once the worker frees up, so the spool does not outlive the check."""

    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    storage = ObjectStorage(fake_s3_factory(FakeS3Store()))
    server = _server()
    backup = await _stored_backup(storage, server)
    created = _spy_spools(monkeypatch)
    probing = asyncio.Event()

    async def _stalled_probe(*_args: Any, **_kwargs: Any) -> object:
        probing.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(ObjectStorage, "_probe_archive", _stalled_probe)
    check = asyncio.ensure_future(
        storage.check_backup_health(
            StorageCommunityId(server.community_id.value),
            StorageServerId(server.id.value),
            BackupKey(backup.storage_ref),
        )
    )
    await probing.wait()
    [(_fd, name)] = created
    worker_free = threading.Event()
    occupied = loop.run_in_executor(executor, worker_free.wait, 10)

    check.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not check.done()  # its removal is queued behind the busy worker
    check.cancel()
    with pytest.raises(asyncio.CancelledError):
        await check

    worker_free.set()
    await occupied
    for _ in range(200):
        if not os.path.exists(name):
            break
        await asyncio.sleep(0.01)
    assert not os.path.exists(name)


def _failing_spool_unlink(
    monkeypatch: pytest.MonkeyPatch, created: list[tuple[int, str]]
) -> None:
    """Make removing a spool raise EIO; every other unlink behaves normally."""

    real_unlink = Path.unlink

    def _unlink(self: Path, missing_ok: bool = False) -> None:
        if any(str(self) == name for _fd, name in created):
            raise OSError(errno.EIO, "injected unlink fault")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _unlink)


def _remove_left_spools(
    monkeypatch: pytest.MonkeyPatch, created: list[tuple[int, str]]
) -> None:
    """Undo the injected fault and remove the spools it kept on disk."""

    monkeypatch.undo()
    for _fd, name in created:
        Path(name).unlink(missing_ok=True)


async def test_a_spool_removal_fault_after_a_sound_check_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check itself succeeded, so the transient cleanup fault is the outcome:
    store-unavailable at the seam, the sweep stops, the health is unchanged."""

    storage = ObjectStorage(fake_s3_factory(FakeS3Store()))
    server = _server()
    backup = await _stored_backup(storage, server)
    servers = FakeServerRepository()
    servers.seed(server)
    backups = FakeBackupRepository()
    backups.seed(backup)
    sweep = IntegritySweep(
        uow=FakeUnitOfWork(servers=servers, backups=backups),
        backup_store=StorageBackupStoreAdapter(storage=storage),
        audit=RecordingAuditRecorder(),
    )
    created = _spy_spools(monkeypatch)
    _failing_spool_unlink(monkeypatch, created)

    try:
        with pytest.raises(BackupStorageUnavailableError):
            await sweep()
        assert backups.by_id[backup.id].health is BackupHealth.HEALTHY
    finally:
        _remove_left_spools(monkeypatch, created)


async def test_a_spool_removal_fault_keeps_the_pending_archive_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The archive verdict is already propagating when the cleanup fails: the
    cleanup fault is logged, and the verdict is what the caller sees."""

    storage = ObjectStorage(fake_s3_factory(FakeS3Store()))
    server = _server()
    community = StorageCommunityId(server.community_id.value)
    scope = StorageServerId(server.id.value)
    key = await storage.put_backup(
        community, scope, stream_of(gzip.compress(malicious_tar_with_escape()))
    )
    created = _spy_spools(monkeypatch)
    _failing_spool_unlink(monkeypatch, created)

    try:
        with pytest.raises(ArchiveUnreadableError):
            await storage.check_backup_health(community, scope, key)
    finally:
        _remove_left_spools(monkeypatch, created)


def test_a_spool_removal_cancelled_at_loop_shutdown_still_removes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``asyncio.run`` cancels the still-queued removal task itself on shutdown,
    which drops the queued unlink: the spool is removed all the same."""

    created = _spy_spools(monkeypatch)
    worker_free = threading.Event()

    async def _stalled_probe(*_args: Any, **_kwargs: Any) -> object:
        probing.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def _free_the_worker_once_shutdown_cancels() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            # Let the runner's cancel reach the queued removal first.
            for _ in range(100):
                if not fs._SPOOL_DISPOSALS:
                    break
                await asyncio.sleep(0)
            worker_free.set()

    async def _main() -> asyncio.Task[None]:
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(executor)
        storage = ObjectStorage(fake_s3_factory(FakeS3Store()))
        server = _server()
        backup = await _stored_backup(storage, server)
        monkeypatch.setattr(ObjectStorage, "_probe_archive", _stalled_probe)
        check = asyncio.ensure_future(
            storage.check_backup_health(
                StorageCommunityId(server.community_id.value),
                StorageServerId(server.id.value),
                BackupKey(backup.storage_ref),
            )
        )
        await probing.wait()
        loop.run_in_executor(executor, worker_free.wait, 10)
        check.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        check.cancel()
        with pytest.raises(asyncio.CancelledError):
            await check
        assert fs._SPOOL_DISPOSALS  # the removal is still queued
        # Returned pending, so the runner's shutdown cancels it.
        return asyncio.ensure_future(_free_the_worker_once_shutdown_cancels())

    probing = asyncio.Event()
    asyncio.run(_main())

    [(_fd, name)] = created
    try:
        assert not os.path.exists(name)
    finally:
        Path(name).unlink(missing_ok=True)
