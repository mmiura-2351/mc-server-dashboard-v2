"""The fs pack walks a tree with an explicit stack (issue #3859).

``_add_plain`` packs a working set for hydrate, backup create and prune. It used
``tarfile.add``'s own walk, which recursed once a directory level and so raised
``RecursionError`` for a tree about 1,000 levels deep. The walk that replaced it
must write the same tar: these tests pack an ordinary tree both ways, through
the pack's real entry points, and compare the bytes.
"""

from __future__ import annotations

import gzip
import io
import os
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest

from mc_server_dashboard_api.storage.adapters import fs


def _recursive_add_plain(tar: tarfile.TarFile, directory: Path, child: Path) -> None:
    """``_add_plain`` as it was before issue #3859, ``tarfile.add`` recursing."""

    def _plain(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if info.islnk():
            info.type = tarfile.REGTYPE
            info.linkname = ""
            info.size = os.lstat(directory / info.name).st_size
        return info if info.isfile() or info.isdir() else None

    tar.add(child, arcname=child.name, filter=_plain)


# Each file with its mode. ``a``, ``a-b.dat`` and ``a.b.dat`` sort one way by
# name and another by path; ``B.txt`` sorts before ``a.txt``.
_FILES = {
    "B.txt": (b"upper", 0o644),
    "a.txt": (b"lower", 0o600),
    "plugins/run.sh": (b"#!/bin/sh\n", 0o755),
    "server.jar": (b"jar", 0o644),
    "server.properties": (b"motd=hi\n", 0o640),
    "world/a/b.dat": (b"nested", 0o644),
    "world/a-b.dat": (b"dash", 0o644),
    "world/a.b.dat": (b"dot", 0o644),
    "world/level.dat": (b"level" * 300, 0o644),
    "world/region/r.-1.0.mca": (bytes(4096), 0o644),
    "world/region/r.0.0.mca": (b"\x01" * 5000, 0o644),
}
# Every directory with its mode; ``logs`` and ``world/empty`` stay empty.
_DIRECTORIES = {
    "logs": 0o700,
    "plugins": 0o755,
    "world": 0o711,
    "world/a": 0o755,
    "world/empty": 0o750,
    "world/region": 0o770,
}


def _ordinary_tree(root: Path) -> Path:
    """A working set holding what a pack meets: nested and empty directories,
    modes and mtimes, a hard-linked file, and symlinks, to a file and to a
    directory, and a FIFO, which the pack leaves out and does not enter."""

    for name in _DIRECTORIES:
        (root / name).mkdir(parents=True)
    for name, (data, mode) in _FILES.items():
        (root / name).write_bytes(data)
        (root / name).chmod(mode)
    os.link(root / "world/level.dat", root / "plugins/level.dat")
    (root / "world/latest").symlink_to("level.dat")
    (root / "world/regions").symlink_to("region", target_is_directory=True)
    os.mkfifo(root / "world/pipe")
    # Deepest first, so setting a directory's mtime is not undone below it.
    paths = sorted(root.rglob("*"), key=lambda p: -len(p.parts))
    for offset, path in enumerate(paths):
        if not path.is_symlink():
            os.utime(path, (1_600_000_000 + offset, 1_600_000_000 + offset))
    for name, mode in _DIRECTORIES.items():
        (root / name).chmod(mode)
    return root


def _hydrate_pack(tree: Path, out: Path) -> bytes:
    """What hydrate streams of ``tree``, ``server.jar`` excluded as the
    injection of a resolved JAR excludes it (issue #1942)."""

    holder: list[BaseException] = []
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    fs._tar_into_fd(tree, fd, None, holder, frozenset({"server.jar"}), lambda: None)
    assert holder == []
    return out.read_bytes()


def _backup_pack(tree: Path, out: Path) -> bytes:
    """The tar backup create and prune write of ``tree``, decompressed."""

    fs._write_tar_gz(tree, out)
    return gzip.decompress(out.read_bytes())


# What hydrate streams, in order: each directory before its entries, sorted by
# name, so ``world/a/b.dat`` comes before ``world/a-b.dat``.
_HYDRATED = [
    "B.txt",
    "a.txt",
    "logs",
    "plugins",
    "plugins/level.dat",
    "plugins/run.sh",
    "server.properties",
    "world",
    "world/a",
    "world/a/b.dat",
    "world/a-b.dat",
    "world/a.b.dat",
    "world/empty",
    "world/level.dat",
    "world/region",
    "world/region/r.-1.0.mca",
    "world/region/r.0.0.mca",
]
_PACKS: dict[str, tuple[Callable[[Path, Path], bytes], list[str]]] = {
    "hydrate": (_hydrate_pack, _HYDRATED),
    "backup-and-prune": (_backup_pack, [*_HYDRATED[:6], "server.jar", *_HYDRATED[6:]]),
}


@pytest.mark.parametrize("pack", list(_PACKS))
def test_the_walk_packs_the_tar_the_recursive_add_packed(
    pack: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The walk writes, byte for byte, the tar ``tarfile.add``'s recursion
    wrote: the same members in the same order, with the same modes, mtimes and
    data, empty directories kept, the hard link as a regular file, and the
    symlinks, the FIFO and an excluded name left out."""

    run, names = _PACKS[pack]
    tree = _ordinary_tree(tmp_path / "tree")

    walked = run(tree, tmp_path / "walked")
    monkeypatch.setattr(fs, "_add_plain", _recursive_add_plain)
    recursed = run(tree, tmp_path / "recursed")

    assert walked == recursed
    with tarfile.open(fileobj=io.BytesIO(walked)) as tar:
        members = tar.getmembers()
    assert [m.name for m in members] == names
    assert {m.name: m.mode for m in members if m.isdir()} == _DIRECTORIES
    assert all(m.isfile() or m.isdir() for m in members)
