"""A sweep never reclaims the snapshot a publish is still filling (issue #3893).

A whole-working-set publish -- a snapshot commit, a backup restore, or the first
file write to a server that has never published -- fills a fresh
``snapshots/<id>/`` before it flips the pointer onto it (STORAGE.md Section 4.2).
The object adapter server-side copies every staged object there, which can take
minutes, and then waits for the server lock and runs its gates; the fs adapter
moves the staged tree there and fsyncs ``snapshots/``. Until the flip no pointer
names the new snapshot, so the sweep used to reclaim it as an orphan, and the
publish then flipped onto what was left and reported success.

Each test holds the publish in that window on an event (a pausing fake client on
the object backend, a pausing failure seam on fs), runs a sweep, and lets the
publish finish. No test sleeps.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from mc_server_dashboard_api.storage.adapters.failure_seam import (
    FailureSeam,
    PublishPhase,
)
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.adapters.object_store import (
    ObjectStorage,
    S3Client,
    S3ClientFactory,
    S3Object,
)
from mc_server_dashboard_api.storage.domain.port import Storage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId,
    RelPath,
    ServerId,
)
from tests.storage.fake_s3 import FakeS3Client, FakeS3Store
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
    *,
    chunk: int = 7,
) -> None:
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(files, chunk=chunk))
    await storage.commit_snapshot(handle)


async def _hydrate(
    storage: Storage, community: CommunityId, server: ServerId
) -> dict[str, bytes]:
    return read_tar(await drain(storage.open_hydrate_source(community, server)))


# --- object backend --------------------------------------------------------


class _Gate:
    """Holds an object publish at a chosen point, once a stop is set.

    ``stop_after_copy(n)`` holds it after its ``n``-th copy into a snapshot;
    ``stop_at_snapshot_listing()`` holds it at its first listing of a snapshot
    prefix, which a commit makes under the server lock after its copy, when it
    reads the prior snapshot for the missing-region gate. Nothing is counted
    before a stop is set, so an arrange-step publish runs through untouched.
    """

    def __init__(self) -> None:
        self._copy_stops: dict[int, tuple[asyncio.Event, asyncio.Event]] = {}
        self._listing_stop: tuple[asyncio.Event, asyncio.Event] | None = None
        self._copies = 0

    def stop_after_copy(self, copies: int) -> tuple[asyncio.Event, asyncio.Event]:
        """Return (paused, resume) for a stop after the ``copies``-th copy."""

        stop = (asyncio.Event(), asyncio.Event())
        self._copy_stops[copies] = stop
        return stop

    def stop_at_snapshot_listing(self) -> tuple[asyncio.Event, asyncio.Event]:
        """Return (paused, resume) for a stop at the next snapshot listing."""

        self._listing_stop = (asyncio.Event(), asyncio.Event())
        return self._listing_stop

    def factory(self, store: FakeS3Store) -> S3ClientFactory:
        gate = self

        class _Client(FakeS3Client):
            async def copy_object(self, src_key: str, dst_key: str) -> None:
                await super().copy_object(src_key, dst_key)
                if not gate._copy_stops or "/snapshots/" not in dst_key:
                    return
                gate._copies += 1
                stop = gate._copy_stops.get(gate._copies)
                if stop is not None:
                    paused, resume = stop
                    paused.set()
                    await resume.wait()

            async def list_objects(self, prefix: str) -> list[S3Object]:
                stop = gate._listing_stop
                if stop is not None and "/snapshots/" in prefix:
                    gate._listing_stop = None
                    paused, resume = stop
                    paused.set()
                    await resume.wait()
                return await super().list_objects(prefix)

        @asynccontextmanager
        async def _factory() -> AsyncIterator[S3Client]:
            yield _Client(store)

        return _factory


def _gated() -> tuple[FakeS3Store, _Gate, ObjectStorage]:
    store = FakeS3Store()
    gate = _Gate()
    return store, gate, ObjectStorage(gate.factory(store))


def _server_prefix(community: CommunityId, server: ServerId) -> str:
    return f"communities/{community.value}/servers/{server.value}/"


def _unpointed_snapshot_keys(
    store: FakeS3Store, community: CommunityId, server: ServerId
) -> set[str]:
    """Every object under a ``snapshots/<id>/`` prefix the pointer does not name."""

    prefix = _server_prefix(community, server)
    pointer = store.objects.get(prefix + "current.json")
    live = json.loads(pointer)["snapshot"] if pointer is not None else None
    return {
        key
        for key in store.objects
        if key.startswith(prefix + "snapshots/")
        and (live is None or not key.startswith(live))
    }


async def _commit_task(
    storage: Storage, community: CommunityId, server: ServerId, files: dict[str, bytes]
) -> asyncio.Task[int]:
    handle = await storage.begin_snapshot(community, server)
    await storage.write_snapshot(handle, tar_stream(files))
    return asyncio.create_task(storage.commit_snapshot(handle))


async def test_object_sweep_during_a_commit_copy_keeps_the_new_snapshot() -> None:
    """The #3873 review probe: a sweep lands after the first of two copies."""

    store, gate, storage = _gated()
    community, server = new_scope()
    await _publish(storage, community, server, OLD)
    paused, resume = gate.stop_after_copy(1)
    commit = await _commit_task(storage, community, server, NEW)
    await paused.wait()
    copied = _unpointed_snapshot_keys(store, community, server)

    await storage.sweep()
    kept = copied <= set(store.objects)
    resume.set()
    await commit

    assert copied
    assert kept
    assert await _hydrate(storage, community, server) == NEW


