"""fs-specific file ops: symlink traversal containment + version-id ordering.

The backend-agnostic file read/edit/version/rollback contract is in
``test_port_contract.py`` (run against both adapters). This file keeps only the
fs realization details that reach into the filesystem: symlink-escape rejection
(object storage has no symlinks, Section 6/7.3), the fs version-id ordering /
oldest-pruning, and the delete-racing-a-read window of the fs read paths —
streaming and whole-bytes alike — all of which depend on the fs module internals.
"""

from __future__ import annotations

import builtins
import errno
import io
import os
import shutil
import tarfile
import time
from collections.abc import Iterator
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


# --- a delete racing a NON-streaming read (issue #2394) ----------------------
#
# The same check-then-act window the stream helpers shed above survived on the
# whole-bytes methods: ``read_file`` (is_file then read_bytes), ``backup_size``
# (is_file then stat) and ``list_dir`` (is_dir then iterdir). The reader lease
# does not close it -- ``delete_file`` / ``delete_dir`` remove in place, inside
# the very tree the lease pins -- so a delete landing in the window made the
# operation raise a bare ``FileNotFoundError``, which the servers seam does not
# translate (it translates :class:`NotFoundError` only): a measured 500 on
# ``GET .../files?path=``. Staged the same way as the stream races above: the
# entry is really removed and the existence predicate is then pinned to the
# pre-delete answer, which is what a surviving pre-check would have observed.


def _stale_dir_existence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``Path.is_dir`` report the pre-delete answer for every path."""

    monkeypatch.setattr(Path, "is_dir", lambda self, *args, **kwargs: True)


async def test_read_file_delete_racing_the_read_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"DATA"})

    await storage.delete_file(community, server, RelPath("f"))
    _stale_existence(monkeypatch)

    with pytest.raises(NotFoundError):
        await storage.read_file(community, server, RelPath("f"))


async def test_backup_size_delete_racing_the_stat_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"DATA"})
    key = await storage.create_backup_from_current(community, server)

    await storage.delete_backup(community, server, key)
    _stale_existence(monkeypatch)

    with pytest.raises(NotFoundError):
        await storage.backup_size(community, server, key)


async def test_list_dir_delete_racing_the_iterdir_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"d/f": b"DATA"})

    await storage.delete_dir(community, server, RelPath("d"))
    _stale_dir_existence(monkeypatch)

    with pytest.raises(NotFoundError):
        await storage.list_dir(community, server, RelPath("d"))


async def test_view_list_dir_delete_racing_the_iterdir_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pinned working-set view has the same window: the pin holds the tree."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"d/f": b"DATA"})

    async with storage.open_working_set_view(community, server) as view:
        await storage.delete_dir(community, server, RelPath("d"))
        _stale_dir_existence(monkeypatch)

        with pytest.raises(NotFoundError):
            await view.list_dir(RelPath("d"))


async def test_list_dir_on_a_file_path_stays_a_miss(tmp_path: Path) -> None:
    """Dropping the ``is_dir`` pre-check must not change what listing a FILE answers.

    ``iterdir`` on a deleted directory (ENOENT) and ``iterdir`` on a path that is a
    file (ENOTDIR) are different misses, and the pre-check folded both into the one
    the Port models: ``directory not found``. The object backend answers a file
    path the same way (an empty prefix listing under a non-empty sub-key), so the
    fold is the contract rather than an fs accident — pinned across both backends
    in ``test_port_contract.py``.
    """

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"DATA"})

    with pytest.raises(NotFoundError):
        await storage.list_dir(community, server, RelPath("f"))


# --- an over-long path component (issue #2394) ------------------------------
#
# ``RelPath`` bounds neither component nor total length, so a name past the
# filesystem's NAME_MAX is reachable from an ordinary ``?path=`` query. It used to
# leave every read path as ``OSError(ENAMETOOLONG)`` — ``is_file`` / ``is_dir``
# RAISE on it rather than answering False — and so reached the edge as a 500 for
# what is purely a client-supplied bad path (measured on ``GET .../files?path=``).
# A component longer than the filesystem allows cannot name an existing file, so
# the honest answer is the Port's miss; the errno therefore joins the shared
# ``_NOT_A_READABLE_FILE`` set, which keeps the read, stream and listing paths
# agreeing on it instead of diverging.

_TOO_LONG = "x" * 300  # past NAME_MAX (255) on every mainstream filesystem


async def test_over_long_name_is_a_miss_on_every_read_path(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"DATA"})
    rel = RelPath(_TOO_LONG)

    with pytest.raises(NotFoundError):
        await storage.read_file(community, server, rel)
    with pytest.raises(NotFoundError):
        await drain(storage.open_file_stream(community, server, rel))
    with pytest.raises(NotFoundError):
        await storage.list_dir(community, server, rel)
    async with storage.open_working_set_view(community, server) as view:
        with pytest.raises(NotFoundError):
            await drain(view.open_file_stream(rel))
        with pytest.raises(NotFoundError):
            await view.list_dir(rel)


# --- a delete racing ONE LISTED CHILD (issue #2414) -------------------------
#
# #2394 closed the window on the listing TARGET; a second one sat on each listed
# CHILD. ``Path.iterdir`` drains an ``os.scandir`` into a list before it yields
# anything, so the listing works from an atomic snapshot of the directory and then
# describes each name in it — and a name unlinked in between made the describing
# ``stat`` raise a bare ``FileNotFoundError``, untranslated by the servers seam: a
# measured 500 on ``GET .../files?path=d&list=true`` for one unlucky delete
# anywhere in the directory. The entry is now omitted, matching the object backend,
# whose listing is one ``list_objects`` response a concurrently deleted key simply
# is not in.
#
# Staged as the real race rather than a stubbed predicate: the entry is really
# unlinked as the snapshot is taken, which IS the window between the snapshot and
# the stat that follows it.


def _unlink_after_the_snapshot(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Really delete ``name`` in the window between the snapshot and the stats."""

    real_iterdir = Path.iterdir

    def snapshot_then_unlink(self: Path) -> Iterator[Path]:
        children = list(real_iterdir(self))
        for child in children:
            if child.name == name:
                child.unlink()
        return iter(children)

    monkeypatch.setattr(Path, "iterdir", snapshot_then_unlink)


