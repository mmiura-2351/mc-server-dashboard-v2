"""Unit tests for FlagControlPlaneReadiness (issue #282)."""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.core.adapters.readiness import FlagControlPlaneReadiness


@pytest.mark.parametrize(
    ("enabled", "started", "expected"),
    [
        (False, False, True),  # disabled: trivially ready
        (False, True, True),
        (True, False, False),  # enabled but not started: not ready
        (True, True, True),  # enabled and started: ready
    ],
)
def test_is_ready(enabled: bool, started: bool, expected: bool) -> None:
    assert (
        FlagControlPlaneReadiness(enabled=enabled, started=started).is_ready()
        is expected
    )
