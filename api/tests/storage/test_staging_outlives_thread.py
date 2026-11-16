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
import builtins
import concurrent.futures
import contextlib
import io
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import pytest

from mc_server_dashboard_api.storage.adapters import fs
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.errors import (
    IntegrityCheckError,
    MissingRegionsError,
    PrunedStoreError,
    PublisherFencedError,
    StaleGenerationError,
)
from mc_server_dashboard_api.storage.domain.port import SnapshotHandle
from mc_server_dashboard_api.storage.domain.value_objects import (
    BackupKey,
    CommunityId,
    ServerId,
)
from mc_server_dashboard_api.storage.integrity.region import check_working_set
from tests.storage.helpers import (
    corrupt_region_bytes,
    drain,
    healthy_region_bytes,
    new_scope,
    publish,
    read_tar,
    tar_stream,
)


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


@dataclass(frozen=True)
class _SpoolCase:
    """How a cancelled ``write_snapshot`` leaves its spool's file in use (#3653)."""

    chunk: bytes
    # The stream stalls after its chunk, so the cancel lands between writes.
    stalls: bool
    # The spool's raw calls held, in the order the file reaches them.
    holds: tuple[str, ...]


# Past the writer's 8 KiB buffer, so the write job hands it to the raw file.
_LARGE = bytes(64 * 1024)

_SPOOL_CASES = {
    "write": _SpoolCase(_LARGE, stalls=False, holds=("write",)),
    # The close that follows the cancelled write, chained from it.
    "write-then-close": _SpoolCase(_LARGE, stalls=False, holds=("write", "close")),
    # Abandoned with no write in flight.
    "close-between-writes": _SpoolCase(b"x", stalls=True, holds=("close",)),
    # The close of a clean exit, its caller cancelled while it runs.
    "awaited-close": _SpoolCase(b"x", stalls=False, holds=("close",)),
}


def _pausing_spool(
    mp: pytest.MonkeyPatch, write: _Pause | None, close: _Pause | None
) -> None:
    """Open the snapshot spool on a raw file whose first write or close is held."""

    raw_write = write.once(io.FileIO.write) if write else io.FileIO.write
    raw_close = close.once(io.FileIO.close) if close else io.FileIO.close

    class _PausedRaw(io.FileIO):
        def write(self, data: Any) -> int:
            return raw_write(self, data)

        def close(self) -> None:
            raw_close(self)

    def _open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if mode == "wb" and Path(file).name.startswith(".snapshot."):
            return io.BufferedWriter(_PausedRaw(file, "wb"))
        return builtins.open(file, mode, *args, **kwargs)

    mp.setattr(fs, "open", _open, raising=False)


# Sequential waits, worst case: the first arrival (120 s), the cancel (5 s), the
# second hold's arrival (120 s) and the release (120 s), plus 30 s of headroom
# (issue #3651).
@pytest.mark.timeout(3 * 120 + 5 + 30)
@pytest.mark.parametrize("case", _SPOOL_CASES.values(), ids=_SPOOL_CASES.keys())
async def test_an_aborted_transfer_keeps_its_staging_until_its_spool_is_closed(
    case: _SpoolCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    handle = await storage.begin_snapshot(community, server)
    staging = _only_staging(storage, community, server)
    released = _watch_release(storage, monkeypatch)
    pauses = {name: _Pause() for name in case.holds}
    _pausing_spool(monkeypatch, pauses.get("write"), pauses.get("close"))
    stalled = asyncio.Event()

    async def _stream() -> AsyncIterator[bytes]:
        yield case.chunk
        if case.stalls:
            stalled.set()
            await asyncio.Event().wait()  # until the cancel

    async def _as_the_snapshot_endpoint() -> None:
        # The data-plane edge aborts the handle on any failure, a cancel included.
        try:
            await storage.write_snapshot(handle, _stream())
        except BaseException:
            await storage.abort_snapshot(handle)
            raise

    task = asyncio.ensure_future(_as_the_snapshot_endpoint())
    try:
        if case.stalls:
            await asyncio.wait_for(stalled.wait(), 120)
        else:
            assert await asyncio.to_thread(pauses[case.holds[0]].entered.wait, 120)
        await _cancel_promptly(task)

        for name in case.holds:
            pause = pauses[name]
            assert await asyncio.to_thread(pause.entered.wait, 120), name
            await asyncio.to_thread(storage.sweep)
            assert staging.is_dir(), name
            assert storage._is_staging_active(staging), name
            assert not released.is_set(), name
            pause.resume.set()
    finally:
        # No timeout on the holds (issue #3600): this releases every one of them.
        for pause in pauses.values():
            pause.resume.set()

    assert await asyncio.to_thread(released.wait, 120)
    assert not staging.exists()
    assert not storage._is_staging_active(staging)
    assert storage._staging_jobs == {}


# A refused commit removes its staging (#3664). The commit's arrange step returns
# the handle to commit, the staging and the commit's keyword arguments.
_Refused = tuple[SnapshotHandle, Path, dict[str, Any]]
_Arrange = Callable[[FsStorage, CommunityId, ServerId], Awaitable[_Refused]]

_REGION = "world/region/r.0.0.mca"


async def _staged(
    storage: FsStorage,
    community: CommunityId,
    server: ServerId,
    files: dict[str, bytes],
) -> tuple[SnapshotHandle, Path]:
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(files))
    return handle, _only_staging(storage, community, server)


