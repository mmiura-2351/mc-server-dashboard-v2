"""Temp files the storage adapters create off the loop, under cancellation (#3553).

Every adapter operation that stages bytes through a local temp file creates it on
an executor worker. Cancelling the caller does not stop that worker, and it does
not stop a removal already handed to one either. Neither may leave the file, or
its fd, behind. The object backend's health-check spool was pinned the same way in
``tests/servers/test_object_backup_health_spool.py`` (issue #3237).
"""

from __future__ import annotations

import asyncio
import errno
import gzip
import os
import tempfile
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from mc_server_dashboard_api.storage.adapters import object_store
from mc_server_dashboard_api.storage.adapters.object_store import _spool_stream
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId,
    ServerId,
)
from tests.storage.conftest import StorageHarness, build_harness
from tests.storage.helpers import new_scope, stream_of, tar_bytes

_REAL_MKSTEMP = tempfile.mkstemp

# Builds the operation under test against a harness; the stream it is handed is
# the one the operation consumes (operations that take no stream ignore it).
_Call = Callable[[AsyncIterator[bytes]], Awaitable[object]]


@dataclass(frozen=True)
class _Operation:
    backend: str
    name: str
    prefix: str
    arrange: Callable[[StorageHarness, CommunityId, ServerId], Awaitable[_Call]]
    payload: bytes = field(default_factory=lambda: tar_bytes({"a.txt": b"a"}))

    @property
    def id(self) -> str:
        return f"{self.backend}-{self.name}"


async def _write_snapshot(
    harness: StorageHarness, community: CommunityId, server: ServerId
) -> _Call:
    handle = await harness.storage.begin_snapshot(community, server)
    return lambda stream: harness.storage.write_snapshot(handle, stream)


async def _put_jar(
    harness: StorageHarness, _community: CommunityId, _server: ServerId
) -> _Call:
    return lambda stream: harness.storage.put_jar(stream)


async def _put_backup(
    harness: StorageHarness, community: CommunityId, server: ServerId
) -> _Call:
    return lambda stream: harness.storage.put_backup(community, server, stream)


async def _create_backup(
    harness: StorageHarness, community: CommunityId, server: ServerId
) -> _Call:
    await harness.publish(community, server, {"a.txt": b"a"})
    return lambda _stream: harness.storage.create_backup_from_current(community, server)


async def _prune(
    harness: StorageHarness, community: CommunityId, server: ServerId
) -> _Call:
    await harness.publish(community, server, {"a.txt": b"a"})
    return lambda _stream: harness.storage.prune_to_final_snapshot(community, server)


async def _restore(
    harness: StorageHarness, community: CommunityId, server: ServerId
) -> _Call:
    await harness.publish(community, server, {"a.txt": b"a"})
    key = await harness.storage.create_backup_from_current(community, server)
    return lambda _stream: harness.storage.restore_backup(community, server, key)


_TARGZ = gzip.compress(tar_bytes({"a.txt": b"a"}))

_ACQUISITIONS = [
    _Operation("object", "write_snapshot", ".snapshot.", _write_snapshot),
    _Operation("object", "put_jar", ".jar.", _put_jar, b"jar"),
    _Operation("object", "restore_backup", ".restore.", _restore),
    _Operation("object", "create_backup_from_current", ".backup.", _create_backup),
    _Operation("object", "prune_to_final_snapshot", ".final.", _prune),
    _Operation("fs", "write_snapshot", ".snapshot.", _write_snapshot),
    _Operation("fs", "put_jar", ".jar.", _put_jar, b"jar"),
    _Operation("fs", "put_backup", ".backup.", _put_backup, _TARGZ),
]

# The operations whose removal of the temp file ran as a plain awaited executor
# call — restore already discarded its spool through the shielded removal.
_DISPOSALS = [op for op in _ACQUISITIONS if op.id != "object-restore_backup"]


@dataclass
class _PausedCreation:
    allocated: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    created: list[tuple[int, str]] = field(default_factory=list)


def _pause_creation(monkeypatch: pytest.MonkeyPatch, prefix: str) -> _PausedCreation:
    """Hold the worker thread creating a ``prefix`` temp file once it exists."""

    paused = _PausedCreation()

    def _mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        fd, name = _REAL_MKSTEMP(*args, **kwargs)
        if kwargs.get("prefix") == prefix:
            paused.created.append((fd, name))
            paused.allocated.set()
            paused.release.wait(timeout=10)
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", _mkstemp)
    return paused


