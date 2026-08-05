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
    NameTooLongError,
    NotFoundError,
    PathOccupiedError,
    PathTraversalError,
    SymlinkRefusedError,
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


async def test_internal_symlink_within_root_is_refused_not_followed(
    tmp_path: Path,
) -> None:
    """Staying inside current/ is not a licence to follow the link (issue #2432).

    This test used to assert the opposite -- that a contained intermediate link
    resolved and the read returned the target's bytes. Containment answers only
    "does this path leave the root", never "may this path be followed": a symlink
    is refused at EVERY component, so contained means ``symlink_refused`` rather
    than allowed. Escaping still outranks it (the test above), which is what keeps
    the two verdicts in the order Section 6 fixes.
    """

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"real/data": b"inside"})

    live = snapshot_dir(tmp_path, community, server)
    os.symlink(live / "real", live / "alias")
    with pytest.raises(SymlinkRefusedError):
        await storage.read_file(community, server, RelPath("alias/data"))


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


# --- LISTING a symlink dirent by its own path (issue #2426) -----------------
#
# Describing the link left one resolution still following it: the parent listing
# called ``alias`` a file, while ``list_dir("alias")`` realpathed the leaf and
# listed the TARGET's children. Both answers met inside ONE request — the
# download endpoint picks the single-file or the directory-zip branch from a
# ``list_dir`` probe on the entry's own path — so a download zipped a subtree for
# an entry the file browser draws as a file.
#
# The listing therefore resolves the way every other read already does: a symlink
# component is refused, and containment still runs first, so an escaping link stays
# a PathTraversalError. The refusal was the Port's own MISS when it landed; issue
# #2432 moved it to the modelled ``SymlinkRefusedError`` -- 422 ``symlink_refused``
# at the edge -- so a listing that shows the entry and a click that refuses it stop
# contradicting each other, and say the same sentence at rest and while running.