async def _pruned(storage: FsStorage, c: CommunityId, s: ServerId) -> _Refused:
    await publish(storage, c, s, {"a.txt": b"OLD"})
    base = await storage.current_generation(c, s)
    handle, staging = await _staged(storage, c, s, {"a.txt": b"NEW"})

    def _prune() -> None:
        with storage._server_lock(c, s):
            storage._prune_to_final_snapshot_inner(c, s)

    await asyncio.to_thread(_prune)
    # The prune empties the staging before it removes it (issue #3481): caught
    # midway, the commit finds it empty and asks whether a prune ran.
    staging.mkdir(parents=True)
    return handle, staging, {"expected_base": base}


async def _corrupt(storage: FsStorage, c: CommunityId, s: ServerId) -> _Refused:
    await publish(storage, c, s, {_REGION: healthy_region_bytes()})
    handle, staging = await _staged(storage, c, s, {_REGION: corrupt_region_bytes()})
    return handle, staging, {}


async def _stale(storage: FsStorage, c: CommunityId, s: ServerId) -> _Refused:
    await publish(storage, c, s, {"a.txt": b"OLD"})
    base = await storage.current_generation(c, s)
    handle, staging = await _staged(storage, c, s, {"a.txt": b"NEW"})
    await publish(storage, c, s, {"a.txt": b"NEWER"})
    return handle, staging, {"expected_base": base}


async def _missing_regions(storage: FsStorage, c: CommunityId, s: ServerId) -> _Refused:
    region = healthy_region_bytes()
    await publish(storage, c, s, {_REGION: region, "world/region/r.0.1.mca": region})
    handle, staging = await _staged(storage, c, s, {_REGION: region})
    return handle, staging, {}


async def _refusing_fence() -> bool:
    return False


async def _fenced(storage: FsStorage, c: CommunityId, s: ServerId) -> _Refused:
    await publish(storage, c, s, {"a.txt": b"OLD"})
    handle, staging = await _staged(storage, c, s, {"a.txt": b"NEW"})
    return handle, staging, {"publisher_fence": _refusing_fence}


_REFUSALS: dict[str, tuple[_Arrange, type[Exception]]] = {
    "pruned": (_pruned, PrunedStoreError),
    "integrity": (_corrupt, IntegrityCheckError),
    "stale": (_stale, StaleGenerationError),
    "missing-regions": (_missing_regions, MissingRegionsError),
    "fenced": (_fenced, PublisherFencedError),
}


def _holding_removal_of(staging: Path, pause: _Pause, mp: pytest.MonkeyPatch) -> None:
    """Hold the first removal of ``staging``; any other removal runs as is.

    The prune, the sweep and the superseded snapshot's reclaim remove paths too.
    """

    rmtree = fs._rmtree
    held = pause.once(rmtree)

    def _rmtree(path: Path) -> None:
        (held if path == staging else rmtree)(path)

    mp.setattr(fs, "_rmtree", _rmtree)


