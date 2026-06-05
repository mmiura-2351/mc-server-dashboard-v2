"""fs-specific file ops: symlink traversal containment + version-id ordering.

The backend-agnostic file read/edit/version/rollback contract is in
``test_port_contract.py`` (run against both adapters). This file keeps only the
fs realization details that reach into the filesystem: symlink-escape rejection
(object storage has no symlinks, Section 6/7.3) and the fs version-id ordering /
oldest-pruning, which depend on the fs module internals.
"""

from __future__ import annotations

import io
import os
import tarfile
import time
from pathlib import Path

import pytest

from mc_server_dashboard_api.storage.adapters import fs as fs_module
from mc_server_dashboard_api.storage.adapters.fs import (
    _DEFAULT_MAX_RESTORE_BYTES,
    FsStorage,
    _extract_tar_gz_into,
    _new_version_id,
)
from mc_server_dashboard_api.storage.domain.errors import PathTraversalError
from mc_server_dashboard_api.storage.domain.value_objects import RelPath
from tests.storage.helpers import new_scope, publish, snapshot_dir


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
