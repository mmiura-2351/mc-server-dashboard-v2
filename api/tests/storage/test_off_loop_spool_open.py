"""A spool's open for writing runs off the loop, its handle owned (#3612).

Every path that spools bytes to a temp file writes it through
``_writing_off_loop``. Its open ran on the loop, so slow filesystem metadata I/O
blocked the whole loop. Off the loop, a cancel during the open does not stop the
worker thread: the open can finish after the cancelled caller has removed its
spool, recreate it (the #3553 race), and return a handle that no one owns.

Each case holds the open on its worker and cancels the caller, which must be
cancelled at once, with the spool already removed, while the open is still held.
Once the open is let go it recreates the spool, and the handle it returns is
closed and the spool removed again.
"""

from __future__ import annotations

import asyncio
import builtins
import errno
import gzip
import io
import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
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


@dataclass
class _HeldOpen:
    loop_thread: int
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    armed: threading.Lock = field(default_factory=threading.Lock)
    on_loop: bool = False
    # The open, once let go, fails instead.
    fails: bool = False
    spools: list[Path] = field(default_factory=list)
    # Strong references, so only an explicit close counts as one.
    opened: list[io.BufferedWriter] = field(default_factory=list)
    recreated: bool = False


@pytest.fixture
async def hold() -> AsyncIterator[_HeldOpen]:
    # Async, so its teardown runs before the event loop's: the loop's shutdown
    # waits for the executor thread the hold parks.
    held = _HeldOpen(threading.get_ident())
    yield held
    held.release.set()


def _hold_open(monkeypatch: pytest.MonkeyPatch, hold: _HeldOpen, prefix: str) -> None:
    """Hold the first open of a ``prefix`` temp file for writing.

    The hold has no timeout: one that let go by itself would let the open finish
    before the cancel it is held for (issue #3600). The test's ``finally`` and the
    fixture's teardown release it. An open on the loop's own thread is recorded and
    not held, since nothing could release it then.
    """

    def _open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if mode != "wb" or not Path(file).name.startswith(prefix):
            return builtins.open(file, mode, *args, **kwargs)
        if not hold.armed.acquire(blocking=False):
            return builtins.open(file, mode, *args, **kwargs)
        hold.spools.append(Path(file))
        if threading.get_ident() == hold.loop_thread:
            hold.on_loop = True
        hold.entered.set()
        if not hold.on_loop:
            hold.release.wait()
        try:
            if hold.fails:
                raise OSError(errno.EIO, "injected open fault")
            out = builtins.open(file, "wb")
            hold.recreated = Path(file).exists()
            hold.opened.append(out)
            return out
        finally:
            hold.finished.set()

    for module in (fs, object_store):
        monkeypatch.setattr(module, "open", _open, raising=False)


async def _one_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


_Op = Callable[[], Awaitable[object]]
# Run while the open is still held, and once the operation's cleanup has settled.
_Check = Callable[[], None]


def _nothing() -> None:
    pass


@dataclass(frozen=True)
class _Site:
    name: str
    backend: str
    prefix: str
    # The operation under test, a check while its open is held, and a check once
    # everything it started has finished.
    arrange: Callable[
        [StorageHarness, CommunityId, ServerId], Awaitable[tuple[_Op, _Check, _Check]]
    ]


async def _write_snapshot(
    harness: StorageHarness, community: CommunityId, server: ServerId
) -> tuple[_Op, _Check, _Check]:
    storage = harness.storage
    assert isinstance(storage, fs.FsStorage)
    handle = await storage.begin_snapshot(community, server)
    (staging,) = (storage._server_root(community, server) / "incoming").iterdir()

    async def _as_the_snapshot_endpoint() -> None:
        # The data-plane edge aborts the handle on any failure, a cancel included.
        try:
            await storage.write_snapshot(handle, _one_chunk(b"x"))
        except BaseException:
            await storage.abort_snapshot(handle)
            raise

    def _staging_kept() -> None:
        # The open is staging work, so the abort leaves the staging to it (#3653).
        storage.sweep()
        assert staging.is_dir()
        assert storage._is_staging_active(staging)

    def _staging_reclaimed() -> None:
        assert not staging.exists()
        assert not storage._active_staging
        assert storage._staging_jobs == {}

    return _as_the_snapshot_endpoint, _staging_kept, _staging_reclaimed