@pytest.mark.timeout(2 * 120 + 5 + 30)
@pytest.mark.parametrize("arrange", [a for a, _ in _REFUSALS.values()], ids=_REFUSALS)
async def test_a_refusal_cancelled_in_its_cleanup_releases_the_lease_after_the_removal(
    arrange: _Arrange, pause: _Pause, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    handle, staging, kwargs = await arrange(storage, community, server)
    released = _watch_release(storage, monkeypatch)
    _holding_removal_of(staging, pause, monkeypatch)
    task = asyncio.ensure_future(storage.commit_snapshot(handle, **kwargs))
    # A bound, not a delay: the commit fsyncs its staging before most refusals.
    assert await asyncio.to_thread(pause.entered.wait, 120)

    await _cancel_promptly(task)

    await _assert_staging_waits_for_the_thread(storage, staging, pause, released)
    assert storage._staging_jobs == {}


@pytest.mark.timeout(120 + 30)
@pytest.mark.parametrize(("arrange", "error"), _REFUSALS.values(), ids=_REFUSALS)
async def test_a_refusal_leaves_its_staging_to_a_job_still_using_it(
    arrange: _Arrange, error: type[Exception], tmp_path: Path
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    handle, staging, kwargs = await arrange(storage, community, server)
    # A job a cancelled step left running on the staging, as a spool's close.
    job: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    storage._count_on_staging(staging, job)
    try:
        with pytest.raises(error):
            await storage.commit_snapshot(handle, **kwargs)

        await asyncio.to_thread(storage.sweep)
        assert staging.is_dir()
        assert storage._is_staging_active(staging)
    finally:
        job.set_result(None)

    async def _removed() -> None:
        while storage._is_staging_active(staging):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_removed(), 120)  # a bound, not a delay
    assert not staging.exists()
    assert storage._staging_jobs == {}


@pytest.mark.parametrize(("arrange", "error"), _REFUSALS.values(), ids=_REFUSALS)
async def test_a_refusal_whose_cleanup_fails_still_raises_the_refusal(
    arrange: _Arrange,
    error: type[Exception],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    handle, staging, kwargs = await arrange(storage, community, server)
    rmtree = fs._rmtree

    def _failing(path: Path) -> None:
        if path == staging:
            raise OSError("removal fault")
        rmtree(path)

    monkeypatch.setattr(fs, "_rmtree", _failing)

    # The sweep reclaims a staging its cleanup could not remove.
    with pytest.raises(error):
        await storage.commit_snapshot(handle, **kwargs)

    assert not storage._is_staging_active(staging)
    assert "the sweep reclaims it" in caplog.text


def _count_releases(storage: FsStorage, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """The staging leases released, one entry per ``_release_staging`` call."""

    calls: list[Path] = []
    release_staging = storage._release_staging

    def _releasing(staging: Path) -> None:
        calls.append(staging)
        release_staging(staging)

    monkeypatch.setattr(storage, "_release_staging", _releasing)
    return calls


def _holding_each_removal_of(
    staging: Path,
    pauses: list[_Pause],
    mp: pytest.MonkeyPatch,
    *,
    first_fails: bool = False,
) -> None:
    """Hold the n-th removal of ``staging`` on ``pauses[n]``; others run as is.

    With ``first_fails``, the first removal raises once it is let go.
    """

    rmtree = fs._rmtree
    calls = iter(range(len(pauses)))
    lock = threading.Lock()

    def _rmtree(path: Path) -> None:
        if path != staging:
            rmtree(path)
            return
        with lock:
            n = next(calls, None)
        if n is not None:
            pauses[n].entered.set()
            pauses[n].resume.wait()
            if first_fails and n == 0:
                raise OSError("removal fault")
        rmtree(path)

    mp.setattr(fs, "_rmtree", _rmtree)


def _removal_pending(storage: FsStorage) -> bool:
    return bool(
        storage._staging_removers
        or storage._staging_jobs
        or storage._staging_removals_due
    )


async def _settle_deferred_removals(storage: FsStorage) -> None:
    while _removal_pending(storage):
        await asyncio.sleep(0.01)


@pytest.mark.timeout(5 * 120 + 30)
async def test_an_abort_during_a_removal_leaves_the_lease_to_the_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream({"a.txt": b"NEW"}))
    staging = _only_staging(storage, community, server)
    releases = _count_releases(storage, monkeypatch)
    first, rerun = _Pause(), _Pause()
    _holding_each_removal_of(staging, [first, rerun], monkeypatch)
    try:
        removing = asyncio.ensure_future(storage.abort_snapshot(handle))
        assert await asyncio.to_thread(first.entered.wait, 120)
        # The second abort finds the removal running and leaves it the removal.
        await asyncio.wait_for(storage.abort_snapshot(handle), 120)
        first.resume.set()
        await asyncio.wait_for(removing, 120)
        assert await asyncio.to_thread(rerun.entered.wait, 120)

        # The rerun owns the lease while it removes the staging.
        await asyncio.to_thread(storage.sweep)
        assert storage._is_staging_active(staging)
        assert releases == []
    finally:
        # No timeout on the holds (issue #3600): this releases every one of them.
        first.resume.set()
        rerun.resume.set()

    await asyncio.wait_for(_settle_deferred_removals(storage), 120)
    assert releases == [staging]
    assert not staging.exists()
    assert not storage._is_staging_active(staging)
    assert storage._staging_jobs == {}


@pytest.mark.timeout(5 * 120 + 30)
async def test_an_abort_during_a_failed_refusal_cleanup_leaves_the_lease_to_the_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    handle, staging, kwargs = await _corrupt(storage, community, server)
    releases = _count_releases(storage, monkeypatch)
    first, rerun = _Pause(), _Pause()
    _holding_each_removal_of(staging, [first, rerun], monkeypatch, first_fails=True)
    try:
        refusing = asyncio.ensure_future(storage.commit_snapshot(handle, **kwargs))
        assert await asyncio.to_thread(first.entered.wait, 120)
        await asyncio.wait_for(storage.abort_snapshot(handle), 120)
        first.resume.set()
        with pytest.raises(IntegrityCheckError):
            await asyncio.wait_for(refusing, 120)
        assert await asyncio.to_thread(rerun.entered.wait, 120)

        # The failed cleanup left the staging; the rerun still owns its lease.
        await asyncio.to_thread(storage.sweep)
        assert staging.is_dir()
        assert storage._is_staging_active(staging)
        assert releases == []
    finally:
        first.resume.set()
        rerun.resume.set()

    await asyncio.wait_for(_settle_deferred_removals(storage), 120)
    assert releases == [staging]
    assert not staging.exists()
    assert not storage._is_staging_active(staging)
    assert storage._staging_jobs == {}


def _hand_settled_removals_of(
    staging: Path, mp: pytest.MonkeyPatch
) -> list[asyncio.Future[None]]:
    """Each removal job of ``staging``, a loop future the test settles by hand.

    Settling two of them back to back ends both jobs in one loop iteration, a
    window no thread timing reaches reliably. Other jobs run as they are.
    """

    loop = asyncio.get_running_loop()
    run_in_executor = loop.run_in_executor
    jobs: list[asyncio.Future[None]] = []

    def _submit(executor: Any, fn: Callable[..., Any], *args: Any) -> Any:
        if fn is not fs._rmtree or args != (staging,):
            return run_in_executor(executor, fn, *args)
        jobs.append(loop.create_future())
        return jobs[-1]

    mp.setattr(loop, "run_in_executor", _submit)
    return jobs


def _registered_staging(storage: FsStorage, tmp_path: Path) -> Path:
    staging = tmp_path / "incoming" / "staging"
    staging.mkdir(parents=True)
    storage._register_staging(staging)
    return staging


def _assert_released_once(
    storage: FsStorage, staging: Path, releases: list[Path]
) -> None:
    assert releases == [staging]
    assert not storage._is_staging_active(staging)
    assert storage._staging_jobs == {}
    assert storage._staging_removers == {}
    assert storage._staging_removals_due == set()


@pytest.mark.timeout(10)
async def test_overlapping_removals_whose_jobs_end_together_release_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path)
    staging = _registered_staging(storage, tmp_path)
    releases = _count_releases(storage, monkeypatch)
    jobs = _hand_settled_removals_of(staging, monkeypatch)
    # Two removals under way at once, as when a discard lands after one removal's
    # job has ended, before that removal resumes.
    removals = [
        asyncio.ensure_future(storage._remove_staging(staging, raise_fault=False))
        for _ in range(2)
    ]
    await asyncio.sleep(0)
    assert len(jobs) == 2
    assert storage._staging_removers == {staging: 2}

    for job in jobs:
        job.set_result(None)
    await asyncio.gather(*removals)

    _assert_released_once(storage, staging, releases)


@pytest.mark.timeout(10)
async def test_a_job_ending_with_a_removal_leaves_the_due_removal_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path)
    staging = _registered_staging(storage, tmp_path)
    releases = _count_releases(storage, monkeypatch)
    jobs = _hand_settled_removals_of(staging, monkeypatch)
    removing = asyncio.ensure_future(
        storage._discard_staging(staging, raise_fault=False)
    )
    await asyncio.sleep(0)
    assert len(jobs) == 1
    # Another job begins on the staging, and a discard marks a removal due.
    other: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    storage._count_on_staging(staging, other)
    await storage._discard_staging(staging, raise_fault=False)
    assert storage._staging_removals_due == {staging}

    # The removal's job and the other job end in one loop iteration.
    jobs[0].set_result(None)
    other.set_result(None)
    await removing

    # The rerun owns the lease until its own job ends.
    await asyncio.sleep(0)
    assert len(jobs) == 2
    assert storage._is_staging_active(staging)
    assert releases == []
    jobs[1].set_result(None)
    await _settle_deferred_removals(storage)

    _assert_released_once(storage, staging, releases)