def _rmtree_after_the_snapshot(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Really delete the listed DIRECTORY itself, once its snapshot is taken."""

    real_iterdir = Path.iterdir

    def snapshot_then_rmtree(self: Path) -> Iterator[Path]:
        children = list(real_iterdir(self))
        if self.name == name:
            shutil.rmtree(self)
        return iter(children)

    monkeypatch.setattr(Path, "iterdir", snapshot_then_rmtree)


def _child_stat_fails(monkeypatch: pytest.MonkeyPatch, name: str, err: int) -> None:
    """Fail the ``stat`` of one listed child with ``err``, leaving the rest real.

    Patching ``Path.stat`` still intercepts the listing's ``Path.lstat`` (issue
    #2418) because pathlib implements ``lstat`` as ``stat(follow_symlinks=False)``.
    If that delegation ever changes, these tests fail loudly (the injected errno
    never arrives) rather than silently passing, so the coupling is safe to rely on.
    """

    real_stat = Path.stat

    def _fake_stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == name:
            raise OSError(err, os.strerror(err), str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _fake_stat)


async def test_list_dir_omits_a_child_deleted_after_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"d/keep": b"DATA", "d/gone": b"DATA"})
    _unlink_after_the_snapshot(monkeypatch, "gone")

    entries = await storage.list_dir(community, server, RelPath("d"))

    assert [entry.name for entry in entries] == ["keep"]


async def test_view_list_dir_omits_a_child_deleted_after_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pinned working-set view describes its children the same way."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"d/keep": b"DATA", "d/gone": b"DATA"})

    async with storage.open_working_set_view(community, server) as view:
        _unlink_after_the_snapshot(monkeypatch, "gone")

        entries = await view.list_dir(RelPath("d"))

    assert [entry.name for entry in entries] == ["keep"]


@pytest.mark.parametrize("err", [errno.EACCES, errno.EIO, errno.ENOTDIR])
async def test_list_dir_propagates_a_non_vanish_child_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, err: int
) -> None:
    """Only the child's OWN disappearance is omitted.

    EACCES and EIO name a child that does exist, and ENOTDIR means the PARENT
    stopped being a directory under the listing — reporting any of them as "that
    entry is gone" would hide a real failure behind a short listing.
    """

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"d/keep": b"DATA", "d/bad": b"DATA"})
    _child_stat_fails(monkeypatch, "bad", err)

    with pytest.raises(OSError) as caught:
        await storage.list_dir(community, server, RelPath("d"))
    assert caught.value.errno == err


@pytest.mark.parametrize("err", [errno.EACCES, errno.EIO, errno.ENOTDIR])
async def test_view_list_dir_propagates_a_non_vanish_child_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, err: int
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"d/keep": b"DATA", "d/bad": b"DATA"})

    async with storage.open_working_set_view(community, server) as view:
        _child_stat_fails(monkeypatch, "bad", err)

        with pytest.raises(OSError) as caught:
            await view.list_dir(RelPath("d"))
        assert caught.value.errno == err


async def test_list_dir_of_a_directory_deleted_after_the_snapshot_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting the vanished entries must not report a deleted directory as empty.

    Every child of a ``delete_dir``'d directory vanishes at once, so omitting them
    all would answer ``[]`` — "this directory exists and is empty" — for a
    directory that is gone, contradicting the miss #2394 pinned. The object
    backend answers a prefix with no members the same way: ``directory not found``.
    """

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"d/f": b"DATA"})
    _rmtree_after_the_snapshot(monkeypatch, "d")

    with pytest.raises(NotFoundError):
        await storage.list_dir(community, server, RelPath("d"))


