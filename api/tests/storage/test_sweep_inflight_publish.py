"""A sweep never reclaims the snapshot a publish is still filling (issue #3893).

A whole-working-set publish -- a snapshot commit or a backup restore -- fills a
fresh ``snapshots/<id>/`` before it flips the pointer onto it (STORAGE.md Section
4.2). The object adapter server-side copies every staged object there, which can
take minutes; the fs adapter moves the staged tree there and fsyncs
``snapshots/``. Until the flip no pointer names the new snapshot, so the sweep
used to reclaim it as an orphan, and the publish then flipped onto what was left
and reported success.

Each test holds the publish in that window on an event (a pausing fake client on
the object backend, a pausing failure seam on fs), runs a sweep, and lets the
publish finish. No test sleeps.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import mc_server_dashboard_api.storage.adapters.fs as fs_mod
import mc_server_dashboard_api.storage.adapters.object_store as object_store_mod
from mc_server_dashboard_api.storage.adapters.failure_seam import (
    FailureSeam,
    PublishPhase,
)
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.adapters.object_store import (
    ObjectStorage,
    S3Client,
    S3ClientFactory,
)
from mc_server_dashboard_api.storage.domain.port import Storage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId,
    ServerId,
)
from tests.storage.fake_s3 import FakeS3Client, FakeS3Store, fake_s3_factory
from tests.storage.helpers import (
    drain,
    new_scope,
    read_tar,
    snapshot_dir,
    tar_stream,
)

OLD = {"a": b"OLD-a", "b": b"OLD-b"}
NEW = {"a": b"NEW-a", "b": b"NEW-b"}
BACKED_UP = {"a": b"BACKUP-a", "b": b"BACKUP-b"}

# Upper bound on any one cross-thread event wait: a hang fails the test instead of
# blocking the run. Never a pause the test relies on.
_WAIT_S = 60


async def _publish(
    storage: Storage,
    community: CommunityId,
    server: ServerId,
    files: dict[str, bytes],
) -> None:
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(files))
    await storage.commit_snapshot(handle)


async def _hydrate(
    storage: Storage, community: CommunityId, server: ServerId
) -> dict[str, bytes]:
    return read_tar(await drain(storage.open_hydrate_source(community, server)))


# --- object backend --------------------------------------------------------


class _CopyGate:
    """Holds the object adapter's snapshot copy after chosen copies landed.

    Copies are counted only once a stop is set, so an arrange-step publish runs
    through untouched.
    """

    def __init__(self) -> None:
        self._stops: dict[int, tuple[asyncio.Event, asyncio.Event]] = {}
        self._copies = 0

    def stop_after(self, copies: int) -> tuple[asyncio.Event, asyncio.Event]:
        """Return (paused, resume) for a stop after the ``copies``-th copy."""

        stop = (asyncio.Event(), asyncio.Event())
        self._stops[copies] = stop
        return stop

    def factory(self, store: FakeS3Store) -> S3ClientFactory:
        gate = self

        class _Client(FakeS3Client):
            async def copy_object(self, src_key: str, dst_key: str) -> None:
                await super().copy_object(src_key, dst_key)
                if not gate._stops or "/snapshots/" not in dst_key:
                    return
                gate._copies += 1
                stop = gate._stops.get(gate._copies)
                if stop is not None:
                    paused, resume = stop
                    paused.set()
                    await resume.wait()

        @asynccontextmanager
        async def _factory() -> AsyncIterator[S3Client]:
            yield _Client(store)

        return _factory


def _server_prefix(community: CommunityId, server: ServerId) -> str:
    return f"communities/{community.value}/servers/{server.value}/"


def _unpointed_snapshot_keys(
    store: FakeS3Store, community: CommunityId, server: ServerId
) -> set[str]:
    """Every object under a ``snapshots/<id>/`` prefix the pointer does not name."""

    prefix = _server_prefix(community, server)
    live = json.loads(store.objects[prefix + "current.json"])["snapshot"]
    return {
        key
        for key in store.objects
        if key.startswith(prefix + "snapshots/") and not key.startswith(live)
    }


def _marker_of(keys: set[str], community: CommunityId, server: ServerId) -> str:
    """The in-flight marker key of the one snapshot ``keys`` lie under."""

    (snapshot_id,) = {key.split("/")[5] for key in keys}
    return _server_prefix(community, server) + f"inflight/{snapshot_id}"


def _past_ttl() -> dt.datetime:
    ttl = dt.timedelta(seconds=fs_mod._INFLIGHT_MARKER_TTL_S)
    return dt.datetime.now(dt.UTC) - ttl - dt.timedelta(minutes=1)


async def test_object_sweep_during_a_commit_copy_keeps_the_new_snapshot() -> None:
    """The #3873 review probe: a sweep lands after the first of two copies."""

    store = FakeS3Store()
    gate = _CopyGate()
    storage = ObjectStorage(gate.factory(store))
    community, server = new_scope()
    await _publish(storage, community, server, OLD)
    paused, resume = gate.stop_after(1)
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(NEW))
    commit = asyncio.create_task(storage.commit_snapshot(handle))
    await paused.wait()
    copied = _unpointed_snapshot_keys(store, community, server)

    await storage.sweep()
    kept = copied <= set(store.objects)
    resume.set()
    await commit

    assert copied
    assert kept
    assert await _hydrate(storage, community, server) == NEW