def _cancel_other_tasks() -> None:
    """Cancel every task but the test's, as the loop's teardown does."""

    for task in asyncio.all_tasks() - {asyncio.current_task()}:
        task.cancel()


async def _defer_removal(storage: FsStorage, staging: Path) -> asyncio.Future[None]:
    """A job on ``staging`` whose end owes its removal, as a cancelled step's."""

    job: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    storage._count_on_staging(staging, job)
    await storage._discard_staging(staging, raise_fault=False)
    assert storage._staging_removals_due == {staging}
    return job


async def _finish_removals(
    storage: FsStorage, staging: Path, jobs: list[asyncio.Future[None]]
) -> None:
    """Remove the staging for each hand-settled removal job, until none is owed."""

    while True:
        for job in jobs:
            if not job.done():
                fs._rmtree(staging)
                job.set_result(None)
        # Timed, so a task created by a callback takes its first step meanwhile.
        await asyncio.sleep(0.01)
        if not _removal_pending(storage) and all(job.done() for job in jobs):
            return


@pytest.mark.timeout(10)
async def test_a_teardown_as_a_deferred_removal_is_owed_still_removes_and_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path)
    staging = _registered_staging(storage, tmp_path)
    releases = _count_releases(storage, monkeypatch)
    jobs = _hand_settled_removals_of(staging, monkeypatch)
    job = await _defer_removal(storage, staging)
    job.set_result(None)
    await asyncio.sleep(0)  # the job's end owes the removal

    # Every task is cancelled before the next takes a step (issue #3675).
    _cancel_other_tasks()
    await asyncio.sleep(0)
    assert storage._is_staging_active(staging)
    assert storage._staging_removers == {staging: 1}

    await _finish_removals(storage, staging, jobs)
    assert not staging.exists()
    _assert_released_once(storage, staging, releases)