async def test_list_dir_omits_the_last_child_without_reporting_a_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory outlives its last entry: empty, not the miss above.

    The boundary the guard has to get right — "every entry vanished because the
    DIRECTORY went" is the miss; "the entries went" is an empty listing.
    """

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"d/gone": b"DATA"})
    _unlink_after_the_snapshot(monkeypatch, "gone")

    assert await storage.list_dir(community, server, RelPath("d")) == []


async def test_list_dir_of_an_empty_directory_is_still_empty(tmp_path: Path) -> None:
    """A directory that really is empty lists as empty, not as a miss."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"DATA"})
    await storage.make_dir(community, server, RelPath("d"))

    assert await storage.list_dir(community, server, RelPath("d")) == []


# --- a listed child that is a SYMLINK (issue #2418) --------------------------
#
# A listing describes the link itself (``lstat``), never its target. Two children
# cannot be described by a target-following ``stat`` at all — a dangling link
# (ENOENT, which #2414's vanished-child rule then silently omitted) and a link
# loop (ELOOP, which escaped untranslated and 500'd the whole listing) — yet both
# are real dirents ``ls`` shows and neither vanished. ``lstat`` succeeds on both,
# so they need no special case: each is described as what it is, a link, with
# ``is_dir=False`` and the link's own size (the target string's length).
#
# This is the contract the Worker's running-server listing already ships and pins
# (``unix.Fstatat(..., AT_SYMLINK_NOFOLLOW)`` in ``instancemanager.go``), so the
# at-rest and running browsers now describe the same entry the same way — which
# is what ``FileEntry`` in the control-plane proto already claims. A link to a
# DIRECTORY is therefore reported as a file too; that is the accepted trade.


async def test_list_dir_describes_a_dangling_symlink_as_the_link(
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"d/keep": b"DATA"})
    (snapshot_dir(tmp_path, community, server) / "d" / "broken").symlink_to("nowhere")

    entries = await storage.list_dir(community, server, RelPath("d"))

    broken = next(entry for entry in entries if entry.name == "broken")
    assert broken.is_dir is False
    assert broken.size == len("nowhere")


async def test_view_list_dir_describes_a_dangling_symlink_as_the_link(
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"d/keep": b"DATA"})
    (snapshot_dir(tmp_path, community, server) / "d" / "broken").symlink_to("nowhere")

    async with storage.open_working_set_view(community, server) as view:
        entries = await view.list_dir(RelPath("d"))

    broken = next(entry for entry in entries if entry.name == "broken")
    assert broken.is_dir is False
    assert broken.size == len("nowhere")


async def test_list_dir_describes_a_symlink_loop_as_the_link(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"d/keep": b"DATA"})
    _plant_symlink_loop(snapshot_dir(tmp_path, community, server) / "d" / "loop")

    entries = await storage.list_dir(community, server, RelPath("d"))

    loop = next(entry for entry in entries if entry.name == "loop")
    assert loop.is_dir is False
    assert loop.size == len("loop")


async def test_view_list_dir_describes_a_symlink_loop_as_the_link(
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"d/keep": b"DATA"})
    _plant_symlink_loop(snapshot_dir(tmp_path, community, server) / "d" / "loop")

    async with storage.open_working_set_view(community, server) as view:
        entries = await view.list_dir(RelPath("d"))

    loop = next(entry for entry in entries if entry.name == "loop")
    assert loop.is_dir is False
    assert loop.size == len("loop")


