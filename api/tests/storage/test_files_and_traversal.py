"""fs-specific file ops: symlink traversal containment + version-id ordering.

The backend-agnostic file read/edit/version/rollback contract is in
``test_port_contract.py`` (run against both adapters). This file keeps only the
fs realization details that reach into the filesystem: symlink-escape rejection
(object storage has no symlinks, Section 6/7.3), the fs version-id ordering /
oldest-pruning, and the delete-racing-a-read window of the fs stream helpers —
all of which depend on the fs module internals.
"""

from __future__ import annotations

import builtins
import errno
import io
import os
import tarfile
import time
from pathlib import Path
from typing import Any

import pytest

from mc_server_dashboard_api.storage.adapters import fs as fs_module
from mc_server_dashboard_api.storage.adapters.fs import (
    _DEFAULT_MAX_RESTORE_BYTES,
    FsStorage,
    _extract_tar_gz_into,
    _new_version_id,
)
from mc_server_dashboard_api.storage.domain.errors import (
    NotFoundError,
    PathTraversalError,
)
from mc_server_dashboard_api.storage.domain.value_objects import RelPath
from tests.storage.helpers import (
    drain,
    new_scope,
    publish,
    snapshot_dir,
    stream_of,
)


async def test_read_rejects_symlink_escape(tmp_path: Path) -> None:
    """A symlink inside current/ pointing outside the root is refused (Section 6)."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"x"})

    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"top-secret")
    live = snapshot_dir(tmp_path, community, server)
    os.symlink(secret, live / "escape")

    with pytest.raises(PathTraversalError):
        await storage.read_file(community, server, RelPath("escape"))


async def test_list_dir_rejects_symlink_escape(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"x"})

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    live = snapshot_dir(tmp_path, community, server)
    os.symlink(outside_dir, live / "escape_dir")

    with pytest.raises(PathTraversalError):
        await storage.list_dir(community, server, RelPath("escape_dir"))


async def test_internal_symlink_within_root_is_allowed(tmp_path: Path) -> None:
    """A symlink that resolves to a location still inside current/ is fine."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"real/data": b"inside"})

    live = snapshot_dir(tmp_path, community, server)
    os.symlink(live / "real", live / "alias")
    assert (
        await storage.read_file(community, server, RelPath("alias/data")) == b"inside"
    )


async def test_make_dir_materializes_empty_dir_and_survives_hydrate(
    tmp_path: Path,
) -> None:
    """fs materializes a real empty directory that survives a hydrate round-trip.

    The hydrate tar is built with ``tar.add`` (recursive), which emits directory
    members, so an empty directory created via ``make_dir`` is preserved in the
    streamed working set — the fs realization of the empty-dir support (issue
    #259). Object storage cannot represent an empty dir (see object specifics).
    """

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"server.properties": b"x"})

    await storage.make_dir(community, server, RelPath("plugins"))

    live = snapshot_dir(tmp_path, community, server)
    assert (live / "plugins").is_dir()
    # The empty dir lists as empty rather than 404-ing.
    assert await storage.list_dir(community, server, RelPath("plugins")) == []


# --- a delete racing an in-flight read (issue #2391) -------------------------
#
# ``delete_file`` unlinks IN PLACE inside the live snapshot, and the
# active-reader lease protects the snapshot DIRECTORY, not the files in it — so a
# DELETE /files racing a download of the same file lands between the stream's
# existence check and its open. The window is microseconds wide, so it is staged
# rather than raced: the file is really deleted and ``Path.is_file`` is then
# pinned to the pre-delete answer, which is exactly what a surviving pre-check
# would have observed. The stream must report the Port's own NotFoundError; a
# bare ``FileNotFoundError`` is not translated by the servers seam and reaches
# the edge as a 500. Pinning ``is_file`` also makes these tests fail again if a
# check-then-open pre-check is ever reintroduced.