@pytest.mark.timeout(10)
async def test_a_teardown_during_a_deferred_removal_still_removes_and_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path)
    staging = _registered_staging(storage, tmp_path)
    releases = _count_releases(storage, monkeypatch)
    jobs = _hand_settled_removals_of(staging, monkeypatch)
    job = await _defer_removal(storage, staging)
    job.set_result(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(jobs) == 1

    # Cancelled while the removal's job runs, then again as each removal is owed.
    _cancel_other_tasks()
    await asyncio.sleep(0)
    fs._rmtree(staging)
    jobs[0].set_result(None)
    await asyncio.sleep(0)
    _cancel_other_tasks()

    await _finish_removals(storage, staging, jobs)
    assert not staging.exists()
    _assert_released_once(storage, staging, releases)


@pytest.mark.timeout(10)
async def test_a_discard_as_a_rerun_is_owed_leaves_the_lease_to_the_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path)
    staging = _registered_staging(storage, tmp_path)
    releases = _count_releases(storage, monkeypatch)
    jobs = _hand_settled_removals_of(staging, monkeypatch)
    loop = asyncio.get_running_loop()
    submit = loop.run_in_executor
    refused: list[Path] = []
    refusing = False

    # A removal the discard submits finds the executor shut down, so it would end,
    # and release, before the rerun's (the residual window of #3664).
    def _refusing(executor: Any, fn: Callable[..., Any], *args: Any) -> Any:
        if refusing and fn is fs._rmtree:
            refused.append(args[0])
            raise RuntimeError("Executor shutdown has been called")
        return submit(executor, fn, *args)

    monkeypatch.setattr(loop, "run_in_executor", _refusing)
    removing = asyncio.ensure_future(
        storage._discard_staging(staging, raise_fault=False)
    )
    await asyncio.sleep(0)
    assert len(jobs) == 1
    await storage._discard_staging(staging, raise_fault=False)
    fs._rmtree(staging)
    jobs[0].set_result(None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)  # the removal ends, owing a rerun

    refusing = True
    await storage._discard_staging(staging, raise_fault=False)
    refusing = False
    assert releases == []
    assert refused == []
    assert storage._staging_removals_due == {staging}

    await removing
    await _finish_removals(storage, staging, jobs)
    assert not staging.exists()
    _assert_released_once(storage, staging, releases)