async def test_object_sweep_between_a_commit_copy_and_its_flip_keeps_it() -> None:
    """The copy is done; the commit reads the prior snapshot under the lock."""

    store, gate, storage = _gated()
    community, server = new_scope()
    await _publish(storage, community, server, OLD)
    paused, resume = gate.stop_at_snapshot_listing()
    commit = await _commit_task(storage, community, server, NEW)
    await paused.wait()
    copied = _unpointed_snapshot_keys(store, community, server)

    await storage.sweep()
    kept = copied <= set(store.objects)
    resume.set()
    await commit

    assert len(copied) == len(NEW)
    assert kept
    assert await _hydrate(storage, community, server) == NEW


async def test_object_sweep_during_a_restore_copy_keeps_the_new_snapshot() -> None:
    store, gate, storage = _gated()
    community, server = new_scope()
    await _publish(storage, community, server, BACKED_UP)
    key = await storage.create_backup_from_current(community, server)
    await _publish(storage, community, server, OLD)
    paused, resume = gate.stop_after_copy(1)
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


async def test_object_sweep_during_a_first_write_keeps_the_new_snapshot() -> None:
    """A file write to a server that never published publishes its first snapshot."""

    store, gate, storage = _gated()
    community, server = new_scope()
    paused, resume = gate.stop_after_copy(1)
    write = asyncio.create_task(
        storage.write_file(community, server, RelPath("a"), b"FIRST")
    )
    await paused.wait()
    copied = _unpointed_snapshot_keys(store, community, server)

    await storage.sweep()
    kept = copied <= set(store.objects)
    resume.set()
    await write

    assert copied
    assert kept
    assert await _hydrate(storage, community, server) == {"a": b"FIRST"}


async def test_object_sweep_reads_the_registration_before_the_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A publish is released only after its flip, so the sweep reads it first.

    The first publish of a server, so its snapshot is the only candidate. Its
    flip and release land just after the sweep has read the pointer: read the
    other way round, the sweep would hold the pre-flip pointer and find the
    registration already gone.
    """

    store, gate, storage = _gated()
    community, server = new_scope()
    paused, resume = gate.stop_after_copy(len(NEW))
    commit = await _commit_task(storage, community, server, NEW)
    await paused.wait()
    real = storage._read_pointer
    fired: list[bool] = []

    async def _publish_completes_after_the_read(
        client: S3Client, server_prefix: str
    ) -> str | None:
        value = await real(client, server_prefix)
        if not fired:
            fired.append(True)
            resume.set()
            await commit
        return value

    monkeypatch.setattr(storage, "_read_pointer", _publish_completes_after_the_read)

    await storage.sweep()

    assert fired
    assert await _hydrate(storage, community, server) == NEW


async def test_object_sweep_reads_a_reader_lease_after_the_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hydrate leases the live snapshot, and its lease outlives the next flip.

    So the sweep reads a reader lease after the pointer, the reverse of a
    publish's registration. Here a hydrate starts and a publish flips just before
    the sweep reads the pointer for the live snapshot. Had the lease been read
    first, it would have been absent, and the sweep would then read the new
    pointer and delete the snapshot under the reader.
    """

    store, _gate, storage = _gated()
    community, server = new_scope()
    await _publish(storage, community, server, OLD)
    reader = storage.open_hydrate_source(community, server)
    chunks: list[bytes] = []
    real = storage._read_pointer
    fired: list[bool] = []

    async def _read_and_publish_first(
        client: S3Client, server_prefix: str
    ) -> str | None:
        if not fired:
            fired.append(True)
            chunks.append(await reader.__anext__())
            await _publish(storage, community, server, NEW)
        return await real(client, server_prefix)

    monkeypatch.setattr(storage, "_read_pointer", _read_and_publish_first)

    await storage.sweep()

    assert chunks
    assert read_tar(b"".join(chunks) + await drain(reader)) == OLD


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


def _moved_snapshot(server_root: Path, live: Path | None) -> Path:
    """The one snapshot directory beside the live one: the in-flight publish's."""

    (moved,) = [p for p in (server_root / "snapshots").iterdir() if p != live]
    return moved