async def test_object_sweep_during_a_restore_copy_keeps_the_new_snapshot() -> None:
    store = FakeS3Store()
    gate = _CopyGate()
    storage = ObjectStorage(gate.factory(store))
    community, server = new_scope()
    await _publish(storage, community, server, BACKED_UP)
    key = await storage.create_backup_from_current(community, server)
    await _publish(storage, community, server, OLD)
    paused, resume = gate.stop_after(1)
    restore = asyncio.create_task(storage.restore_backup(community, server, key))
    await paused.wait()
    copied = _unpointed_snapshot_keys(store, community, server)

    await storage.sweep()
    kept = copied <= set(store.objects)
    resume.set()
    await restore

    assert copied
    assert kept
    assert await _hydrate(storage, community, server) == BACKED_UP


async def test_object_sweep_keeps_its_own_copy_past_the_marker_ttl() -> None:
    """The owning process pins its copy however long it runs, marker age aside."""

    store = FakeS3Store()
    gate = _CopyGate()
    storage = ObjectStorage(gate.factory(store))
    community, server = new_scope()
    await _publish(storage, community, server, OLD)
    paused, resume = gate.stop_after(1)
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(NEW))
    commit = asyncio.create_task(storage.commit_snapshot(handle))
    await paused.wait()
    copied = _unpointed_snapshot_keys(store, community, server)
    store.mtimes[_marker_of(copied, community, server)] = _past_ttl()

    await storage.sweep()
    kept = copied <= set(store.objects)
    resume.set()
    await commit

    assert kept
    assert await _hydrate(storage, community, server) == NEW


async def test_object_sweep_of_another_process_honours_the_marker() -> None:
    """A second adapter over the same bucket sees the marker, not the memory pin.

    The stop is after the last copy: that adapter's sweep also removes the
    in-flight staging, whose pin is in process only.
    """

    store = FakeS3Store()
    gate = _CopyGate()
    storage = ObjectStorage(gate.factory(store))
    other = ObjectStorage(fake_s3_factory(store))
    community, server = new_scope()
    await _publish(storage, community, server, OLD)
    paused, resume = gate.stop_after(len(NEW))
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(NEW))
    commit = asyncio.create_task(storage.commit_snapshot(handle))
    await paused.wait()
    copied = _unpointed_snapshot_keys(store, community, server)
    marker = _marker_of(copied, community, server)
    marked = marker in store.objects

    await other.sweep()
    kept = copied <= set(store.objects)
    resume.set()
    await commit

    assert marked
    assert kept
    assert await _hydrate(storage, community, server) == NEW
    assert marker not in store.objects


async def test_object_copy_renews_its_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """A copy that outlasts the TTL keeps its marker fresh for other processes."""

    monkeypatch.setattr(object_store_mod, "_INFLIGHT_MARKER_RENEW_S", 0)
    store = FakeS3Store()
    gate = _CopyGate()
    storage = ObjectStorage(gate.factory(store))
    other = ObjectStorage(fake_s3_factory(store))
    community, server = new_scope()
    await _publish(storage, community, server, OLD)
    stalled, carry_on = gate.stop_after(1)
    paused, resume = gate.stop_after(len(NEW))
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(NEW))
    commit = asyncio.create_task(storage.commit_snapshot(handle))
    await stalled.wait()
    marker = _marker_of(
        _unpointed_snapshot_keys(store, community, server), community, server
    )
    store.mtimes[marker] = _past_ttl()
    carry_on.set()
    await paused.wait()
    copied = _unpointed_snapshot_keys(store, community, server)

    await other.sweep()
    kept = copied <= set(store.objects)
    resume.set()
    await commit

    assert kept
    assert await _hydrate(storage, community, server) == NEW


