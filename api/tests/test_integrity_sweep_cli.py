"""Argument handling for the integrity-sweep admin command (issue #744).

The wiring + DB/storage round-trip lives behind ``run`` (DB-gated); these unit
tests pin only the ``main`` argument parsing — the default (every server), the
``--server`` scoping, and an invalid-uuid rejection — by stubbing ``run`` so no
database is touched.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from mc_server_dashboard_api import integrity_sweep_cli
from mc_server_dashboard_api.servers.application.integrity_sweep import SweepSummary
from mc_server_dashboard_api.servers.domain.errors import (
    BackupStorageUnavailableError,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId

_EMPTY = SweepSummary(
    servers_scanned=0,
    backups_healthy=0,
    backups_quarantined=0,
    backups_unreadable=0,
    backups_dangling=0,
    snapshots_scanned=0,
    snapshots_flagged=0,
    snapshots_not_examined=0,
)


def test_main_prints_the_not_examined_snapshot_count(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The summary reports snapshots the backend never examined apart from the
    scanned ones (issue #2377), so "0 flagged" cannot read as a real verdict."""

    summary = replace(_EMPTY, servers_scanned=3, snapshots_not_examined=3)

    async def _fake_run(*, server_id: ServerId | None) -> SweepSummary:
        return summary

    monkeypatch.setattr(integrity_sweep_cli, "run", _fake_run)
    assert integrity_sweep_cli.main([]) == 0

    out = capsys.readouterr().out
    assert "snapshots scanned: 0" in out
    assert "snapshots not examined (backend does not fsck snapshots at rest): 3" in out


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


def test_main_reports_a_store_outage_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A store outage stops the pass (issue #2371) — an outage is no verdict about a
    backup. That is an expected operator-facing outcome, so it must exit non-zero
    with a message naming the backup, not with a bare traceback."""

    async def _fake_run(*, server_id: ServerId | None) -> SweepSummary:
        raise BackupStorageUnavailableError("abc123ref")

    monkeypatch.setattr(integrity_sweep_cli, "run", _fake_run)

    assert integrity_sweep_cli.main([]) == 1

    err = capsys.readouterr().err
    assert "integrity sweep aborted" in err
    assert "abc123ref" in err  # names which backup the pass died on
    assert "Traceback" not in err


def test_main_rejects_an_invalid_server_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_run(*, server_id: ServerId | None) -> SweepSummary:
        raise AssertionError("run must not be called on a bad uuid")

    monkeypatch.setattr(integrity_sweep_cli, "run", _fake_run)
    assert integrity_sweep_cli.main(["--server", "not-a-uuid"]) == 2