def _names(directory: Path) -> set[str]:
    return {p.name for p in directory.iterdir()} if directory.is_dir() else set()


def _server_root(tmp_path: Path, community: CommunityId, server: ServerId) -> Path:
    return (
        tmp_path / "communities" / str(community.value) / "servers" / str(server.value)
    )


# 14 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(14 * 10 + 30)
async def test_fs_sweep_during_a_commit_move_keeps_the_new_snapshot(
    tmp_path: Path,
) -> None:
    seam = _PauseAt(PublishPhase.AFTER_MOVE)
    storage = FsStorage(tmp_path, failure_seam=seam)
    community, server = new_scope()
    await _publish(storage, community, server, OLD)
    live = snapshot_dir(tmp_path, community, server)
    seam.armed = True
    commit = await _commit_task(storage, community, server, NEW)
    await _wait(seam.reached)
    moved = _moved_snapshot(live.parent.parent, live)

    await asyncio.to_thread(storage.sweep)
    kept = _names(moved)
    seam.resume.set()
    await commit

    assert kept == set(NEW)
    assert await _hydrate(storage, community, server) == NEW


# 23 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(23 * 10 + 30)
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
    moved = _moved_snapshot(live.parent.parent, live)

    await asyncio.to_thread(storage.sweep)
    kept = _names(moved)
    seam.resume.set()
    await restore

    assert kept == set(BACKED_UP)
    assert await _hydrate(storage, community, server) == BACKED_UP


# 6 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(6 * 10 + 30)
async def test_fs_sweep_during_a_first_write_keeps_the_new_snapshot(
    tmp_path: Path,
) -> None:
    seam = _PauseAt(PublishPhase.AFTER_MOVE)
    storage = FsStorage(tmp_path, failure_seam=seam)
    community, server = new_scope()
    seam.armed = True
    write = asyncio.create_task(
        storage.write_file(community, server, RelPath("a"), b"FIRST")
    )
    await _wait(seam.reached)
    moved = _moved_snapshot(_server_root(tmp_path, community, server), None)

    await asyncio.to_thread(storage.sweep)
    kept = _names(moved)
    seam.resume.set()
    await write

    assert kept == {"a"}
    assert await _hydrate(storage, community, server) == {"a": b"FIRST"}


# 7 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(7 * 10 + 30)
async def test_fs_sweep_reads_the_registration_before_the_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fs twin of the object test.

    The first publish of a server, held between its move and its flip. Read in
    the right order, the registration keeps the snapshot and the pointer is never
    read for it. Read the other way round, the publish completes just after the
    sweep reads the pointer, and the sweep finds the registration gone.
    """

    seam = _PauseAt(PublishPhase.AFTER_MOVE)
    storage = FsStorage(tmp_path, failure_seam=seam)
    community, server = new_scope()
    seam.armed = True
    commit = await _commit_task(storage, community, server, NEW)
    await _wait(seam.reached)
    published = threading.Event()
    commit.add_done_callback(lambda _: published.set())
    real = storage._live_snapshot_name
    armed = [True]

    def _publish_completes_after_the_read(server_root: Path) -> str | None:
        value = real(server_root)
        if armed[0]:
            armed[0] = False
            seam.resume.set()
            assert published.wait(_WAIT_S)
        return value

    monkeypatch.setattr(
        storage, "_live_snapshot_name", _publish_completes_after_the_read
    )

    await asyncio.to_thread(storage.sweep)
    armed[0] = False
    seam.resume.set()
    await commit

    assert await _hydrate(storage, community, server) == NEW


# 14 fsyncs on the fs backend, at 10 s each plus 30 s (issue #3651).
@pytest.mark.timeout(14 * 10 + 30)
async def test_fs_sweep_reads_a_reader_lease_after_the_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fs twin of the object test: the reader lease is read after the pointer.

    The files are larger than the hydrate's first chunk plus a pipe buffer, so
    the tar writer is still in ``a`` when the sweep runs and has yet to open ``b``.
    """

    big = {"a": b"A" * (2 << 20), "b": b"B" * (2 << 20)}
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await _publish(storage, community, server, big, chunk=1 << 20)
    reader = storage.open_hydrate_source(community, server)
    chunks: list[bytes] = []
    loop = asyncio.get_running_loop()
    real = storage._live_snapshot_name
    fired: list[bool] = []

    async def _read_and_publish() -> None:
        chunks.append(await reader.__anext__())
        await _publish(storage, community, server, NEW)

    def _read_and_publish_first(server_root: Path) -> str | None:
        if not fired:
            fired.append(True)
            asyncio.run_coroutine_threadsafe(_read_and_publish(), loop).result(_WAIT_S)
        return real(server_root)

    monkeypatch.setattr(storage, "_live_snapshot_name", _read_and_publish_first)

    await asyncio.to_thread(storage.sweep)

    assert chunks
    assert read_tar(b"".join(chunks) + await drain(reader)) == big
