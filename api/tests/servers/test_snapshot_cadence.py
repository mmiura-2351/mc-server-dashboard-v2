"""Unit tests for the pure snapshot-cadence scheduling math (FR-DATA-7).

These pin the effective-interval and jitter functions in the servers domain: a
per-server override replaces the global default, both are clamped to the floor,
and the jitter is deterministic from the server id and bounded by a fraction of
the interval. No I/O, no clock — pure functions.
"""

from __future__ import annotations

import uuid

import pytest

from mc_server_dashboard_api.servers.domain.errors import (
    InvalidSnapshotIntervalError,
)
from mc_server_dashboard_api.servers.domain.snapshot_cadence import (
    JITTER_FRACTION,
    SNAPSHOT_INTERVAL_CONFIG_KEY,
    effective_interval_seconds,
    jitter_seconds,
    override_from_config,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId


def _sid(hexdigits: str) -> ServerId:
    return ServerId(uuid.UUID(hexdigits))


def test_uses_default_when_no_override() -> None:
    assert effective_interval_seconds(override=None, default=3600, floor=300) == 3600


def test_override_replaces_default() -> None:
    assert effective_interval_seconds(override=1800, default=3600, floor=300) == 1800


def test_override_clamped_up_to_floor() -> None:
    assert effective_interval_seconds(override=60, default=3600, floor=300) == 300


def test_default_below_floor_is_clamped_too() -> None:
    # A misconfigured global default below the floor is also clamped, so the
    # floor is an absolute guarantee against thrash (CONFIGURATION.md 5.4).
    assert effective_interval_seconds(override=None, default=100, floor=300) == 300


def test_jitter_is_within_bound() -> None:
    interval = 3600
    bound = interval * JITTER_FRACTION
    for i in range(50):
        sid = _sid(f"{i:032x}")
        j = jitter_seconds(sid, interval_seconds=interval)
        assert 0.0 <= j < bound


def test_jitter_is_deterministic_per_server() -> None:
    sid = _sid("00000000000000000000000000000001")
    assert jitter_seconds(sid, interval_seconds=3600) == jitter_seconds(
        sid, interval_seconds=3600
    )


def test_jitter_differs_across_servers() -> None:
    a = jitter_seconds(_sid("00000000000000000000000000000001"), interval_seconds=3600)
    b = jitter_seconds(_sid("00000000000000000000000000000002"), interval_seconds=3600)
    assert a != b


def test_override_absent_is_none() -> None:
    assert override_from_config({}, floor=300) is None


def test_override_present_returned() -> None:
    cfg = {SNAPSHOT_INTERVAL_CONFIG_KEY: 1800}
    assert override_from_config(cfg, floor=300) == 1800


def test_override_below_floor_rejected() -> None:
    cfg = {SNAPSHOT_INTERVAL_CONFIG_KEY: 60}
    with pytest.raises(InvalidSnapshotIntervalError):
        override_from_config(cfg, floor=300)


def test_override_non_integer_rejected() -> None:
    cfg = {SNAPSHOT_INTERVAL_CONFIG_KEY: "fast"}
    with pytest.raises(InvalidSnapshotIntervalError):
        override_from_config(cfg, floor=300)


def test_override_boolean_rejected() -> None:
    # bool is an int subclass in Python; reject it explicitly so True/False do
    # not slip through as 1/0.
    cfg = {SNAPSHOT_INTERVAL_CONFIG_KEY: True}
    with pytest.raises(InvalidSnapshotIntervalError):
        override_from_config(cfg, floor=300)