async def test_list_dir_of_a_symlink_to_a_directory_is_refused(
    tmp_path: Path,
) -> None:
    """The link's own path lists nothing, exactly as its parent describes it."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"real/inner": b"X"})
    live = snapshot_dir(tmp_path, community, server)
    (live / "alias").symlink_to("real")

    entries = await storage.list_dir(community, server, RelPath("."))

    listed = next(entry for entry in entries if entry.name == "alias")
    assert listed.is_dir is False  # the parent's answer
    # and the same answer by the link's own path (issue #2432: the refusal, not a
    # miss -- the entry is right there in the listing above)
    with pytest.raises(SymlinkRefusedError):
        await storage.list_dir(community, server, RelPath("alias"))


async def test_view_list_dir_of_a_symlink_to_a_directory_is_refused(
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"real/inner": b"X"})
    live = snapshot_dir(tmp_path, community, server)
    (live / "alias").symlink_to("real")

    async with storage.open_working_set_view(community, server) as view:
        entries = await view.list_dir(RelPath("."))

        listed = next(entry for entry in entries if entry.name == "alias")
        assert listed.is_dir is False
        with pytest.raises(SymlinkRefusedError):
            await view.list_dir(RelPath("alias"))


async def test_listing_through_an_intermediate_symlink_is_refused(
    tmp_path: Path,
) -> None:
    """A directory reached THROUGH a link no longer lists either (issue #2432).

    This test used to assert that ``alias/inner`` listed the target's children,
    because only the leaf was refused. That split rule meant every probe had to
    know which half applied to it; the listing now refuses a link at any
    component, exactly as the Worker's running-server listing already does.
    """

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"real/inner/data": b"inside"})
    live = snapshot_dir(tmp_path, community, server)
    (live / "alias").symlink_to("real")

    with pytest.raises(SymlinkRefusedError):
        await storage.list_dir(community, server, RelPath("alias/inner"))


# --- the OCCUPIED-NAME question (issue #2426 review) ------------------------
#
# A rename's never-clobber pre-check asks whether a name is taken. It cannot be
# composed out of the read methods, because the whole point of the two rules
# above is that a symlink dirent answers neither a read nor a listing -- yet it
# very much occupies its name, and a rename that believed otherwise would land on
# whatever the link points at. Nor can it be answered from the PARENT listing:
# the parent may itself be a link, whose listing is that same miss, while the
# path still resolves through it.
#
# ``path_exists`` therefore resolves exactly as a read does -- intermediate
# components followed, the leaf described as itself, containment first.


async def test_a_symlink_occupies_its_name_although_nothing_can_read_it(
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"real/inner": b"X", "big.bin": b"B"})
    live = snapshot_dir(tmp_path, community, server)
    (live / "alias").symlink_to("real")
    (live / "link.bin").symlink_to("big.bin")
    (live / "broken").symlink_to("nowhere")
    _plant_symlink_loop(live / "loop")

    for name in ("alias", "link.bin", "broken", "loop"):
        assert await storage.path_exists(community, server, RelPath(name)), name


async def test_a_name_under_a_symlink_parent_is_refused_not_probed(
    tmp_path: Path,
) -> None:
    """The probe stops following the parent chain too (issue #2432).

    This test used to assert that ``alias/inner`` was reported OCCUPIED, and the
    justification was that the read paths resolved it to the real file — so a
    pre-check calling the name free would have let a rename overwrite it. Once no
    read follows an intermediate link that rationale dissolves with it: nothing can
    reach the name any more, so nothing has to be protected from clobbering it, and
    the probe answers the same refusal every read does. Never-clobber is preserved
    by the rename's own resolve refusing the same path, not by this probe.

    The LEAF stays described as itself (the test above): a link occupies its name.
    """

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"real/inner": b"X", "real/sub/f": b"Y"})
    live = snapshot_dir(tmp_path, community, server)
    (live / "alias").symlink_to("real")

    for name in ("alias/inner", "alias/sub", "alias/free"):
        with pytest.raises(SymlinkRefusedError):
            await storage.path_exists(community, server, RelPath(name))


async def test_path_exists_of_an_escaping_symlink_is_still_a_traversal_refusal(
    tmp_path: Path,
) -> None:
    """Containment outranks the answer here too: an escape is refused, not reported."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"x"})
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"top-secret")
    (snapshot_dir(tmp_path, community, server) / "escape").symlink_to(secret)

    with pytest.raises(PathTraversalError):
        await storage.path_exists(community, server, RelPath("escape"))


# --- a MUTATION reached through a symlink parent (issue #2432) ---------------
#
# One resolve rule for every Port operation, mutations included: a mutation that
# still followed an intermediate link would edit, move or destroy something no read
# can reach and no listing describes, through a name the browser draws under a
# different parent. So the parent chain refuses for a mutation exactly as it does
# for a read.
#
# What a mutation does with a link as its LEAF is deliberately untouched here and
# stays #2429's question, so these plant the link strictly ABOVE the leaf.


async def test_mutations_under_a_symlink_parent_are_refused(tmp_path: Path) -> None:
    """Every mutation refuses the path, and the link's target is left intact."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"real/inner": b"X", "g": b"G"})
    live = snapshot_dir(tmp_path, community, server)
    (live / "alias").symlink_to("real")

    with pytest.raises(SymlinkRefusedError):
        await storage.write_file(community, server, RelPath("alias/inner"), b"EDITED")
    with pytest.raises(SymlinkRefusedError):
        await storage.delete_file(community, server, RelPath("alias/inner"))
    with pytest.raises(SymlinkRefusedError):
        await storage.make_dir(community, server, RelPath("alias/new"))
    with pytest.raises(SymlinkRefusedError):
        await storage.rename_file(
            community, server, RelPath("alias/inner"), RelPath("moved")
        )
    with pytest.raises(SymlinkRefusedError):
        await storage.rename_file(
            community, server, RelPath("g"), RelPath("alias/inner")
        )

    assert (live / "real" / "inner").read_bytes() == b"X"
    assert not (live / "real" / "new").exists()
    assert (live / "g").read_bytes() == b"G"


# --- a MUTATION on a symlink LEAF dirent (issue #2429) ----------------------
#
# The listing describes the dirent, so the mutation surface must act on the
# dirent too. A symlink dirent supports exactly two operations -- being listed
# and being deleted; every other mutation refuses. delete unlinks the LINK
# (never the target it points at) and captures no version (a link has no
# Port-readable content to retain); write / rename-source / make_dir refuse;
# delete_dir misses (a link is never a directory dirent); retain is a no-op.


async def test_delete_file_unlinks_a_working_link_and_keeps_its_target(
    tmp_path: Path,
) -> None:
    """The dirent goes; the target's bytes stay; no version is captured."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"real.txt": b"TARGET"})
    live = snapshot_dir(tmp_path, community, server)
    (live / "link.txt").symlink_to("real.txt")
    before = await storage.current_generation(community, server)

    await storage.delete_file(community, server, RelPath("link.txt"))

    assert not (live / "link.txt").exists(follow_symlinks=False)
    assert (live / "real.txt").read_bytes() == b"TARGET"
    # A link has no content to retain, so nothing lands in the version ring.
    assert (
        await storage.list_file_versions(community, server, RelPath("link.txt")) == []
    )
    # An authoritative edit still bumps the generation.
    assert await storage.current_generation(community, server) == before + 1


async def test_delete_file_unlinks_a_dangling_link(tmp_path: Path) -> None:
    """A now-visible broken link is removable rather than a 404 (issue #2429)."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"keep": b"K"})
    live = snapshot_dir(tmp_path, community, server)
    (live / "broken").symlink_to("nowhere")

    await storage.delete_file(community, server, RelPath("broken"))

    assert not (live / "broken").exists(follow_symlinks=False)


async def test_delete_file_unlinks_a_looping_link(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"keep": b"K"})
    live = snapshot_dir(tmp_path, community, server)
    _plant_symlink_loop(live / "loop")

    await storage.delete_file(community, server, RelPath("loop"))

    assert not (live / "loop").exists(follow_symlinks=False)


async def test_delete_dir_on_a_link_to_a_directory_is_a_miss(tmp_path: Path) -> None:
    """A link is never a directory dirent, so delete_dir leaves the subtree."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"real/inner": b"X"})
    live = snapshot_dir(tmp_path, community, server)
    (live / "alias").symlink_to("real")

    with pytest.raises(NotFoundError):
        await storage.delete_dir(community, server, RelPath("alias"))

    assert (live / "real" / "inner").read_bytes() == b"X"
    assert (live / "alias").is_symlink()


async def test_write_file_on_a_link_is_refused_and_leaves_the_target(
    tmp_path: Path,
) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"real.txt": b"TARGET"})
    live = snapshot_dir(tmp_path, community, server)
    (live / "link.txt").symlink_to("real.txt")

    with pytest.raises(SymlinkRefusedError):
        await storage.write_file(community, server, RelPath("link.txt"), b"EDITED")

    assert (live / "real.txt").read_bytes() == b"TARGET"
    assert (live / "link.txt").is_symlink()