async def test_list_dir_does_not_follow_a_symlink_to_a_directory(
    tmp_path: Path,
) -> None:
    """The link is described, not the directory it points at."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"d/keep": b"DATA", "real/inner": b"X"})
    live = snapshot_dir(tmp_path, community, server)
    (live / "d" / "alias").symlink_to("../real")

    entries = await storage.list_dir(community, server, RelPath("d"))

    alias = next(entry for entry in entries if entry.name == "alias")
    assert alias.is_dir is False
    assert alias.size == len("../real")


async def test_view_list_dir_does_not_follow_a_symlink_to_a_directory(
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"d/keep": b"DATA", "real/inner": b"X"})
    live = snapshot_dir(tmp_path, community, server)
    (live / "d" / "alias").symlink_to("../real")

    async with storage.open_working_set_view(community, server) as view:
        entries = await view.list_dir(RelPath("d"))

    alias = next(entry for entry in entries if entry.name == "alias")
    assert alias.is_dir is False
    assert alias.size == len("../real")


# --- READING a symlink dirent (issue #2418 review) --------------------------
#
# Describing the link rather than its target split the listing from the read: the
# listing sized the LINK while the read still followed it to the target. That is
# not cosmetic. ``DownloadFile.file_size`` hands the parent listing's size
# straight to a ``Content-Length`` header while the body comes from the stream, so
# a 7-byte header over a 1000-byte body is a protocol abort or a silent
# truncation; and the search's per-file memory cap gates on the same listed size,
# so a link to a multi-GiB file slipped under the cap and was read whole.
#
# An at-rest read of a symlink dirent is therefore a MISS. One rule closes both,
# and it is the same convergence that decided the listing: the Worker already
# refuses to follow a symlink on read, #2393 already answers a dangling link and a
# loop as a miss on this very path, and a working set cannot legitimately contain
# a symlink anyway (uploads refuse symlink members, the Worker's snapshot tar
# skips them, hydrate rejects them).
#
# It is the LEAF dirent that is refused, never the resolution of the working-set
# root: ``current`` IS a symlink (``current -> snapshots/<id>``), but
# ``_current_dir`` readlinks it and hands ``_safe_target`` the resolved snapshot
# DIRECTORY, so the root is never a leaf under test here. Containment still runs
# first, so an escaping link stays a PathTraversalError.


async def test_a_listed_symlink_never_promises_a_size_the_read_contradicts(
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"big.bin": b"B" * 1000})
    (snapshot_dir(tmp_path, community, server) / "link.bin").symlink_to("big.bin")

    entries = await storage.list_dir(community, server, RelPath("."))

    listed = next(entry for entry in entries if entry.name == "link.bin")
    assert listed.size == len("big.bin")  # the link's own size, not the target's
    with pytest.raises(NotFoundError):
        await storage.read_file(community, server, RelPath("link.bin"))


async def test_open_file_stream_of_a_symlink_is_not_found(tmp_path: Path) -> None:
    """The stream is the path that would emit the body under the wrong header."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"big.bin": b"B" * 1000})
    (snapshot_dir(tmp_path, community, server) / "link.bin").symlink_to("big.bin")

    with pytest.raises(NotFoundError):
        await drain(storage.open_file_stream(community, server, RelPath("link.bin")))


async def test_view_file_stream_of_a_symlink_is_not_found(tmp_path: Path) -> None:
    """The pinned view reads the same way, so the export walk skips the link."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"big.bin": b"B" * 1000})
    (snapshot_dir(tmp_path, community, server) / "link.bin").symlink_to("big.bin")

    async with storage.open_working_set_view(community, server) as view:
        with pytest.raises(NotFoundError):
            await drain(view.open_file_stream(RelPath("link.bin")))


async def test_reading_an_escaping_symlink_is_still_a_traversal_refusal(
    tmp_path: Path,
) -> None:
    """Refusing the leaf must not soften containment: escape still outranks the miss.

    Containment is evaluated first, so a link out of the root keeps reporting the
    escape it is rather than being downgraded to an ordinary missing file. Not
    following the leaf makes escape strictly harder, never easier.
    """

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"x"})
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"top-secret")
    (snapshot_dir(tmp_path, community, server) / "escape").symlink_to(secret)

    with pytest.raises(PathTraversalError):
        await storage.read_file(community, server, RelPath("escape"))


async def test_reading_through_an_intermediate_symlink_still_works(
    tmp_path: Path,
) -> None:
    """Only the LEAF is refused; an intermediate component still resolves.

    ``alias/data`` names a real file, and the listing that shows it (``list_dir``
    on the link's own path still follows) sizes it truthfully — so the listing and
    the read agree and there is nothing to fix here.
    """

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"real/data": b"inside"})
    live = snapshot_dir(tmp_path, community, server)
    (live / "alias").symlink_to("real")

    assert (
        await storage.read_file(community, server, RelPath("alias/data")) == b"inside"
    )


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
