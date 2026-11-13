"""A staging dir outlives the threads using it (#3583).

Restore, the backup fsck and the snapshot ingest run their work on staging under
``incoming/`` on executor threads. Cancelling the caller does not stop such a
thread, so a cleanup that removes the staging as the caller unwinds pulls it from
under the thread, which can then fail spuriously, recreate paths after the
cleanup, or publish a half-removed tree.

Each case pauses the thread inside its work, cancels the caller (which must
re-raise ``CancelledError`` at once), and checks the staging survives and stays
pinned against the sweep while the thread is paused. Once the thread is let go,
the staging is removed and its active-staging lease released.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import pytest

from mc_server_dashboard_api.storage.adapters import fs
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.port import SnapshotHandle
from mc_server_dashboard_api.storage.domain.value_objects import (
    BackupKey,
    CommunityId,
    ServerId,
)
from mc_server_dashboard_api.storage.integrity.region import check_working_set
from tests.storage.helpers import drain, new_scope, publish, read_tar, tar_stream


@dataclass
class _Pause:
    entered: threading.Event = field(default_factory=threading.Event)
    resume: threading.Event = field(default_factory=threading.Event)

    def once(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """``fn``, holding its first call until ``resume`` is set.

        The hold has no timeout: one that let go by itself would let the thread
        finish and remove its staging while the test still relies on it, faking a
        lifetime bug (issue #3600). The fixture's teardown and the pytest timeout
        end a hold the test never resumes.
        """

        armed = threading.Lock()

        def _paused(*args: Any, **kwargs: Any) -> Any:
            if armed.acquire(blocking=False):
                self.entered.set()
                self.resume.wait()
            return fn(*args, **kwargs)

        return _paused


@pytest.fixture
async def pause() -> AsyncIterator[_Pause]:
    # Async, so its teardown runs before the event loop's: the loop's shutdown
    # waits for the executor thread a hold parks.
    held = _Pause()
    yield held
    held.resume.set()


def _watch_release(
    storage: FsStorage, monkeypatch: pytest.MonkeyPatch
) -> threading.Event:
    """An event set once a staging lease is released."""

    released = threading.Event()
    release_staging = storage._release_staging

    def _releasing(staging: Path) -> None:
        release_staging(staging)
        released.set()

    monkeypatch.setattr(storage, "_release_staging", _releasing)
    return released


_Arm = Callable[[FsStorage, _Pause, pytest.MonkeyPatch], None]


def _pausing(name: str) -> _Arm:
    """Pause the first call of the fs module function ``name``."""

    def _arm(storage: FsStorage, pause: _Pause, mp: pytest.MonkeyPatch) -> None:
        mp.setattr(fs, name, pause.once(getattr(fs, name)))

    return _arm


def _in_region_scan(storage: FsStorage, pause: _Pause, mp: pytest.MonkeyPatch) -> None:
    mp.setattr(fs, "check_working_set", pause.once(check_working_set))


def _in_publish(storage: FsStorage, pause: _Pause, mp: pytest.MonkeyPatch) -> None:
    mp.setattr(storage, "_publish", pause.once(storage._publish))


async def _cancel_promptly(task: asyncio.Future[Any]) -> None:
    task.cancel()
    await asyncio.wait({task}, timeout=5)
    assert task.cancelled()


async def _assert_staging_waits_for_the_thread(
    storage: FsStorage, staging: Path, pause: _Pause, released: threading.Event
) -> None:
    try:
        await asyncio.to_thread(storage.sweep)
        assert staging.is_dir()
        assert storage._is_staging_active(staging)
        assert not released.is_set()
    finally:
        pause.resume.set()

    # A bound, not a delay: the publish cases' fsyncs can stall on a loaded host.
    assert await asyncio.to_thread(released.wait, 120)
    assert not staging.exists()
    assert not storage._is_staging_active(staging)


def _only_staging(storage: FsStorage, community: CommunityId, server: ServerId) -> Path:
    (staging,) = (storage._server_root(community, server) / "incoming").iterdir()
    return staging


_BackupOp = Callable[[FsStorage, CommunityId, ServerId, BackupKey], Awaitable[object]]


async def _restore(
    storage: FsStorage, community: CommunityId, server: ServerId, key: BackupKey
) -> object:
    return await storage.restore_backup(community, server, key)


async def _check_health(
    storage: FsStorage, community: CommunityId, server: ServerId, key: BackupKey
) -> object:
    return await storage.check_backup_health(community, server, key)


# (id, op, arm, the world live once the thread finishes)
_BACKUP_OPS: list[tuple[str, _BackupOp, _Arm, dict[str, bytes]]] = [
    ("restore-extract", _restore, _pausing("_extract_tar_gz_into"), {"a.txt": b"NEW"}),
    ("restore-region-scan", _restore, _in_region_scan, {"a.txt": b"NEW"}),
    ("restore-fsync", _restore, _pausing("_fsync_tree"), {"a.txt": b"NEW"}),
    # The cancel arrives during the flip: the thread completes the publish.
    ("restore-publish", _restore, _in_publish, {"a.txt": b"OLD"}),
    (
        "check_backup_health-extract",
        _check_health,
        _pausing("_extract_tar_gz_into"),
        {"a.txt": b"NEW"},
    ),
    (
        "check_backup_health-region-scan",
        _check_health,
        _in_region_scan,
        {"a.txt": b"NEW"},
    ),
]


@pytest.mark.parametrize(
    ("op", "arm", "live"),
    [(o, a, w) for _, o, a, w in _BACKUP_OPS],
    ids=[n for n, _, _, _ in _BACKUP_OPS],
)
async def test_a_cancelled_backup_op_keeps_its_staging_until_its_thread_finishes(
    op: _BackupOp,
    arm: _Arm,
    live: dict[str, bytes],
    pause: _Pause,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"a.txt": b"OLD"})
    key = await storage.create_backup_from_current(community, server)
    await publish(storage, community, server, {"a.txt": b"NEW"})
    released = _watch_release(storage, monkeypatch)
    arm(storage, pause, monkeypatch)
    task = asyncio.ensure_future(op(storage, community, server, key))
    # A bound, not a delay: the later pauses wait behind the op's fsyncs, which
    # can stall on a loaded host (issue #3624).
    assert await asyncio.to_thread(pause.entered.wait, 120)
    staging = _only_staging(storage, community, server)

    await _cancel_promptly(task)

    await _assert_staging_waits_for_the_thread(storage, staging, pause, released)
    monkeypatch.undo()
    blob = await drain(storage.open_hydrate_source(community, server))
    assert read_tar(blob) == live


_HandleOp = Callable[[FsStorage, SnapshotHandle], Awaitable[object]]


async def _write(storage: FsStorage, handle: SnapshotHandle) -> object:
    await storage.write_snapshot(handle, tar_stream({"a.txt": b"NEW"}))
    return None


async def _commit(storage: FsStorage, handle: SnapshotHandle) -> object:
    return await storage.commit_snapshot(handle)


# (id, op, arm, the world live once the thread finishes)
_HANDLE_OPS: list[tuple[str, _HandleOp, _Arm, dict[str, bytes]]] = [
    ("write_snapshot-spool", _write, _pausing("_make_spool"), {"a.txt": b"OLD"}),
    (
        "write_snapshot-extract",
        _write,
        _pausing("_extract_tar_into"),
        {"a.txt": b"OLD"},
    ),
    (
        "commit_snapshot-entries",
        _commit,
        _pausing("_dir_has_entries"),
        {"a.txt": b"OLD"},
    ),
    ("commit_snapshot-region-scan", _commit, _in_region_scan, {"a.txt": b"OLD"}),
    ("commit_snapshot-fsync", _commit, _pausing("_fsync_tree"), {"a.txt": b"OLD"}),
    # The cancel arrives during the flip: the thread completes the publish.
    ("commit_snapshot-publish", _commit, _in_publish, {"a.txt": b"NEW"}),
]


@pytest.mark.parametrize(
    ("op", "arm", "live"),
    [(o, a, w) for _, o, a, w in _HANDLE_OPS],
    ids=[n for n, _, _, _ in _HANDLE_OPS],
)
async def test_an_aborted_transfer_keeps_its_staging_until_its_thread_finishes(
    op: _HandleOp,
    arm: _Arm,
    live: dict[str, bytes],
    pause: _Pause,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"a.txt": b"OLD"})
    handle = await storage.begin_snapshot(community, server)
    if op is _commit:
        await _write(storage, handle)
    staging = _only_staging(storage, community, server)
    released = _watch_release(storage, monkeypatch)
    arm(storage, pause, monkeypatch)

    async def _as_the_snapshot_endpoint() -> object:
        # The data-plane edge aborts the handle on any failure, a cancel included.
        try:
            return await op(storage, handle)
        except BaseException:
            await storage.abort_snapshot(handle)
            raise

    task = asyncio.ensure_future(_as_the_snapshot_endpoint())
    assert await asyncio.to_thread(pause.entered.wait, 120)  # a bound, as above

    await _cancel_promptly(task)

    await _assert_staging_waits_for_the_thread(storage, staging, pause, released)
    monkeypatch.undo()
    blob = await drain(storage.open_hydrate_source(community, server))
    assert read_tar(blob) == live


_T = TypeVar("_T")


class _FinishedAtSubmit(concurrent.futures.ThreadPoolExecutor):
    """Runs each job as it is submitted, so the job is complete before
    ``run_in_executor`` returns. The job's done-callback is then only queued when
    the caller's await resumes, as for a real job that ends that fast."""

    def submit(
        self, fn: Callable[..., _T], /, *args: Any, **kwargs: Any
    ) -> concurrent.futures.Future[_T]:
        future: concurrent.futures.Future[_T] = concurrent.futures.Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        return future


@pytest.fixture
async def finished_at_submit() -> None:
    asyncio.get_running_loop().set_default_executor(_FinishedAtSubmit())


@pytest.mark.parametrize("fails", [False, True], ids=["returns", "raises"])
@pytest.mark.usefixtures("finished_at_submit")
async def test_a_job_done_before_its_await_is_settled_before_the_caller_resumes(
    fails: bool, tmp_path: Path
) -> None:
    storage = FsStorage(tmp_path)
    staging = tmp_path / "incoming" / "staging"
    staging.mkdir(parents=True)
    storage._register_staging(staging)

    def _job() -> None:
        if fails:
            raise RuntimeError("job fault")

    with pytest.raises(RuntimeError) if fails else contextlib.nullcontext():
        await storage._run_on_staging(staging, _job)
    await storage._discard_staging(staging, raise_fault=True)

    assert not staging.exists()
    assert not storage._is_staging_active(staging)


def _raise_job_fault(_staging: Path) -> None:
    raise RuntimeError("region scan fault")


# (id, op, whether its region scan raises)
_FAST_OPS: list[tuple[str, _BackupOp, bool]] = [
    ("check_backup_health-returns", _check_health, False),
    ("check_backup_health-raises", _check_health, True),
    ("restore-raises", _restore, True),
]


@pytest.mark.parametrize(
    ("op", "fails"),
    [(o, f) for _, o, f in _FAST_OPS],
    ids=[n for n, _, _ in _FAST_OPS],
)
async def test_a_backup_op_whose_jobs_end_at_once_removes_its_staging_before_it_ends(
    op: _BackupOp,
    fails: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"a.txt": b"OLD"})
    key = await storage.create_backup_from_current(community, server)
    asyncio.get_running_loop().set_default_executor(_FinishedAtSubmit())
    if fails:
        monkeypatch.setattr(fs, "check_working_set", _raise_job_fault)

    with pytest.raises(RuntimeError) if fails else contextlib.nullcontext():
        await op(storage, community, server, key)

    incoming = storage._server_root(community, server) / "incoming"
    assert not incoming.exists() or not any(incoming.iterdir())
    assert not storage._active_staging
