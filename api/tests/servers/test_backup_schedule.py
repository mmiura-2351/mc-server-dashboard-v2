"""Unit tests for the pure scheduled-backup math (FR-BAK-3).

Standard-library-only domain math: read/validate the per-server interval from
config (hours -> seconds), and the deterministic per-server jitter bound.
"""

from __future__ import annotations

import uuid

import pytest

from mc_server_dashboard_api.servers.domain.backup_schedule import (
    BACKUP_INTERVAL_CONFIG_KEY,
    JITTER_FRACTION,
    jitter_seconds,
    schedule_from_config,
)
from mc_server_dashboard_api.servers.domain.errors import (
    InvalidBackupScheduleError,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId


def test_absent_schedule_returns_none() -> None:
    assert schedule_from_config({}) is None


def test_valid_hours_returns_seconds() -> None:
    assert schedule_from_config({BACKUP_INTERVAL_CONFIG_KEY: 6}) == 6 * 3600


@pytest.mark.parametrize("bad", [0, -1, "6", 1.5, True])
def test_invalid_schedule_rejected(bad: object) -> None:
    with pytest.raises(InvalidBackupScheduleError):
        schedule_from_config({BACKUP_INTERVAL_CONFIG_KEY: bad})


def test_jitter_is_deterministic_per_server() -> None:
    server_id = ServerId(uuid.uuid4())
    a = jitter_seconds(server_id, interval_seconds=3600)
    b = jitter_seconds(server_id, interval_seconds=3600)
    assert a == b


def test_jitter_within_bound() -> None:
    server_id = ServerId(uuid.uuid4())
    interval = 3600
    offset = jitter_seconds(server_id, interval_seconds=interval)
    assert 0 <= offset < interval * JITTER_FRACTION
