"""The api suite's tmp root sits on a tmpfs when one has room (issue #3722).

The fs-backed storage tests fsync for real, and under host I/O load a single
fsync on disk has blocked for over 120 s. ``pytest_configure`` therefore points
``TMPDIR`` (and with it pytest's basetemp) at ``/dev/shm`` when it is a writable
directory with room, or at ``MCSD_TEST_TMPDIR`` when that is set, and otherwise
keeps the default tmp dir.

Each test runs the selection against its own default, candidate tmpfs and
override directories, or in an inner pytest session, so the outer session's
own root, ``--basetemp`` and ``PYTEST_DEBUG_TEMPROOT`` do not affect the
outcome.
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests import conftest
from tests.conftest import _TMP_ROOT_ENV, _select_tmp_root

pytest_plugins = ["pytester"]


@dataclass(frozen=True)
class _Roots:
    default: Path
    candidate: Path
    override: Path


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Roots:
    """A distinct default, candidate tmpfs and override, all existing.

    The candidate has room by default. The monkeypatch records the environment
    and ``tempfile.tempdir`` first, so what the selection sets is undone.
    """
    roots = _Roots(*(tmp_path / name for name in ("default", "candidate", "override")))
    for root in (roots.default, roots.candidate, roots.override):
        root.mkdir()
    monkeypatch.delenv(_TMP_ROOT_ENV, raising=False)
    monkeypatch.setenv("TMPDIR", str(roots.default))
    monkeypatch.setattr(tempfile, "tempdir", str(roots.default))
    monkeypatch.setattr(conftest, "_TMPFS", roots.candidate)
    monkeypatch.setattr(conftest, "_TMPFS_MIN_FREE", 1)
    return roots


def _assert_selected(root: Path) -> None:
    assert os.environ[_TMP_ROOT_ENV] == str(root)
    assert os.environ["TMPDIR"] == str(root)
    assert tempfile.gettempdir() == str(root)


def test_a_tmpfs_with_room_is_selected(roots: _Roots) -> None:
    _select_tmp_root()

    _assert_selected(roots.candidate)


def test_a_tmpfs_without_room_keeps_the_default(
    roots: _Roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conftest, "_TMPFS_MIN_FREE", 1 << 62)

    _select_tmp_root()

    _assert_selected(roots.default)


def test_a_missing_tmpfs_keeps_the_default(
    roots: _Roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conftest, "_TMPFS", roots.candidate / "absent")

    _select_tmp_root()

    _assert_selected(roots.default)


def test_the_override_wins_over_a_tmpfs_with_room(
    roots: _Roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_TMP_ROOT_ENV, str(roots.override))

    _select_tmp_root()

    _assert_selected(roots.override)


def test_an_override_that_is_not_a_directory_is_refused(
    roots: _Roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_TMP_ROOT_ENV, str(roots.override / "absent"))

    with pytest.raises(pytest.UsageError):
        _select_tmp_root()


def test_a_session_without_basetemp_puts_tmp_path_under_the_selected_root(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inner pytest session that loads this conftest, with no ``--basetemp``
    (which ``runpytest_subprocess`` would add) and no ``PYTEST_DEBUG_TEMPROOT``
    (which ``pytester`` sets), roots its ``tmp_path`` at the selected dir."""
    root = pytester.mkdir("root").resolve()
    monkeypatch.delenv("PYTEST_DEBUG_TEMPROOT", raising=False)
    # The inner session must not create a scratch database of its own.
    monkeypatch.delenv("MCD_TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("MCD_TEST_DATABASE_URL_BASE", raising=False)
    monkeypatch.setenv(_TMP_ROOT_ENV, str(root))
    monkeypatch.setenv("PYTHONPATH", str(Path(conftest.__file__).parents[1]))
    pytester.makepyfile(
        f"""
        from pathlib import Path

        def test_tmp_path(tmp_path):
            assert tmp_path.is_relative_to(Path({str(root)!r}))
        """
    )

    result = pytester.run(
        sys.executable, "-m", "pytest", "-p", "tests.conftest", "-p", "no:cacheprovider"
    )

    result.assert_outcomes(passed=1)
