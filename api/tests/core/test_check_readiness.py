"""Unit tests for the CheckReadiness use case (issue #282)."""

from __future__ import annotations

import pytest

from mc_server_dashboard_api.core.application.check_readiness import CheckReadiness
from mc_server_dashboard_api.core.domain.health import DatabasePing
from mc_server_dashboard_api.core.domain.readiness import ControlPlaneReadiness


class _FakePing(DatabasePing):
    def __init__(self, *, reachable: bool) -> None:
        self._reachable = reachable

    async def is_reachable(self) -> bool:
        return self._reachable


class _FakeControlPlane(ControlPlaneReadiness):
    def __init__(self, *, ready: bool) -> None:
        self._ready = ready

    def is_ready(self) -> bool:
        return self._ready


@pytest.mark.asyncio
async def test_ready_when_all_components_ready() -> None:
    report = await CheckReadiness(
        database=_FakePing(reachable=True),
        control_plane=_FakeControlPlane(ready=True),
    )()
    assert report.ready is True
    assert {c.name: c.ready for c in report.components} == {
        "database": True,
        "control_plane": True,
    }


@pytest.mark.asyncio
async def test_not_ready_when_database_down() -> None:
    report = await CheckReadiness(
        database=_FakePing(reachable=False),
        control_plane=_FakeControlPlane(ready=True),
    )()
    assert report.ready is False
    assert {c.name: c.ready for c in report.components} == {
        "database": False,
        "control_plane": True,
    }


@pytest.mark.asyncio
async def test_not_ready_when_control_plane_not_started() -> None:
    report = await CheckReadiness(
        database=_FakePing(reachable=True),
        control_plane=_FakeControlPlane(ready=False),
    )()
    assert report.ready is False
    assert {c.name: c.ready for c in report.components}["control_plane"] is False
