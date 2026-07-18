"""Argument handling for the integrity-sweep admin command (issue #744).

The wiring + DB/storage round-trip lives behind ``run`` (DB-gated); these unit
tests pin only the ``main`` argument parsing — the default (every server), the
``--server`` scoping, and an invalid-uuid rejection — by stubbing ``run`` so no
database is touched.
"""

from __future__ import annotations

import uuid

import pytest

from mc_server_dashboard_api import integrity_sweep_cli
from mc_server_dashboard_api.servers.application.integrity_sweep import SweepSummary
from mc_server_dashboard_api.servers.domain.value_objects import ServerId

_EMPTY = SweepSummary(
    servers_scanned=0,
    backups_healthy=0,
    backups_quarantined=0,
    backups_dangling=0,
    snapshots_scanned=0,
    snapshots_flagged=0,
)


def test_main_with_no_args_sweeps_every_server(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[ServerId | None] = []

    async def _fake_run(*, server_id: ServerId | None) -> SweepSummary:
        seen.append(server_id)
        return _EMPTY

    monkeypatch.setattr(integrity_sweep_cli, "run", _fake_run)
    assert integrity_sweep_cli.main([]) == 0
    assert seen == [None]


def test_main_scopes_to_a_single_server(monkeypatch: pytest.MonkeyPatch) -> None:
    sid = uuid.uuid4()
    seen: list[ServerId | None] = []

    async def _fake_run(*, server_id: ServerId | None) -> SweepSummary:
        seen.append(server_id)
        return _EMPTY

    monkeypatch.setattr(integrity_sweep_cli, "run", _fake_run)
    assert integrity_sweep_cli.main(["--server", str(sid)]) == 0
    assert seen == [ServerId(sid)]


def test_main_rejects_an_invalid_server_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_run(*, server_id: ServerId | None) -> SweepSummary:
        raise AssertionError("run must not be called on a bad uuid")

    monkeypatch.setattr(integrity_sweep_cli, "run", _fake_run)
    assert integrity_sweep_cli.main(["--server", "not-a-uuid"]) == 2