def _spy_creation(monkeypatch: pytest.MonkeyPatch, prefix: str) -> list[str]:
    created: list[str] = []

    def _mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
        fd, name = _REAL_MKSTEMP(*args, **kwargs)
        if kwargs.get("prefix") == prefix:
            created.append(name)
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", _mkstemp)
    return created


def _fd_still_open(fd: int, name: str) -> bool:
    try:
        target = os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return False
    return target in (name, f"{name} (deleted)")


async def _until(predicate: Callable[[], bool]) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.01)


@pytest.mark.parametrize("op", _ACQUISITIONS, ids=lambda op: op.id)
async def test_a_temp_file_made_after_its_caller_was_cancelled_is_removed(
    op: _Operation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelled while the worker thread is still creating the temp file: once
    the thread finishes, the file it made is removed and its fd is closed."""

    harness = build_harness(op.backend, tmp_path)
    community, server = new_scope()
    call = await op.arrange(harness, community, server)
    paused = _pause_creation(monkeypatch, op.prefix)
    task = asyncio.ensure_future(call(stream_of(op.payload)))
    assert await asyncio.to_thread(paused.allocated.wait, 10)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    paused.release.set()

    [(fd, name)] = paused.created
    await _until(lambda: not os.path.exists(name) and not _fd_still_open(fd, name))
    assert not os.path.exists(name)
    assert not _fd_still_open(fd, name)


@pytest.mark.parametrize("op", _DISPOSALS, ids=lambda op: op.id)
async def test_a_temp_file_is_removed_through_repeated_cancellation(
    op: _Operation, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelled after the temp file is written, while the only executor worker
    is busy, then cancelled again while its removal is still queued: the removal
    runs once the worker frees up, so the file does not outlive the operation."""

    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    harness = build_harness(op.backend, tmp_path)
    community, server = new_scope()
    call = await op.arrange(harness, community, server)
    created = _spy_creation(monkeypatch, op.prefix)
    worker_free = threading.Event()
    occupied: list[asyncio.Future[bool]] = []

    def _occupy_the_worker() -> None:
        occupied.append(loop.run_in_executor(executor, worker_free.wait, 10))

    async def _stream_then_occupy() -> AsyncIterator[bytes]:
        async for chunk in stream_of(op.payload):
            yield chunk
        _occupy_the_worker()

    real_write_targz = object_store._write_backup_targz

    async def _write_targz_then_occupy(*args: Any, **kwargs: Any) -> None:
        await real_write_targz(*args, **kwargs)
        _occupy_the_worker()

    # The operations that take no stream fill their temp file from the store.
    monkeypatch.setattr(object_store, "_write_backup_targz", _write_targz_then_occupy)
    task = asyncio.ensure_future(call(_stream_then_occupy()))
    await _until(lambda: bool(occupied))
    [name] = created
    for _ in range(5):
        await asyncio.sleep(0)

    task.cancel()
    for _ in range(5):
        await asyncio.sleep(0)
    assert not task.done()  # its removal is queued behind the busy worker
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    worker_free.set()
    await occupied[0]
    await _until(lambda: not os.path.exists(name))
    assert not os.path.exists(name)


class _StreamFault(Exception):
    pass


async def test_a_spool_removal_fault_keeps_the_pending_stream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The source stream failed mid-spool and the spool cannot be removed: the
    removal fault is logged, and the stream's error is what the caller sees."""

    created = _spy_creation(monkeypatch, ".fault.")
    real_unlink = Path.unlink

    def _unlink(self: Path, missing_ok: bool = False) -> None:
        if str(self) in created:
            raise OSError(errno.EIO, "injected unlink fault")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _unlink)

    async def _failing() -> AsyncIterator[bytes]:
        yield b"x"
        raise _StreamFault

    try:
        with pytest.raises(_StreamFault):
            await _spool_stream(_failing(), ".fault.", ".tmp")
    finally:
        monkeypatch.undo()
        for name in created:
            Path(name).unlink(missing_ok=True)