async def test_write_file_on_a_dangling_link_is_refused_not_materialized(
    tmp_path: Path,
) -> None:
    """A write must not follow the link and create the target it dangles at."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"keep": b"K"})
    live = snapshot_dir(tmp_path, community, server)
    (live / "broken").symlink_to("nowhere")

    with pytest.raises(SymlinkRefusedError):
        await storage.write_file(community, server, RelPath("broken"), b"EDITED")

    assert not (live / "nowhere").exists()


async def test_rename_file_with_a_link_source_is_refused(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"real.txt": b"TARGET"})
    live = snapshot_dir(tmp_path, community, server)
    (live / "link.txt").symlink_to("real.txt")

    with pytest.raises(SymlinkRefusedError):
        await storage.rename_file(
            community, server, RelPath("link.txt"), RelPath("moved.txt")
        )

    assert (live / "real.txt").read_bytes() == b"TARGET"
    assert (live / "link.txt").is_symlink()
    assert not (live / "moved.txt").exists(follow_symlinks=False)


async def test_rename_dir_with_a_link_source_is_refused(tmp_path: Path) -> None:
    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"real/inner": b"X"})
    live = snapshot_dir(tmp_path, community, server)
    (live / "alias").symlink_to("real")

    with pytest.raises(SymlinkRefusedError):
        await storage.rename_dir(community, server, RelPath("alias"), RelPath("moved"))

    assert (live / "real" / "inner").read_bytes() == b"X"
    assert (live / "alias").is_symlink()
    assert not (live / "moved").exists(follow_symlinks=False)


async def test_make_dir_onto_a_link_is_refused(tmp_path: Path) -> None:
    """make_dir onto a dangling link must not follow it and materialize a dir."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"keep": b"K"})
    live = snapshot_dir(tmp_path, community, server)
    (live / "broken").symlink_to("nowhere")

    with pytest.raises(SymlinkRefusedError):
        await storage.make_dir(community, server, RelPath("broken"))

    assert (live / "broken").is_symlink()
    assert not (live / "nowhere").exists()