async def _put_jar(
    harness: StorageHarness, _community: CommunityId, _server: ServerId
) -> tuple[_Op, _Check, _Check]:
    return lambda: harness.storage.put_jar(_one_chunk(b"jar")), _nothing, _nothing


async def _put_backup(
    harness: StorageHarness, community: CommunityId, server: ServerId
) -> tuple[_Op, _Check, _Check]:
    archive = gzip.compress(tar_bytes({"a.txt": b"a"}))
    return (
        lambda: harness.storage.put_backup(community, server, _one_chunk(archive)),
        _nothing,
        _nothing,
    )


async def _check_backup_health(
    harness: StorageHarness, community: CommunityId, server: ServerId
) -> tuple[_Op, _Check, _Check]:
    archive = gzip.compress(tar_bytes({"a.txt": b"a"}))
    key = await harness.storage.put_backup(community, server, _one_chunk(archive))
    return (
        lambda: harness.storage.check_backup_health(community, server, key),
        _nothing,
        _nothing,
    )


_SITES = [
    _Site("write_snapshot", "fs", ".snapshot.", _write_snapshot),
    _Site("put_jar", "fs", ".jar.", _put_jar),
    _Site("put_backup", "fs", ".backup.", _put_backup),
    # The object adapter spools through the same helper.
    _Site("put_jar", "object", ".jar.", _put_jar),
    _Site("check_backup_health", "object", ".fsck.", _check_backup_health),
]


async def _until(predicate: Callable[[], bool]) -> None:
    # A bound, not a delay: a close or removal can wait behind a queued fsync on a
    # loaded host (issue #3624). Each caller asserts the predicate right after.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 120
    while not predicate() and loop.time() < deadline:
        await asyncio.sleep(0.01)


def _settled(hold: _HeldOpen, spool: Path, check: _Check) -> bool:
    if not all(out.closed for out in hold.opened) or os.path.exists(spool):
        return False
    try:
        check()
    except AssertionError:
        return False
    return True


@pytest.mark.parametrize("site", _SITES, ids=lambda site: f"{site.backend}-{site.name}")
async def test_a_cancelled_open_neither_blocks_the_loop_nor_leaves_its_spool(
    site: _Site, hold: _HeldOpen, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = build_harness(site.backend, tmp_path)
    community, server = new_scope()
    op, while_held, once_settled = await site.arrange(harness, community, server)
    _hold_open(monkeypatch, hold, site.prefix)

    task = asyncio.ensure_future(op())
    try:
        # A bound, not a delay: the spool's creation can stall on a loaded host.
        assert await asyncio.to_thread(hold.entered.wait, 120)
        assert not hold.on_loop, "the spool was opened on the event loop"
        [spool] = hold.spools

        # The loop runs the cancel through while the open is still held.
        task.cancel()
        await asyncio.wait({task}, timeout=10)
        assert task.cancelled()
        assert not hold.finished.is_set()
        assert not spool.exists()  # the cancelled caller removed its spool
        await asyncio.to_thread(while_held)
    finally:
        hold.release.set()

    assert await asyncio.to_thread(hold.finished.wait, 120)
    assert hold.recreated  # the late open recreated the removed spool
    [out] = hold.opened
    await _until(lambda: _settled(hold, spool, once_settled))
    assert out.closed
    assert not spool.exists()
    once_settled()


async def test_a_cancelled_open_that_fails_still_hands_back_its_staging(
    hold: _HeldOpen, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The late open fails: no file to close, and the staging it was counted
    against is still removed and its lease released (issue #3653)."""

    harness = build_harness("fs", tmp_path)
    community, server = new_scope()
    op, while_held, once_settled = await _write_snapshot(harness, community, server)
    _hold_open(monkeypatch, hold, ".snapshot.")
    hold.fails = True

    task = asyncio.ensure_future(op())
    try:
        assert await asyncio.to_thread(hold.entered.wait, 120)  # a bound, as above
        assert not hold.on_loop, "the spool was opened on the event loop"
        task.cancel()
        await asyncio.wait({task}, timeout=10)
        assert task.cancelled()
        await asyncio.to_thread(while_held)
    finally:
        hold.release.set()

    assert await asyncio.to_thread(hold.finished.wait, 120)
    assert hold.opened == []
    [spool] = hold.spools
    await _until(lambda: _settled(hold, spool, once_settled))
    assert not spool.exists()
    once_settled()
