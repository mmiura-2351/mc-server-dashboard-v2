"""The api suite's tmp root sits on a tmpfs when one has room (issue #3722).

The fs-backed storage tests fsync for real, and under host I/O load a single
fsync on disk has blocked for over 120 s. ``pytest_configure`` therefore points
``TMPDIR`` (and with it pytest's basetemp) at ``/dev/shm`` when it is a writable
directory with room, or at ``MCSD_TEST_TMPDIR`` when that is set, and otherwise
keeps the default tmp dir.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from tests.conftest import _TMP_ROOT_ENV, _tmpfs_or_default


def test_a_writable_tmpfs_with_room_is_chosen(tmp_path: Path) -> None:
    assert _tmpfs_or_default(tmp_path, min_free=1) == str(tmp_path)


def test_a_tmpfs_without_room_falls_back_to_the_default(tmp_path: Path) -> None:
    assert _tmpfs_or_default(tmp_path, min_free=1 << 62) == tempfile.gettempdir()


def test_a_missing_tmpfs_falls_back_to_the_default(tmp_path: Path) -> None:
    assert _tmpfs_or_default(tmp_path / "absent", min_free=1) == tempfile.gettempdir()


def test_tmp_path_and_tempfile_share_the_selected_root(tmp_path: Path) -> None:
    root = os.environ[_TMP_ROOT_ENV]
    assert tempfile.gettempdir() == root
    assert tmp_path.is_relative_to(Path(root).resolve())