def _stale_existence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``Path.is_file`` report the pre-delete answer for every path."""

    monkeypatch.setattr(Path, "is_file", lambda self, *args, **kwargs: True)


async def test_open_file_stream_delete_racing_the_open_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"DATA"})

    stream = storage.open_file_stream(community, server, RelPath("f"))
    await storage.delete_file(community, server, RelPath("f"))
    _stale_existence(monkeypatch)

    with pytest.raises(NotFoundError):
        await drain(stream)


async def test_view_file_stream_delete_racing_the_open_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pinned working-set view has the same window: the pin holds the dir."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"DATA"})

    async with storage.open_working_set_view(community, server) as view:
        stream = view.open_file_stream(RelPath("f"))
        await storage.delete_file(community, server, RelPath("f"))
        # The lease did not hold the file back: it is gone from the pinned tree.
        assert not (snapshot_dir(tmp_path, community, server) / "f").exists()
        _stale_existence(monkeypatch)

        with pytest.raises(NotFoundError):
            await drain(stream)


# --- a path that names no readable file (issue #2393) ------------------------
#
# The ``is_file()`` pre-check the streams above dropped answered False — a plain
# miss — for four distinct open failures: the path is gone (ENOENT), it names a
# directory (EISDIR), it is reached through a non-directory (ENOTDIR), and it is a
# symlink that loops (ELOOP). The open that replaced the pre-check translated by
# exception TYPE, which covers only the first three: ELOOP has no dedicated
# ``OSError`` subclass, so it escaped the helper backend-native, and the
# servers-side seam translates :class:`NotFoundError` only — a 500 on a path
# ``read_file`` still answers as a clean miss. A self-looping internal symlink is
# deliverable (the tar upload path extracts with ``filter="data"``, which permits
# internal relative symlinks), so the translation is errno-filtered instead.
#
# The filter must stay a filter: EACCES and EIO name a path that DOES exist and
# must escape as themselves rather than be reported as a missing file.


def _plant_symlink_loop(path: Path) -> None:
    """Replace ``path`` with a symlink to itself; opening it fails with ELOOP."""

    path.unlink(missing_ok=True)
    path.symlink_to(path.name)


def _open_fails(monkeypatch: pytest.MonkeyPatch, target: Path, err: int) -> None:
    """Make opening ``target`` fail with ``err``, leaving every other open alone.

    EIO cannot be staged on a real file at all and EACCES does not bind a root
    test runner, so the errnos that must NOT be swallowed are injected at the one
    call the helpers make.
    """

    real_open = builtins.open

    def _fake_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(file) == target:
            raise OSError(err, os.strerror(err), str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(fs_module, "open", _fake_open, raising=False)


async def test_read_file_on_a_symlink_loop_is_not_found(tmp_path: Path) -> None:
    """The read path's answer, which the stream paths have to agree with."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"DATA"})
    _plant_symlink_loop(snapshot_dir(tmp_path, community, server) / "loop")

    with pytest.raises(NotFoundError):
        await storage.read_file(community, server, RelPath("loop"))


async def test_open_file_stream_on_a_symlink_loop_is_not_found(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"DATA"})
    _plant_symlink_loop(snapshot_dir(tmp_path, community, server) / "loop")

    with pytest.raises(NotFoundError):
        await drain(storage.open_file_stream(community, server, RelPath("loop")))


