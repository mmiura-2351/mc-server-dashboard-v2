"""The api service's stop grace covers the scheduler's shutdown drain (#4109).

An API shutdown waits up to ``schedule.shutdown_drain_seconds`` for a scheduled
backup in flight, and a backup it then cancels spends up to
``_DISCARD_TIMEOUT_SECONDS`` deleting its archive. ``compose.yaml``'s
``stop_grace_period`` on the api service is when Docker kills the process: one
below those two would kill the API mid-drain, and no cleanup would run. The
worker's own guard is ``TestComposeWorkerStopGraceCoversClose``.
"""

from __future__ import annotations

import re
from pathlib import Path

from mc_server_dashboard_api.config import ScheduleSettings
from mc_server_dashboard_api.servers.application import backups

_COMPOSE_FILE = Path(__file__).resolve().parents[2] / "compose.yaml"


def _api_stop_grace_seconds() -> int:
    lines = _COMPOSE_FILE.read_text().splitlines()
    start = lines.index("  api:")
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith("   "):
            break
        match = re.fullmatch(r"    stop_grace_period: (\d+)s", line)
        if match:
            return int(match.group(1))
    raise AssertionError("compose.yaml has no stop_grace_period on the api service")


def test_the_api_stop_grace_covers_the_drain_and_the_cleanup() -> None:
    drain = ScheduleSettings().shutdown_drain_seconds

    assert _api_stop_grace_seconds() > drain + backups._DISCARD_TIMEOUT_SECONDS