async def test_retain_file_version_on_a_link_is_a_no_op(tmp_path: Path) -> None:
    """A link has no content to retain, so the version ring stays empty."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"real.txt": b"TARGET"})
    live = snapshot_dir(tmp_path, community, server)
    (live / "link.txt").symlink_to("real.txt")

    await storage.retain_file_version(community, server, RelPath("link.txt"))

    assert (
        await storage.list_file_versions(community, server, RelPath("link.txt")) == []
    )


async def test_a_leaf_link_occupies_its_name_so_a_rename_dest_never_clobbers(
    tmp_path: Path,
) -> None:
    """The never-clobber probe reports a link's name occupied (issue #2429).

    A rename destination is refused at the route by ``path_exists``; that a leaf
    link -- working or dangling -- is reported occupied is what keeps a rename
    from landing on the link's target.
    """

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"real.txt": b"TARGET"})
    live = snapshot_dir(tmp_path, community, server)
    (live / "link.txt").symlink_to("real.txt")
    (live / "broken").symlink_to("nowhere")

    assert await storage.path_exists(community, server, RelPath("link.txt"))
    assert await storage.path_exists(community, server, RelPath("broken"))


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
# An at-rest read of a symlink dirent is therefore REFUSED. One rule closes both,
# and it is the same convergence that decided the listing: the Worker already
# refuses to follow a symlink on read, #2393 already answers a dangling link and a
# loop as a miss on this very path, and a working set cannot legitimately contain
# a symlink anyway (uploads refuse symlink members, the Worker's snapshot tar
# skips them, hydrate rejects them).
#
# The refusal landed as the Port's own MISS and became the modelled
# ``SymlinkRefusedError`` in #2432: a miss told the operator a file was gone while
# the listing right above it showed the entry, whereas the refusal is the same 422
# ``symlink_refused`` sentence the running-server browser already shows.
#
# The resolution of the working-set root is never in question: ``current`` IS a
# symlink (``current -> snapshots/<id>``), but ``_current_dir`` readlinks it and
# hands the resolve the already-resolved snapshot DIRECTORY as its base, so no
# component under test here is the root's own link. Containment still runs first,
# so an escaping link stays a PathTraversalError.


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
    with pytest.raises(SymlinkRefusedError):
        await storage.read_file(community, server, RelPath("link.bin"))


async def test_open_file_stream_of_a_symlink_is_refused(tmp_path: Path) -> None:
    """The stream is the path that would emit the body under the wrong header."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"big.bin": b"B" * 1000})
    (snapshot_dir(tmp_path, community, server) / "link.bin").symlink_to("big.bin")

    with pytest.raises(SymlinkRefusedError):
        await drain(storage.open_file_stream(community, server, RelPath("link.bin")))


