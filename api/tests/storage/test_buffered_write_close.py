"""A cancelled buffered write closes its temp file off the loop (#3603).

Several paths write a temp file through executor jobs. Cancelling the caller does
not stop a job's thread, and that thread holds the buffered writer's lock while
its raw write runs. A close on the loop, as a ``with`` exit does, waits for that
lock and then flushes: the whole loop blocks until the write ends.

Each case holds a raw write under the writer's lock, cancels the caller, and
checks that the caller is cancelled at once and that the loop keeps running while
the write is held. Once the write is let go, the file is closed and the path's
cleanup has run.
"""

from __future__ import annotations

import asyncio
import builtins
import gzip
import io
import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from mc_server_dashboard_api.storage.adapters import fs, object_store
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId,
    ServerId,
)
from tests.storage.conftest import StorageHarness, build_harness
from tests.storage.helpers import new_scope, tar_bytes

# Past the writer's 8 KiB buffer, so a write hands it to the raw file at once,
# with the writer's lock held.
_PAYLOAD = os.urandom(64 * 1024)


@dataclass
class _Hold:
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    armed: threading.Lock = field(default_factory=threading.Lock)
    # Strong references, so only an explicit close counts as one.
    files: list[tuple[Path, io.BufferedWriter[_HeldRaw]]] = field(default_factory=list)


class _HeldRaw(io.FileIO):
    """A raw file whose first write waits for ``hold.release``.

    The wait has no timeout: one that let go by itself would let the write finish
    and unblock the loop, faking a pass (issue #3600). The test's ``finally``, the
    watchdog and the fixture's teardown release it.
    """

    def __init__(self, path: Path, hold: _Hold) -> None:
        super().__init__(path, "wb")
        self._hold = hold

    def write(self, data: Any) -> int:
        if self._hold.armed.acquire(blocking=False):
            self._hold.entered.set()
            self._hold.release.wait()
        return super().write(data)


@pytest.fixture
async def hold() -> AsyncIterator[_Hold]:
    # Async, so its teardown runs before the event loop's: the loop's shutdown
    # waits for the executor thread the hold parks.
    held = _Hold()
    yield held
    held.release.set()


def _hold_writes(monkeypatch: pytest.MonkeyPatch, hold: _Hold, prefix: str) -> None:
    """Open each ``prefix`` temp file for writing with a held raw file."""

    def _open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if mode == "wb" and Path(file).name.startswith(prefix):
            out = io.BufferedWriter(_HeldRaw(Path(file), hold))
            hold.files.append((Path(file), out))
            return out
        return builtins.open(file, mode, *args, **kwargs)

    for module in (fs, object_store):
        monkeypatch.setattr(module, "open", _open, raising=False)


async def _one_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


_Op = Callable[[], Awaitable[object]]
_Leftovers = Callable[[], Awaitable[list[Any]]]


@dataclass(frozen=True)
class _Site:
    name: str
    backend: str
    prefix: str
    # Returns the operation under test and what it must leave behind once the
    # write is let go: nothing.
    arrange: Callable[
        [StorageHarness, CommunityId, ServerId], Awaitable[tuple[_Op, _Leftovers]]
    ]


async def _write_snapshot(
    harness: StorageHarness, community: CommunityId, server: ServerId
) -> tuple[_Op, _Leftovers]:
    storage = harness.storage
    handle = await storage.begin_snapshot(community, server)
    assert isinstance(storage, fs.FsStorage)
    incoming = storage._server_root(community, server) / "incoming"

    async def _as_the_snapshot_endpoint() -> None:
        # The data-plane edge aborts the handle on any failure, a cancel included.
        try:
            await storage.write_snapshot(handle, _one_chunk(_PAYLOAD))
        except BaseException:
            await storage.abort_snapshot(handle)
            raise

    async def _staging() -> list[Any]:
        return list(incoming.iterdir())

    return _as_the_snapshot_endpoint, _staging


async def _put_jar(
    harness: StorageHarness, _community: CommunityId, _server: ServerId
) -> tuple[_Op, _Leftovers]:
    async def _jars() -> list[Any]:
        return list(await harness.storage.list_jars())

    return lambda: harness.storage.put_jar(_one_chunk(_PAYLOAD)), _jars


async def _put_backup(
    harness: StorageHarness, community: CommunityId, server: ServerId
) -> tuple[_Op, _Leftovers]:
    async def _backups() -> list[Any]:
        return list(await harness.storage.list_backups(community, server))

    return (
        lambda: harness.storage.put_backup(community, server, _one_chunk(_PAYLOAD)),
        _backups,
    )


async def _check_backup_health(
    harness: StorageHarness, community: CommunityId, server: ServerId
) -> tuple[_Op, _Leftovers]:
    # Incompressible, so each of the fake store's two chunks passes the buffer.
    archive = gzip.compress(tar_bytes({"a.bin": _PAYLOAD}))
    key = await harness.storage.put_backup(community, server, _one_chunk(archive))

    async def _nothing() -> list[Any]:
        return []

    return (
        lambda: harness.storage.check_backup_health(community, server, key),
        _nothing,
    )


