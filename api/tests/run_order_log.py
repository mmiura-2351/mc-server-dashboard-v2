"""Record which tests ran, on which xdist worker, in what order (issue #3750).

The gate's pytest output is dots only, so after a flake nothing said which tests
had run before the failing one on the same worker. ``--run-order-log=PATH``
writes one tab-separated line per test as it finishes::

    worker  outcome  duration  nodeid

``worker`` is the xdist worker id (``gw0``...), or ``main`` in a serial run.
``outcome`` is ``passed``, ``failed`` or ``skipped`` as the test's call
reported it (so an xfailed test is ``skipped`` and an xpassed one ``passed``),
``error`` when its setup or teardown failed, and ``crashed`` when its worker
died mid-test. ``duration`` is in seconds, summed over setup, call and teardown.
Each worker runs its tests one at a time, so a worker's lines are in its run
order. The node id is last because a parametrize id may contain spaces.

A crashed test's line carries the worker and duration of the phases it
finished; the phase that crashed reports neither, and a crash during setup
leaves the worker as ``?``.

Only the xdist controller writes: it sees every worker's reports, and xdist
tells it about a crashed worker through ``pytest_handlecrashitem``. Each line
is flushed as it is written, so a run killed from outside leaves a line for
every test that finished. A test still running then has none, and the last
line of each worker shows where it stopped. A test that pytest-timeout stops
fails like any other and has its line.
"""

from __future__ import annotations

from typing import TextIO

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-order-log",
        metavar="PATH",
        default=None,
        help="Write each test's worker, outcome and duration to PATH, in run order.",
    )


def pytest_configure(config: pytest.Config) -> None:
    path = config.getoption("run_order_log")
    if path is None or hasattr(config, "workerinput"):
        return
    config.pluginmanager.register(
        _RunOrderLog(open(path, "w", encoding="utf-8", buffering=1))
    )


def _outcome(report: pytest.TestReport) -> str:
    if report.when == "call" or not report.failed:
        return report.outcome
    return "error"


class _RunOrderLog:
    def __init__(self, file: TextIO) -> None:
        self._file = file
        # Tests whose teardown has not been reported yet, by node id: the
        # worker, the outcome so far and the duration so far.
        self._running: dict[str, tuple[str, str, float]] = {}

    def _write(self, worker: str, outcome: str, duration: float, nodeid: str) -> None:
        self._file.write(f"{worker}\t{outcome}\t{duration:.3f}\t{nodeid}\n")

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        worker = str(getattr(report, "worker_id", "main"))
        _, outcome, duration = self._running.pop(report.nodeid, (worker, "passed", 0.0))
        if outcome == "passed":
            outcome = _outcome(report)
        duration += report.duration
        if report.when == "teardown":
            self._write(worker, outcome, duration, report.nodeid)
        else:
            self._running[report.nodeid] = (worker, outcome, duration)

    @pytest.hookimpl(optionalhook=True)
    def pytest_handlecrashitem(self, crashitem: str) -> None:
        worker, _, duration = self._running.pop(crashitem, ("?", "", 0.0))
        self._write(worker, "crashed", duration, crashitem)
        # xdist then reports a made-up failure for the test, with ``when`` set
        # to ``"???"``. It is not a teardown, so it is never written.

    def pytest_unconfigure(self) -> None:
        self._file.close()