async def test_object_sweep_reads_the_marker_before_the_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A publish drops its marker only after its flip.

    So a sweep that finds no marker and then reads the pointer sees the flip.
    Here the publish completes exactly while the sweep checks the marker: read
    the other way round, the sweep would hold a pre-flip pointer and find the
    marker already gone.
    """

    store = FakeS3Store()
    gate = _CopyGate()
    storage = ObjectStorage(gate.factory(store))
    other = ObjectStorage(fake_s3_factory(store))
    community, server = new_scope()
    await _publish(storage, community, server, OLD)
    paused, resume = gate.stop_after(len(NEW))
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(NEW))
    commit = asyncio.create_task(storage.commit_snapshot(handle))
    await paused.wait()
    marker = _marker_of(
        _unpointed_snapshot_keys(store, community, server), community, server
    )
    real = object_store_mod._is_fresh_marker

    async def _publish_completes_meanwhile(client: S3Client, key: str) -> bool:
        if key == marker and not commit.done():
            resume.set()
            await commit
        return await real(client, key)

    monkeypatch.setattr(
        object_store_mod, "_is_fresh_marker", _publish_completes_meanwhile
    )

    await other.sweep()

    assert commit.done()
    assert await _hydrate(storage, community, server) == NEW


async def test_object_stale_marker_expires_and_its_orphan_is_reclaimed() -> None:
    """A marker its process never removed pins its snapshot only for the TTL."""

    store = FakeS3Store()
    storage = ObjectStorage(fake_s3_factory(store))
    community, server = new_scope()
    await _publish(storage, community, server, OLD)
    prefix = _server_prefix(community, server)
    # A commit whose process died mid-copy, and one that died before copying.
    orphan = prefix + "snapshots/deadbeef/a"
    marker = prefix + "inflight/deadbeef"
    lone_marker = prefix + "inflight/cafebabe"
    leftovers = {orphan, marker, lone_marker}
    client = FakeS3Client(store)
    for key in leftovers:
        await client.put_object(key, b"x")

    await storage.sweep()
    pinned = leftovers <= set(store.objects)
    store.mtimes[marker] = store.mtimes[lone_marker] = _past_ttl()
    await storage.sweep()

    assert pinned
    assert not leftovers & set(store.objects)
    assert await _hydrate(storage, community, server) == OLD


# --- fs backend ------------------------------------------------------------


class _PauseAt(FailureSeam):
    """Holds the publish thread at ``phase`` once armed, until ``resume`` is set."""

    def __init__(self, phase: PublishPhase) -> None:
        self._phase = phase
        self.armed = False
        self.reached = threading.Event()
        self.resume = threading.Event()

    def reach(self, phase: PublishPhase) -> None:
        if self.armed and phase is self._phase:
            self.armed = False
            self.reached.set()
            assert self.resume.wait(_WAIT_S)


async def _wait(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, _WAIT_S)


def _moved_snapshot(live: Path) -> Path:
    """The one snapshot directory beside the live one: the in-flight publish's."""

    (moved,) = [p for p in live.parent.iterdir() if p != live]
    return moved


def _backdate(path: Path) -> None:
    then = time.time() - fs_mod._INFLIGHT_MARKER_TTL_S - 60
    os.utime(path, (then, then))


async def _fs_commit_held_after_move(
    tmp_path: Path,
) -> tuple[FsStorage, _PauseAt, CommunityId, ServerId, asyncio.Task[int], Path]:
    """Publish OLD, then hold a commit of NEW between its move and its flip."""

    seam = _PauseAt(PublishPhase.AFTER_MOVE)
    storage = FsStorage(tmp_path, failure_seam=seam)
    community, server = new_scope()
    await _publish(storage, community, server, OLD)
    live = snapshot_dir(tmp_path, community, server)
    seam.armed = True
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(NEW))
    commit = asyncio.create_task(storage.commit_snapshot(handle))
    await _wait(seam.reached)
    return storage, seam, community, server, commit, _moved_snapshot(live)


def _names(directory: Path) -> set[str]:
    return {p.name for p in directory.iterdir()} if directory.is_dir() else set()


