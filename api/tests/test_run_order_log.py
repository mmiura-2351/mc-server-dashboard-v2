"""The gate's api run records each test's worker, outcome and duration (#3750).

The gate's pytest output is dots only, so after a flake nothing said which
tests had run before the failing one on the same xdist worker. With
``--run-order-log=PATH`` the controller writes one tab-separated line per test,
``worker  outcome  duration  nodeid``, as each test finishes; each worker runs
its tests one at a time, so a worker's lines are in its run order.

Each test runs an inner pytest session that loads only the plugin, so the outer
session's own options do not affect the outcome. The inner tests record the
worker they ran on (xdist's ``PYTEST_XDIST_WORKER``) in a file named after the
test, which is what each line's worker is checked against.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests import run_order_log

pytest_plugins = ["pytester"]

_CONFTEST = """
import os
from pathlib import Path

import pytest

@pytest.fixture(autouse=True)
def _record_worker(request):
    path = Path(os.environ["RUN_ORDER_WORKERS"]) / request.node.name
    path.write_text(os.environ.get("PYTEST_XDIST_WORKER", "main"))
"""

_TESTS = """
import time

import pytest

@pytest.fixture
def broken():
    raise RuntimeError("setup fails")

@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("teardown fails")

@pytest.fixture
def slow_setup():
    time.sleep(0.1)

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

@pytest.mark.xfail
def test_xfails():
    assert False

@pytest.mark.xfail
def test_xpasses():
    pass

def test_sleeps(slow_setup):
    time.sleep(0.1)

@pytest.mark.parametrize("value", ["a b"])
def test_with_space(value):
    pass
"""

_EXPECTED_OUTCOMES = {
    "test_inner.py::test_passes": "passed",
    "test_inner.py::test_fails": "failed",
    "test_inner.py::test_skipped": "skipped",
    "test_inner.py::test_setup_error": "error",
    "test_inner.py::test_teardown_error": "error",
    "test_inner.py::test_xfails": "skipped",
    "test_inner.py::test_xpasses": "passed",
    "test_inner.py::test_sleeps": "passed",
    "test_inner.py::test_with_space[a b]": "passed",
}

# Each test takes its worker down; only an xdist run survives it. One crashes
# in its call after a slow setup, the other in its setup.
_CRASH = """
import os
import time

import pytest

@pytest.fixture
def slow_setup():
    time.sleep(0.3)

@pytest.fixture
def crash():
    os._exit(1)

def test_crashes(slow_setup):
    os._exit(1)

def test_crashes_in_setup(crash):
    pass
"""

# Runs after test_inner.py in a serial run: the lines for the tests before it
# must already be in the file, not in a write buffer.
_FLUSHED = """
import os
from pathlib import Path

def test_earlier_lines_are_already_written():
    log = Path(os.environ["RUN_ORDER_LOG"]).read_text()
    assert "test_inner.py::test_with_space[a b]" in log
"""


def _run(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    files: dict[str, str],
    *args: str,
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Run the inner session over ``files`` with the plugin.

    Returns the log's rows by node id, and the worker each test recorded by
    test name.
    """
    log = pytester.path / "run-order.tsv"
    workers = pytester.mkdir("workers")
    monkeypatch.setenv("PYTHONPATH", str(Path(run_order_log.__file__).parents[1]))
    monkeypatch.setenv("RUN_ORDER_LOG", str(log))
    monkeypatch.setenv("RUN_ORDER_WORKERS", str(workers))
    # Under the gate this test itself runs on an xdist worker, whose id a serial
    # inner session would otherwise inherit.
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(**files)
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
        *(f"{name}.py" for name in files),
    )
    lines = log.read_text().splitlines()
    rows = {row[3]: row for row in (line.split("\t") for line in lines)}
    assert len(rows) == len(lines), lines
    recorded = {path.name: path.read_text() for path in workers.iterdir()}
    return rows, recorded


def _assert_lines(
    rows: dict[str, list[str]], recorded: dict[str, str], expected: dict[str, str]
) -> None:
    """One line per test, with its outcome and the worker it recorded."""
    assert all(len(row) == 4 for row in rows.values()), rows
    assert {nodeid: row[1] for nodeid, row in rows.items()} == expected
    # Every test but the skipped one ran its fixtures and recorded its worker.
    assert set(recorded) == {nodeid.split("::")[1] for nodeid in expected} - {
        "test_skipped"
    }
    for nodeid, row in rows.items():
        name = nodeid.split("::")[1]
        if name in recorded:
            assert row[0] == recorded[name], (nodeid, row)
    assert float(rows["test_inner.py::test_sleeps"][2]) >= 0.2


def test_an_xdist_run_records_each_test_with_the_worker_that_ran_it(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, recorded = _run(
        pytester,
        monkeypatch,
        {"test_inner": _TESTS, "test_crash": _CRASH},
        "-n",
        "2",
    )

    # A crash in setup leaves no finished phase to take the worker from, though
    # the test recorded one before it crashed.
    assert rows.pop("test_crash.py::test_crashes_in_setup")[:3] == [
        "?",
        "crashed",
        "0.000",
    ]
    del recorded["test_crashes_in_setup"]
    crash = "test_crash.py::test_crashes"
    _assert_lines(rows, recorded, {**_EXPECTED_OUTCOMES, crash: "crashed"})
    # A crash in call counts the setup that finished before it.
    assert float(rows[crash][2]) >= 0.3
    # More than one worker ran tests, so a fixed worker name cannot pass.
    assert len({row[0] for row in rows.values()}) > 1


def test_a_serial_run_records_its_tests_in_run_order(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    flushed = "test_flushed.py::test_earlier_lines_are_already_written"
    rows, recorded = _run(
        pytester,
        monkeypatch,
        {"test_inner": _TESTS, "test_flushed": _FLUSHED},
    )

    _assert_lines(rows, recorded, {**_EXPECTED_OUTCOMES, flushed: "passed"})
    assert list(rows) == [*_EXPECTED_OUTCOMES, flushed]


def test_a_run_without_xdist_still_records_its_tests(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The crash hook is xdist's; without xdist pytest must not reject it.
    rows, _ = _run(
        pytester,
        monkeypatch,
        {"test_one": "def test_passes():\n    pass\n"},
        "-p",
        "no:xdist",
    )

    assert [row[:2] for row in rows.values()] == [["main", "passed"]]