async def test_a_removal_owed_after_the_executor_shuts_down_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path)
    staging = _registered_staging(storage, tmp_path)
    (staging / "region.mca").write_bytes(b"x")
    releases = _count_releases(storage, monkeypatch)
    job = await _defer_removal(storage, staging)

    await asyncio.get_running_loop().shutdown_default_executor()
    job.set_result(None)
    await asyncio.wait_for(_settle_deferred_removals(storage), 10)

    assert not staging.exists()
    _assert_released_once(storage, staging, releases)


def _assert_removal_fault_logged(
    caplog: pytest.LogCaptureFixture, staging: Path
) -> None:
    faults = [
        record
        for record in caplog.records
        if record.getMessage()
        == f"removing the staging {staging} failed; the sweep reclaims it"
    ]
    assert len(faults) == 1
    assert faults[0].exc_info is not None
    assert isinstance(faults[0].exc_info[1], OSError)


@pytest.mark.timeout(10)
async def test_a_deferred_removal_whose_job_fails_still_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    storage = FsStorage(tmp_path)
    staging = _registered_staging(storage, tmp_path)
    releases = _count_releases(storage, monkeypatch)
    jobs = _hand_settled_removals_of(staging, monkeypatch)
    job = await _defer_removal(storage, staging)
    job.set_result(None)
    await asyncio.sleep(0)
    assert len(jobs) == 1

    jobs[0].set_exception(OSError("removal fault"))
    await asyncio.sleep(0)  # the job's settle, then the removal's end

    # The sweep reclaims a staging its removal could not remove.
    _assert_released_once(storage, staging, releases)
    _assert_removal_fault_logged(caplog, staging)


@pytest.mark.timeout(10)
async def test_a_removal_owed_after_the_executor_shuts_down_and_failing_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    storage = FsStorage(tmp_path)
    staging = _registered_staging(storage, tmp_path)
    releases = _count_releases(storage, monkeypatch)
    job = await _defer_removal(storage, staging)
    await asyncio.get_running_loop().shutdown_default_executor()

    def _failing(path: Path) -> None:
        raise OSError("removal fault")

    monkeypatch.setattr(fs, "_rmtree", _failing)
    job.set_result(None)
    await asyncio.sleep(0)  # the job's end owes the removal, run on the loop

    # The sweep reclaims a staging its removal could not remove.
    assert staging.exists()
    _assert_released_once(storage, staging, releases)
    _assert_removal_fault_logged(caplog, staging)


@pytest.mark.timeout(120 + 30)
def test_a_removal_owed_as_the_loop_closes_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path)
    staging = _registered_staging(storage, tmp_path)
    (staging / "region.mca").write_bytes(b"x")
    releases = _count_releases(storage, monkeypatch)

    async def _main() -> None:
        # The removal is owed as the loop finishes, before it takes a turn.
        job = await _defer_removal(storage, staging)
        job.set_result(None)

    asyncio.run(_main())

    assert not staging.exists()
    _assert_released_once(storage, staging, releases)