_SITES = [
    _Site("write_snapshot", "fs", ".snapshot.", _write_snapshot),
    _Site("put_jar", "fs", ".jar.", _put_jar),
    _Site("put_backup", "fs", ".backup.", _put_backup),
    # The object adapter spools an upload through ``_spool_stream``.
    _Site("put_jar", "object", ".jar.", _put_jar),
    _Site("check_backup_health", "object", ".fsck.", _check_backup_health),
]


def _watchdog(
    task: asyncio.Future[Any], hold: _Hold
) -> tuple[threading.Thread, list[bool]]:
    """Release the hold if ``task`` is not done soon after its cancel.

    A loop blocked on the held write cannot run the test again until the write
    ends, so only a thread can end the hold then. It records that it had to, and
    the test fails on that record.
    """

    done = threading.Event()
    task.add_done_callback(lambda _task: done.set())
    blocked: list[bool] = []

    def _run() -> None:
        if not done.wait(5):
            blocked.append(True)
            hold.release.set()

    watchdog = threading.Thread(target=_run, daemon=True)
    watchdog.start()
    return watchdog, blocked


async def _until(predicate: Callable[[], bool]) -> None:
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0.01)


# Waits in turn: under 45 s of short bounds (10 + 10 + the 5 s watchdog + 1 + the
# 1000-step ``_until``) and the 120 s staging-removal bound below, plus 30 s of
# headroom, so that bound's assertion reports a failure before the timeout does.
@pytest.mark.timeout(45 + 120 + 30)
@pytest.mark.parametrize("site", _SITES, ids=lambda site: f"{site.backend}-{site.name}")
async def test_a_cancelled_write_closes_its_file_off_the_loop(
    site: _Site, hold: _Hold, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_harness(site.backend, tmp_path)
    community, server = new_scope()
    op, leftovers = await site.arrange(harness, community, server)
    _hold_writes(monkeypatch, hold, site.prefix)

    task = asyncio.ensure_future(op())
    try:
        assert await asyncio.to_thread(hold.entered.wait, 10)
        [(spool, out)] = hold.files
        watchdog, blocked = _watchdog(task, hold)

        task.cancel()
        await asyncio.wait({task}, timeout=10)
        await asyncio.to_thread(watchdog.join)
        assert not blocked, "the loop blocked on the held write"
        assert task.cancelled()

        # The loop runs other work while the write is still held, and the file
        # stays open under it.
        await asyncio.wait_for(asyncio.ensure_future(asyncio.sleep(0.01)), 1)
        assert not hold.release.is_set()
        assert not out.closed
    finally:
        hold.release.set()

    await _until(lambda: out.closed and not spool.exists())
    assert out.closed
    assert not spool.exists()
    # The staging is removed only after the file's close (issue #3653). A bound,
    # not a delay: the removal can wait behind a queued fsync on a loaded host.
    deadline = asyncio.get_running_loop().time() + 120
    while await leftovers() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert await leftovers() == []


async def test_a_close_queued_behind_a_busy_worker_survives_a_cancel(
    hold: _Hold, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelled while the close after a clean exit still waits for a worker: the
    close is not dropped, and runs once the worker frees up."""

    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    hold.armed.acquire()  # record the file, never hold its writes
    _hold_writes(monkeypatch, hold, ".spool.")
    worker_free = threading.Event()
    occupied: list[asyncio.Future[bool]] = []

    async def _write() -> None:
        async with fs._writing_off_loop(tmp_path / ".spool.tmp") as out:
            await out.write(b"x")
            # No timeout (issue #3600): the ``finally`` below frees the worker.
            occupied.append(loop.run_in_executor(executor, worker_free.wait))

    task = asyncio.ensure_future(_write())
    try:
        await _until(lambda: bool(occupied))
        [(_spool, out)] = hold.files
        for _ in range(5):
            await asyncio.sleep(0)
        assert not task.done()  # its close is queued behind the busy worker

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        worker_free.set()

    await occupied[0]
    await _until(lambda: out.closed)
    assert out.closed


async def test_an_abandoned_file_is_closed_while_the_executor_shuts_down(
    hold: _Hold, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelled while its write is held, and the loop shuts its executor down
    before the write ends: the executor refuses the close, which still runs."""

    loop = asyncio.get_running_loop()
    # Two workers: the held write takes one, ``hold.entered.wait`` the other.
    loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
    _hold_writes(monkeypatch, hold, ".spool.")

    async def _write() -> None:
        async with fs._writing_off_loop(tmp_path / ".spool.tmp") as out:
            await out.write(_PAYLOAD)

    task = asyncio.ensure_future(_write())
    try:
        assert await asyncio.to_thread(hold.entered.wait, 10)
        [(_spool, out)] = hold.files
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        shutdown = asyncio.ensure_future(loop.shutdown_default_executor())
        await asyncio.sleep(0)  # the executor refuses new jobs from here on
    finally:
        hold.release.set()

    await shutdown
    await _until(lambda: out.closed)
    assert out.closed
