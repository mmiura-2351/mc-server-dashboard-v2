"""Record which tests ran, on which xdist worker, in what order (issue #3750).

The gate's pytest output is dots only, so after a flake nothing said which tests
had run before the failing one on the same worker. ``--run-order-log=PATH``
writes one tab-separated line per test as it finishes::

    worker  outcome  duration  nodeid

``worker`` is the xdist gateway id (``gw0``...), or ``main`` in a serial run.
``outcome`` is ``passed``, ``failed`` or ``skipped`` as the test's call
reported it, ``error`` when its setup or teardown failed, and ``crashed`` when
its worker died mid-test. ``duration`` is in seconds, summed over setup, call
and teardown. Each worker runs its tests one at a time, so a worker's lines are
in its run order. The node id is last because a parametrize id may contain
spaces.

Only the xdist controller writes: it sees every worker's reports, including the
one xdist makes up for a test whose worker crashed. Each line is flushed as it
is written, so a killed run leaves the record up to that point.
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
    if report.when == "call":
        return report.outcome
    if report.when not in ("setup", "teardown"):
        return "crashed"
    if report.failed:
        return "error"
    return report.outcome


class _RunOrderLog:
    def __init__(self, file: TextIO) -> None:
        self._file = file
        # Tests whose teardown has not been reported yet, by (worker, node id).
        self._running: dict[tuple[str, str], tuple[str, float]] = {}

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        node = getattr(report, "node", None)
        worker = "main" if node is None else str(node.gateway.id)
        key = (worker, report.nodeid)
        outcome, duration = self._running.pop(key, ("passed", 0.0))
        if outcome == "passed":
            outcome = _outcome(report)
        duration += report.duration
        if report.when in ("setup", "call"):
            self._running[key] = (outcome, duration)
            return
        self._file.write(f"{worker}\t{outcome}\t{duration:.3f}\t{report.nodeid}\n")

    def pytest_unconfigure(self) -> None:
        self._file.close()