async def test_view_file_stream_on_a_symlink_loop_is_not_found(tmp_path: Path) -> None:
    """The pinned working-set view opens the same way, so it folds the same set."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"DATA"})
    _plant_symlink_loop(snapshot_dir(tmp_path, community, server) / "loop")

    async with storage.open_working_set_view(community, server) as view:
        with pytest.raises(NotFoundError):
            await drain(view.open_file_stream(RelPath("loop")))


async def test_open_jar_on_a_symlink_loop_is_not_found(tmp_path: Path) -> None:
    """The JAR/backup egress helper is the same shape and folds the same set."""

    storage = FsStorage(tmp_path)
    key = await storage.put_jar(stream_of(b"jar-bytes"))
    _plant_symlink_loop(storage._jar_path(key))

    with pytest.raises(NotFoundError):
        await drain(storage.open_jar(key))


@pytest.mark.parametrize("err", [errno.EACCES, errno.EIO])
async def test_open_file_stream_propagates_a_non_miss_open_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, err: int
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"DATA"})
    _open_fails(monkeypatch, snapshot_dir(tmp_path, community, server) / "f", err)

    with pytest.raises(OSError) as caught:
        await drain(storage.open_file_stream(community, server, RelPath("f")))
    assert caught.value.errno == err


@pytest.mark.parametrize("err", [errno.EACCES, errno.EIO])
async def test_view_file_stream_propagates_a_non_miss_open_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, err: int
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"DATA"})

    async with storage.open_working_set_view(community, server) as view:
        _open_fails(monkeypatch, snapshot_dir(tmp_path, community, server) / "f", err)

        with pytest.raises(OSError) as caught:
            await drain(view.open_file_stream(RelPath("f")))
        assert caught.value.errno == err


@pytest.mark.parametrize("err", [errno.EACCES, errno.EIO])
async def test_open_jar_propagates_a_non_miss_open_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, err: int
) -> None:
    storage = FsStorage(tmp_path)
    key = await storage.put_jar(stream_of(b"jar-bytes"))
    _open_fails(monkeypatch, storage._jar_path(key), err)

    with pytest.raises(OSError) as caught:
        await drain(storage.open_jar(key))
    assert caught.value.errno == err


def test_version_ids_sort_chronologically_across_time_low_wrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lexicographic order of version ids must equal creation order (Section 5).

    The old uuid1 scheme keyed on ``time_low`` (the low 32 bits of the 100ns
    timestamp), which wraps roughly every 429 s; ids minted across a wrap sorted
    out of creation order. Mint ids across timestamps that span such a wrap and
    confirm the new nanosecond-prefixed id sorts chronologically.
    """

    wrap_ns = (1 << 32) * 100  # one uuid1 time_low period, in nanoseconds
    base = 1_700_000_000 * 1_000_000_000  # an arbitrary wall-clock nanosecond base
    timestamps = [base, base + wrap_ns // 2, base + wrap_ns, base + 2 * wrap_ns]

    feed = iter(timestamps)
    monkeypatch.setattr(time, "time_ns", lambda: next(feed))
    ids = [_new_version_id() for _ in timestamps]

    assert sorted(ids) == ids  # lexicographic order == creation order


async def test_retention_prunes_the_oldest_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pruning drops the OLDEST retained version, identified by id order (Section 5).

    Crafted, strictly increasing ids make creation order unambiguous; after writing
    past the retention bound the lowest-sorting (oldest) ids are the ones removed.
    """

    storage = FsStorage(tmp_path, version_retention=2)
    community, server = new_scope()
    await publish(storage, community, server, {"cfg": b"v0"})

    crafted = iter([f"{n:020d}-aaaaaaaa" for n in range(1, 100)])
    monkeypatch.setattr(fs_module, "_new_version_id", lambda: next(crafted))

    for i in range(1, 5):  # writes v1..v4, capturing v0..v3 as versions
        await storage.write_file(community, server, RelPath("cfg"), f"v{i}".encode())

    versions = await storage.list_file_versions(community, server, RelPath("cfg"))
    contents = [
        await storage.read_file_version(community, server, RelPath("cfg"), v)
        for v in versions
    ]
    # Only the two newest prior contents survive; the oldest (v0, v1) were pruned.
    assert contents == [b"v3", b"v2"]


def test_restore_extract_preserves_file_mode_and_mtime(tmp_path: Path) -> None:
    """Streaming a member body out by hand still restores its mode and mtime.

    The size-bounded restore extraction writes file bodies via a plain ``open`` /
    ``write`` loop instead of ``extractall``, which would otherwise drop the member
    metadata; the mode/mtime are reapplied from the data-filter-sanitized member so
    a restored file keeps the parity ``extractall(filter="data")`` gave before (#287).
    """

    archive = tmp_path / "backup.tar.gz"
    mtime = 1_600_000_000
    info = tarfile.TarInfo(name="run.sh")
    payload = b"#!/bin/sh\necho hi\n"
    info.size = len(payload)
    info.mode = 0o750
    info.mtime = mtime
    with tarfile.open(archive, mode="w:gz") as tar:
        tar.addfile(info, io.BytesIO(payload))

    dest = tmp_path / "out"
    dest.mkdir()
    _extract_tar_gz_into(archive, dest, _DEFAULT_MAX_RESTORE_BYTES)

    extracted = dest / "run.sh"
    assert extracted.read_bytes() == payload
    stat = extracted.stat()
    assert stat.st_mode & 0o777 == 0o750
    assert int(stat.st_mtime) == mtime
