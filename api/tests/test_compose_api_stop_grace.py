"""The api service's stop grace stays above the scheduler's shutdown bound (#4109).

An API shutdown waits up to ``schedule.shutdown_drain_seconds`` for a scheduled
run in flight, and a backup it then cancels spends up to
``_DISCARD_TIMEOUT_SECONDS`` deleting its archive. Docker kills the process
when the api service's grace runs out: ``compose.yaml``'s ``stop_grace_period``
when one is set, and Docker's 10 s default otherwise (the api has none, by the
owner decision on #2934). A grace below those two would kill the API mid-drain,
and no cleanup would run.

This pins a necessary condition only, not that the whole teardown fits: the
teardown also waits for the Worker sessions' teardowns, bounded at 10 s plus
2 s (#3555), and a cancel during a backup's archive write is outside the 5 s
(#4186). The worker's own guard is ``TestComposeWorkerStopGraceCoversClose``.
"""

from __future__ import annotations

import re
from pathlib import Path

from mc_server_dashboard_api.config import ScheduleSettings
from mc_server_dashboard_api.servers.application import backups

_COMPOSE_FILE = Path(__file__).resolve().parents[2] / "compose.yaml"
# Docker's stop timeout for a service that sets no stop_grace_period.
_DOCKER_DEFAULT_GRACE_SECONDS = 10


def _api_stop_grace_seconds() -> int:
    """The api service's grace: its ``stop_grace_period``, or Docker's default."""

    lines = _COMPOSE_FILE.read_text().splitlines()
    start = lines.index("  api:")
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith("   "):
            break
        if not line.strip().startswith("stop_grace_period:"):
            continue
        match = re.fullmatch(r"    stop_grace_period: (\d+)s", line)
        # Any other spelling (1m30s, quoted, a trailing comment) is refused here
        # rather than read as no grace at all.
        assert match, f"cannot read the api service's grace: {line.strip()!r}"
        return int(match.group(1))
    return _DOCKER_DEFAULT_GRACE_SECONDS


def test_the_api_stop_grace_is_above_the_default_drain_and_the_cleanup() -> None:
    drain = ScheduleSettings().shutdown_drain_seconds

    assert drain + backups._DISCARD_TIMEOUT_SECONDS < _api_stop_grace_seconds()
