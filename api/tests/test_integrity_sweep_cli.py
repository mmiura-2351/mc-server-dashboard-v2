"""Argument handling for the integrity-sweep admin command (issue #744).

The wiring + DB/storage round-trip lives behind ``run`` (DB-gated); these unit
tests pin only the ``main`` argument parsing — the default (every server), the
``--server`` scoping, and an invalid-uuid rejection — by stubbing ``run`` so no
database is touched.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace

import pytest
from sqlalchemy.exc import DBAPIError, InterfaceError

from mc_server_dashboard_api import integrity_sweep_cli
from mc_server_dashboard_api.servers.application.integrity_sweep import SweepSummary
from mc_server_dashboard_api.servers.domain.errors import (
    BackupStorageUnavailableError,
    LifecycleLockLostError,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId

_EMPTY = SweepSummary(
    servers_scanned=0,
    backups_healthy=0,
    backups_quarantined=0,
    backups_unreadable=0,
    backups_dangling=0,
    backups_skipped_busy=0,
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


def test_main_prints_the_busy_skipped_backup_count(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A backup skipped because its server's lifecycle lock was busy (issue
    #3220) is reported on its own line, so the operator knows to re-run."""

    summary = replace(_EMPTY, servers_scanned=2, backups_skipped_busy=1)

    async def _fake_run(*, server_id: ServerId | None) -> SweepSummary:
        return summary

    monkeypatch.setattr(integrity_sweep_cli, "run", _fake_run)
    assert integrity_sweep_cli.main([]) == 0

    out = capsys.readouterr().out
    assert "servers scanned: 2" in out
    assert "backups skipped (server busy; re-run the sweep to check them): 1" in out


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


@pytest.mark.parametrize(
    "error",
    [
        # A connection the database closed between two statements.
        InterfaceError("SELECT 1", None, Exception("connection is closed")),
        # A statement the database cut off in flight: the asyncpg dialect has no
        # narrower class for that error, so it raises the bare DBAPIError.
        DBAPIError(
            "SELECT 1",
            None,
            Exception(
                "connection was closed in the middle of operation: terminating "
                "connection due to administrator command"
            ),
        ),
    ],
    ids=["connection-closed", "terminated-in-flight"],
)
def test_main_reports_a_database_failure_cleanly(
    error: DBAPIError,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A database failure mid-sweep stops the pass (issue #3942). It is an
    operator-facing outcome like a store outage, so it exits with its own status
    and a message saying the re-run is safe; the traceback is logged at DEBUG
    only."""

    async def _fake_run(*, server_id: ServerId | None) -> SweepSummary:
        raise error

    monkeypatch.setattr(integrity_sweep_cli, "run", _fake_run)
    caplog.set_level(logging.DEBUG, logger=integrity_sweep_cli.__name__)

    assert integrity_sweep_cli.main([]) == 3

    err = capsys.readouterr().err
    assert "integrity sweep aborted: the database failed" in err
    assert f"{type(error).__name__}: {error.orig}" in err  # says why
    assert "the sweep stopped" in err
    assert "Backups already classified this run keep their verdict" in err
    assert "safe to re-run the sweep" in err
    assert "Traceback" not in err
    assert [r.levelno for r in caplog.records if r.exc_info] == [logging.DEBUG]


def test_main_reports_a_lost_lifecycle_lock_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A lifecycle lock lost mid-sweep (issue #3927) stops the pass before the
    verdict write it guarded. Like a database failure it exits 3 with a message
    naming the server, and the traceback is logged at DEBUG only (issue #3942)."""

    sid = str(uuid.uuid4())

    async def _fake_run(*, server_id: ServerId | None) -> SweepSummary:
        raise LifecycleLockLostError(sid)

    monkeypatch.setattr(integrity_sweep_cli, "run", _fake_run)
    caplog.set_level(logging.DEBUG, logger=integrity_sweep_cli.__name__)

    assert integrity_sweep_cli.main([]) == 3

    err = capsys.readouterr().err
    assert "integrity sweep aborted: the lifecycle lock on server" in err
    assert f"server {sid} was lost" in err  # names the server it lost
    assert "the sweep stopped" in err
    assert "Backups already classified this run keep their verdict" in err
    assert "safe to re-run the sweep" in err
    assert "Traceback" not in err
    assert [r.levelno for r in caplog.records if r.exc_info] == [logging.DEBUG]


def test_main_lets_an_unexpected_error_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a database failure or a lost lock is an expected outcome (issue
    #3942); any other error is a bug and keeps its traceback."""

    async def _fake_run(*, server_id: ServerId | None) -> SweepSummary:
        raise RuntimeError("a bug")

    monkeypatch.setattr(integrity_sweep_cli, "run", _fake_run)

    with pytest.raises(RuntimeError, match="a bug"):
        integrity_sweep_cli.main([])


def test_main_rejects_an_invalid_server_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_run(*, server_id: ServerId | None) -> SweepSummary:
        raise AssertionError("run must not be called on a bad uuid")

    monkeypatch.setattr(integrity_sweep_cli, "run", _fake_run)
    assert integrity_sweep_cli.main(["--server", "not-a-uuid"]) == 2
