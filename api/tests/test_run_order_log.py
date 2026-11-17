"""The gate's api run records each test's worker, outcome and duration (#3750).

The gate's pytest output is dots only, so after a flake nothing said which
tests had run before the failing one on the same xdist worker. With
``--run-order-log=PATH`` the controller writes one tab-separated line per test,
``worker  outcome  duration  nodeid``, as each test finishes; each worker runs
its tests one at a time, so a worker's lines are in its run order.

Each test runs an inner pytest session that loads only the plugin, so the outer
session's own options do not affect the outcome.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests import run_order_log

pytest_plugins = ["pytester"]

_TESTS = """
import pytest

@pytest.fixture
def broken():
    raise RuntimeError("setup fails")

@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("teardown fails")

def test_passes():
    pass

def test_fails():
    assert False

@pytest.mark.skip(reason="skipped")
def test_skipped():
    pass

def test_setup_error(broken):
    pass

def test_teardown_error(broken_teardown):
    pass

@pytest.mark.parametrize("value", ["a b"])
def test_with_space(value):
    pass
"""


def _run(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, *args: str
) -> list[list[str]]:
    """Run the inner session with the plugin and return the log's rows."""
    monkeypatch.setenv("PYTHONPATH", str(Path(run_order_log.__file__).parents[1]))
    pytester.makepyfile(test_inner=_TESTS)
    log = pytester.path / "run-order.tsv"
    pytester.run(
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "tests.run_order_log",
        "-p",
        "no:cacheprovider",
        f"--run-order-log={log}",
        *args,
    )
    return [line.split("\t") for line in log.read_text().splitlines()]


_EXPECTED_OUTCOMES = {
    "test_inner.py::test_passes": "passed",
    "test_inner.py::test_fails": "failed",
    "test_inner.py::test_skipped": "skipped",
    "test_inner.py::test_setup_error": "error",
    "test_inner.py::test_teardown_error": "error",
    "test_inner.py::test_with_space[a b]": "passed",
}


def test_an_xdist_run_records_one_line_per_test_with_its_worker(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _run(pytester, monkeypatch, "-n", "2")

    assert all(len(row) == 4 for row in rows), rows
    assert {row[3]: row[1] for row in rows} == _EXPECTED_OUTCOMES
    assert len(rows) == len(_EXPECTED_OUTCOMES)
    assert {row[0] for row in rows} <= {"gw0", "gw1"}
    assert all(float(row[2]) >= 0 for row in rows)


def test_a_serial_run_records_its_tests_in_run_order(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _run(pytester, monkeypatch)

    assert [row[3] for row in rows] == list(_EXPECTED_OUTCOMES)
    assert {row[0] for row in rows} == {"main"}