async def test_view_file_stream_of_a_symlink_is_refused(tmp_path: Path) -> None:
    """The pinned view reads the same way, so the export walk skips the link."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"big.bin": b"B" * 1000})
    (snapshot_dir(tmp_path, community, server) / "link.bin").symlink_to("big.bin")

    async with storage.open_working_set_view(community, server) as view:
        with pytest.raises(SymlinkRefusedError):
            await drain(view.open_file_stream(RelPath("link.bin")))


async def test_reading_a_dangling_symlink_is_refused_not_missed(
    tmp_path: Path,
) -> None:
    """A dangling link is a link, so it gets the link answer (issue #2432).

    Its answer moves with every other leaf link's: it used to be the miss, and the
    miss is exactly what made a visible-but-broken entry read as "not there". The
    Worker answers it the same way (its ``O_NOFOLLOW`` open reports ELOOP on a
    dangling link too, not ENOENT), so at rest and running agree here as well.
    """

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"x"})
    (snapshot_dir(tmp_path, community, server) / "broken").symlink_to("nowhere")

    with pytest.raises(SymlinkRefusedError):
        await storage.read_file(community, server, RelPath("broken"))


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


async def test_reading_through_an_intermediate_symlink_is_refused(
    tmp_path: Path,
) -> None:
    """Every read path refuses an intermediate link, not just the leaf (#2432).

    This test used to assert that ``alias/data`` streamed the real bytes, on the
    grounds that the listing showing ``data`` sized it truthfully. What it left was
    one request answering the same ``?path=alias/data`` with the real bytes at rest
    and 422 ``symlink_refused`` while running -- the Worker refuses at every
    component. The at-rest paths were the outlier, so all three converge here.
    """

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"real/data": b"inside"})
    live = snapshot_dir(tmp_path, community, server)
    (live / "alias").symlink_to("real")

    with pytest.raises(SymlinkRefusedError):
        await storage.read_file(community, server, RelPath("alias/data"))
    with pytest.raises(SymlinkRefusedError):
        await drain(storage.open_file_stream(community, server, RelPath("alias/data")))
    async with storage.open_working_set_view(community, server) as view:
        with pytest.raises(SymlinkRefusedError):
            await drain(view.open_file_stream(RelPath("alias/data")))


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


# --- a mutation's raw fs errnos become storage-domain outcomes (issue #2433) ---
#
# The read paths already model an over-long name as a miss (#2394); the MUTATIONS
# leaked the raw errno as a 500. ``RelPath`` bounds no length, so every case below
# is reachable from an ordinary client request (a long name pasted into a rename
# dialog). The vocabulary the mutation side maps onto:
#   over-long DESTINATION / component  -> NameTooLongError  (422 at the edge)
#   over-long SOURCE (delete, rename-from) -> NotFoundError (404, matching reads)
#   a non-directory occupying a needed parent / target -> PathOccupiedError (409)


async def test_over_long_destination_on_a_mutation_is_name_too_long(
    tmp_path: Path,
) -> None:
    """A destination past NAME_MAX is a modelled 422, never a bare ENAMETOOLONG 500."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"DATA", "d/inner": b"X"})
    rel = RelPath(_TOO_LONG)

    with pytest.raises(NameTooLongError):
        await storage.write_file(community, server, rel, b"NEW")
    with pytest.raises(NameTooLongError):
        await storage.make_dir(community, server, rel)
    with pytest.raises(NameTooLongError):
        await storage.rename_file(community, server, RelPath("f"), rel)
    with pytest.raises(NameTooLongError):
        await storage.rename_dir(community, server, RelPath("d"), rel)


async def test_over_long_source_on_a_mutation_is_a_miss(tmp_path: Path) -> None:
    """A source past NAME_MAX names nothing, so delete / rename-from is a miss (404)."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"DATA"})
    rel = RelPath(_TOO_LONG)

    with pytest.raises(NotFoundError):
        await storage.delete_file(community, server, rel)
    with pytest.raises(NotFoundError):
        await storage.delete_dir(community, server, rel)
    with pytest.raises(NotFoundError):
        await storage.rename_file(community, server, rel, RelPath("moved"))
    with pytest.raises(NotFoundError):
        await storage.rename_dir(community, server, rel, RelPath("moved"))


async def test_retain_file_version_of_an_over_long_source_is_a_silent_no_op(
    tmp_path: Path,
) -> None:
    """An over-long retain source names nothing to retain, so it is a no-op (#2433).

    ``retain_file_version``'s contract already makes a missing file a no-op; an
    over-long name is the same miss. It shares the ``_existing_file`` precheck the
    other over-long SOURCES use, so it returns silently rather than raising the bare
    ENAMETOOLONG ``Path.is_file`` throws (which would reach the edge as a 500). This
    pins that no-op: against the pre-fix code the ``retain_file_version`` call raises
    ENAMETOOLONG here instead of returning.
    """

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"f": b"DATA"})
    rel = RelPath(_TOO_LONG)

    # Returns silently (no raise) and captures nothing for the unnameable path.
    await storage.retain_file_version(community, server, rel)
    assert await storage.list_file_versions(community, server, rel) == []


async def test_a_file_occupied_parent_on_a_mutation_is_a_conflict(
    tmp_path: Path,
) -> None:
    """Writing / renaming under a name held by a regular file is a 409, not a 500.

    ``regular/child`` reaches its leaf through ``regular``, a plain file; creating
    the intermediate directory raises ENOTDIR (or EEXIST), which used to escape as
    a bare 500. It is the never-clobber's 409 conflict: a non-directory is in the
    way.
    """

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(
        storage, community, server, {"regular": b"X", "g": b"G", "dd/i": b"I"}
    )
    under_file = RelPath("regular/child")

    with pytest.raises(PathOccupiedError):
        await storage.write_file(community, server, under_file, b"NEW")
    with pytest.raises(PathOccupiedError):
        await storage.make_dir(community, server, under_file)
    with pytest.raises(PathOccupiedError):
        await storage.rename_file(community, server, RelPath("g"), under_file)
    with pytest.raises(PathOccupiedError):
        await storage.rename_dir(community, server, RelPath("dd"), under_file)

    # The blocking file and the untouched sources survive the refusals.
    live = snapshot_dir(tmp_path, community, server)
    assert (live / "regular").read_bytes() == b"X"
    assert (live / "g").read_bytes() == b"G"
    assert (live / "dd" / "i").read_bytes() == b"I"


async def test_make_dir_onto_a_file_is_a_conflict(tmp_path: Path) -> None:
    """``make_dir`` onto a name held by a file is a 409, not a raw EEXIST 500."""

    storage = FsStorage(tmp_path)
    community, server = new_scope()
    await publish(storage, community, server, {"occupied": b"X"})

    with pytest.raises(PathOccupiedError):
        await storage.make_dir(community, server, RelPath("occupied"))

    # The file is left intact (the refused mkdir never clobbered it).
    live = snapshot_dir(tmp_path, community, server)
    assert (live / "occupied").read_bytes() == b"X"