# 12 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(12 * 10 + 30)
async def test_fs_sweep_during_a_commit_move_keeps_the_new_snapshot(
    tmp_path: Path,
) -> None:
    storage, seam, community, server, commit, moved = await _fs_commit_held_after_move(
        tmp_path
    )

    await asyncio.to_thread(storage.sweep)
    kept = _names(moved)
    seam.resume.set()
    await commit

    assert kept == set(NEW)
    assert await _hydrate(storage, community, server) == NEW


# 18 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(18 * 10 + 30)
async def test_fs_sweep_during_a_restore_move_keeps_the_new_snapshot(
    tmp_path: Path,
) -> None:
    seam = _PauseAt(PublishPhase.AFTER_MOVE)
    storage = FsStorage(tmp_path, failure_seam=seam)
    community, server = new_scope()
    await _publish(storage, community, server, BACKED_UP)
    key = await storage.create_backup_from_current(community, server)
    await _publish(storage, community, server, OLD)
    live = snapshot_dir(tmp_path, community, server)
    seam.armed = True
    restore = asyncio.create_task(storage.restore_backup(community, server, key))
    await _wait(seam.reached)
    moved = _moved_snapshot(live)

    await asyncio.to_thread(storage.sweep)
    kept = _names(moved)
    seam.resume.set()
    await restore

    assert kept == set(BACKED_UP)
    assert await _hydrate(storage, community, server) == BACKED_UP


# 12 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(12 * 10 + 30)
async def test_fs_sweep_keeps_its_own_publish_past_the_marker_ttl(
    tmp_path: Path,
) -> None:
    storage, seam, community, server, commit, moved = await _fs_commit_held_after_move(
        tmp_path
    )
    _backdate(moved.parent.parent / "inflight" / moved.name)

    await asyncio.to_thread(storage.sweep)
    kept = _names(moved)
    seam.resume.set()
    await commit

    assert kept == set(NEW)
    assert await _hydrate(storage, community, server) == NEW


# 12 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(12 * 10 + 30)
async def test_fs_sweep_of_another_process_honours_the_marker(tmp_path: Path) -> None:
    storage, seam, community, server, commit, moved = await _fs_commit_held_after_move(
        tmp_path
    )
    marker = moved.parent.parent / "inflight" / moved.name
    marked = marker.is_file()

    await asyncio.to_thread(FsStorage(tmp_path).sweep)
    kept = _names(moved)
    seam.resume.set()
    await commit

    assert marked
    assert kept == set(NEW)
    assert await _hydrate(storage, community, server) == NEW
    assert not marker.exists()


# 12 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(12 * 10 + 30)
async def test_fs_sweep_reads_the_marker_before_the_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fs twin of the object test: the publish completes during the check."""

    storage, seam, community, server, commit, moved = await _fs_commit_held_after_move(
        tmp_path
    )
    marker = moved.parent.parent / "inflight" / moved.name
    published = threading.Event()
    commit.add_done_callback(lambda _: published.set())
    real = fs_mod._is_fresh_marker

    def _publish_completes_meanwhile(path: Path) -> bool:
        if path == marker and not published.is_set():
            seam.resume.set()
            assert published.wait(_WAIT_S)
        return real(path)

    monkeypatch.setattr(fs_mod, "_is_fresh_marker", _publish_completes_meanwhile)

    await asyncio.to_thread(FsStorage(tmp_path).sweep)
    seam.resume.set()
    await commit

    assert await _hydrate(storage, community, server) == NEW


# 6 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(6 * 10 + 30)
async def test_fs_stale_marker_expires_and_its_orphan_is_reclaimed(
    tmp_path: Path,
) -> None:
    """A marker its process never removed pins its snapshot only for the TTL."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await _publish(storage, community, server, OLD)
    server_root = snapshot_dir(tmp_path, community, server).parent.parent
    # A publish whose process died after its move, and one that died before it.
    orphan = server_root / "snapshots" / "deadbeef"
    orphan.mkdir()
    (orphan / "a").write_bytes(b"x")
    (server_root / "inflight").mkdir(exist_ok=True)
    marker = server_root / "inflight" / "deadbeef"
    lone_marker = server_root / "inflight" / "cafebabe"
    marker.touch()
    lone_marker.touch()

    storage.sweep()
    pinned = orphan.is_dir() and marker.exists() and lone_marker.exists()
    _backdate(marker)
    _backdate(lone_marker)
    storage.sweep()

    assert pinned
    assert not orphan.exists()
    assert not marker.exists()
    assert not lone_marker.exists()
    assert await _hydrate(storage, community, server) == OLD
