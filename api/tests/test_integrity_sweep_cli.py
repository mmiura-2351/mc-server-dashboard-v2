"""The integrity-sweep admin command's ``main`` (issues #744, #3942).

The wiring + DB/storage round-trip lives behind ``run`` (DB-gated). These unit
tests mostly stub ``run`` so no database is touched, and pin what ``main`` does
around it: the argument and configuration handling (every server by default,
``--server`` scoping, an invalid uuid or configuration rejected), the summary it
prints, the exit status and one-line message of each failure it expects, and its
logging in the configured format. One runs the real ``run`` against a port where
no database listens.
"""

from __future__ import annotations

import errno
import logging
import socket
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy.exc import DBAPIError, InterfaceError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from mc_server_dashboard_api import integrity_sweep_cli
from mc_server_dashboard_api.config import Settings, load_settings
from mc_server_dashboard_api.core.adapters.database import (
    DatabaseConnectError,
    DatabaseTimeoutError,
    is_transient_database_error,
)
from mc_server_dashboard_api.servers.application.integrity_sweep import SweepSummary
from mc_server_dashboard_api.servers.domain.errors import (
    BackupStorageUnavailableError,
    LifecycleLockLostError,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId
from tests.core.fakes import black_hole_database, first_failure, silent_database

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

# ``main`` installs the app's log handler (issue #3942); take it off afterwards.
pytestmark = pytest.mark.usefixtures("app_log_handler_restored")


def test_main_prints_the_not_examined_snapshot_count(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The summary reports snapshots the backend never examined apart from the
    scanned ones (issue #2377), so "0 flagged" cannot read as a real verdict."""

    summary = replace(_EMPTY, servers_scanned=3, snapshots_not_examined=3)

    async def _fake_run(
        _settings: Settings, *, server_id: ServerId | None
    ) -> SweepSummary:
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

    async def _fake_run(
        _settings: Settings, *, server_id: ServerId | None
    ) -> SweepSummary:
        return summary

    monkeypatch.setattr(integrity_sweep_cli, "run", _fake_run)
    assert integrity_sweep_cli.main([]) == 0

    out = capsys.readouterr().out
    assert "servers scanned: 2" in out
    assert "backups skipped (server busy; re-run the sweep to check them): 1" in out


def test_main_with_no_args_sweeps_every_server(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[ServerId | None] = []

    async def _fake_run(
        _settings: Settings, *, server_id: ServerId | None
    ) -> SweepSummary:
        seen.append(server_id)
        return _EMPTY

    monkeypatch.setattr(integrity_sweep_cli, "run", _fake_run)
    assert integrity_sweep_cli.main([]) == 0
    assert seen == [None]


def test_main_scopes_to_a_single_server(monkeypatch: pytest.MonkeyPatch) -> None:
    sid = uuid.uuid4()
    seen: list[ServerId | None] = []

    async def _fake_run(
        _settings: Settings, *, server_id: ServerId | None
    ) -> SweepSummary:
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

    async def _fake_run(
        _settings: Settings, *, server_id: ServerId | None
    ) -> SweepSummary:
        raise BackupStorageUnavailableError("abc123ref")

    monkeypatch.setattr(integrity_sweep_cli, "run", _fake_run)

    assert integrity_sweep_cli.main([]) == 1

    err = capsys.readouterr().err
    assert "integrity sweep aborted" in err
    assert "abc123ref" in err  # names which backup the pass died on
    assert "Traceback" not in err


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        # A connection the database closed between two statements.
        (
            InterfaceError("SELECT 1", None, Exception("connection is closed")),
            "InterfaceError: connection is closed",
        ),
        # A statement the database cut off in flight: the asyncpg dialect has no
        # narrower class for that error, so it raises the bare DBAPIError.
        (
            DBAPIError(
                "SELECT 1",
                None,
                Exception(
                    "connection was closed in the middle of operation: "
                    "terminating connection due to administrator command"
                ),
            ),
            "DBAPIError: connection was closed in the middle of operation: "
            "terminating connection due to administrator command",
        ),
        # A new connection the database refused (the engine types it).
        (
            DatabaseConnectError("ConnectionRefusedError: [Errno 111] refused"),
            "DatabaseConnectError: ConnectionRefusedError: [Errno 111] refused",
        ),
        # A pool that could not hand out a connection: an outage, and no
        # DBAPIError.
        (
            PoolTimeoutError("QueuePool limit reached"),
            "TimeoutError: QueuePool limit reached",
        ),
        # A statement that outlasted the engine's bound (issue #4042).
        (
            DatabaseTimeoutError("the database did not answer"),
            "DatabaseTimeoutError: the database did not answer",
        ),
    ],
    ids=[
        "connection-closed",
        "terminated-in-flight",
        "connect-refused",
        "pool",
        "statement-timeout",
    ],
)
def test_main_reports_a_database_failure_cleanly(
    error: Exception,
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A database failure mid-sweep stops the pass (issue #3942). It is an
    operator-facing outcome like a store outage, so it exits with its own status
    and a message saying the re-run is safe, and no traceback at the default log
    level."""

    async def _fake_run(
        _settings: Settings, *, server_id: ServerId | None
    ) -> SweepSummary:
        raise error

    monkeypatch.setattr(integrity_sweep_cli, "run", _fake_run)

    assert integrity_sweep_cli.main([]) == 3

    err = capsys.readouterr().err
    assert "integrity sweep aborted: the database failed" in err
    assert f"({reason})" in err  # says why
    assert "the sweep stopped" in err
    assert "Backups already classified this run keep their verdict" in err
    assert "safe to re-run the sweep" in err
    assert "Traceback" not in err


def test_main_reports_a_lost_lifecycle_lock_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A lifecycle lock lost mid-sweep (issue #3927) stops the pass before the
    verdict write it guarded. Like a database failure it exits 3 with a message
    naming the server, and no traceback at the default log level (issue #3942)."""

    sid = str(uuid.uuid4())

    async def _fake_run(
        _settings: Settings, *, server_id: ServerId | None
    ) -> SweepSummary:
        raise LifecycleLockLostError(sid)

    monkeypatch.setattr(integrity_sweep_cli, "run", _fake_run)

    assert integrity_sweep_cli.main([]) == 3

    err = capsys.readouterr().err
    assert "integrity sweep aborted: the lifecycle lock on server" in err
    assert f"server {sid} was lost" in err  # names the server it lost
    assert "the sweep stopped" in err
    assert "Backups already classified this run keep their verdict" in err
    assert "safe to re-run the sweep" in err
    assert "Traceback" not in err


@pytest.mark.parametrize(
    ("error", "logged"),
    [
        (LifecycleLockLostError(str(uuid.uuid4())), "the lifecycle lock was lost"),
        (
            InterfaceError("SELECT 1", None, Exception("connection is closed")),
            "the database failed",
        ),
    ],
    ids=["lock-lost", "database-failed"],
)
def test_main_logs_in_the_configured_format(
    error: Exception,
    logged: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The command logs through the app's handler at the configured ``log.level``
    and ``log.format`` (issue #3942): the sweep's lines come out escaped, as the
    API's do (issue #3938), and ``log.level=debug`` shows the traceback of the
    failure that stopped the pass."""

    monkeypatch.setenv("MCD_API_LOG__LEVEL", "debug")
    monkeypatch.setenv("MCD_API_LOG__FORMAT", "text")

    async def _fake_run(
        _settings: Settings, *, server_id: ServerId | None
    ) -> SweepSummary:
        logging.getLogger(
            "mc_server_dashboard_api.servers.application.integrity_sweep"
        ).info("backup \x1b[31mred\x1b[0m\nINFO forged")
        raise error

    monkeypatch.setattr(integrity_sweep_cli, "run", _fake_run)

    assert integrity_sweep_cli.main([]) == 3

    err = capsys.readouterr().err
    assert (
        "INFO mc_server_dashboard_api.servers.application.integrity_sweep "
        "backup \\x1b[31mred\\x1b[0m\\nINFO forged\n"
    ) in err
    assert "\x1b" not in err
    assert (
        f"DEBUG mc_server_dashboard_api.integrity_sweep_cli integrity sweep: "
        f"{logged}\nTraceback (most recent call last):\n"
    ) in err


def test_main_lets_an_unexpected_error_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a database failure or a lost lock is an expected outcome (issue
    #3942); any other error is a bug and keeps its traceback."""

    async def _fake_run(
        _settings: Settings, *, server_id: ServerId | None
    ) -> SweepSummary:
        raise RuntimeError("a bug")

    monkeypatch.setattr(integrity_sweep_cli, "run", _fake_run)

    with pytest.raises(RuntimeError, match="a bug"):
        integrity_sweep_cli.main([])


def test_main_lets_a_store_os_error_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raw ``OSError`` is not taken for a database outage (issue #3942): a store
    read raises one for a full disk or a permission fault, which the fs adapter
    keeps raw (STORAGE.md Section 7.3), and the database's own arrives typed."""

    async def _fake_run(
        _settings: Settings, *, server_id: ServerId | None
    ) -> SweepSummary:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(integrity_sweep_cli, "run", _fake_run)

    with pytest.raises(OSError, match="No space left on device"):
        integrity_sweep_cli.main([])


def test_main_lets_a_timeout_that_is_not_the_databases_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store's bare ``TimeoutError`` is not taken for a database outage either
    (issue #4042): the engine's own arrives typed."""

    async def _fake_run(
        _settings: Settings, *, server_id: ServerId | None
    ) -> SweepSummary:
        raise TimeoutError("the store did not answer")

    monkeypatch.setattr(integrity_sweep_cli, "run", _fake_run)

    with pytest.raises(TimeoutError, match="the store did not answer"):
        integrity_sweep_cli.main([])


def test_main_reports_a_database_that_stopped_answering_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A database that stops answering stops the sweep at the statement bound with
    exit 3 and one line, as a refused one does (issue #4042)."""

    monkeypatch.setenv("MCD_API_STORAGE__BACKEND", "fs")
    monkeypatch.setenv("MCD_API_STORAGE__FS__ROOT", str(tmp_path))
    monkeypatch.setenv("MCD_API_DATABASE__STATEMENT_TIMEOUT_SECONDS", "0.2")
    with silent_database() as url:
        monkeypatch.setenv("MCD_API_DATABASE__URL", url)
        assert integrity_sweep_cli.main([]) == 3

    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert err.startswith(
        "integrity sweep aborted: the database failed (DatabaseTimeoutError: "
    )


def test_main_reports_a_refused_database_connection_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A database that refuses connections from the start stops the sweep at its
    first query with exit 3 and one line (issue #3942). asyncpg raises the refused
    connect as a raw ``OSError``; the command's engines type it."""

    with socket.socket() as probe:  # a free port, then nothing listens on it
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv(
        "MCD_API_DATABASE__URL", f"postgresql+asyncpg://u:p@127.0.0.1:{port}/app"
    )
    monkeypatch.setenv("MCD_API_STORAGE__BACKEND", "fs")
    monkeypatch.setenv("MCD_API_STORAGE__FS__ROOT", str(tmp_path))

    assert integrity_sweep_cli.main([]) == 3

    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert err.startswith(
        "integrity sweep aborted: the database failed (DatabaseConnectError: "
        "ConnectionRefusedError: "
    )
    assert err.endswith("it is safe to re-run the sweep.\n")


def test_main_rejects_an_invalid_server_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_run(
        _settings: Settings, *, server_id: ServerId | None
    ) -> SweepSummary:
        raise AssertionError("run must not be called on a bad uuid")

    monkeypatch.setattr(integrity_sweep_cli, "run", _fake_run)
    assert integrity_sweep_cli.main(["--server", "not-a-uuid"]) == 2


def test_main_rejects_a_bad_argument_before_reading_the_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bad argument is told apart from a bad configuration: it is checked
    first, so it exits 2 with its own line whatever the configuration holds."""

    monkeypatch.delenv("MCD_API_DATABASE__URL")

    assert integrity_sweep_cli.main(["--server", "not-a-uuid"]) == 2

    assert capsys.readouterr().err == "invalid --server uuid: 'not-a-uuid'\n"


@pytest.mark.parametrize(
    ("env", "line"),
    [
        (
            {"MCD_API_DATABASE__URL": " ", "MCD_API_LOG__LEVEL": "loud"},
            "log.level: Input should be 'debug', 'info', 'warning' or 'error' "
            "(first of 2 errors)",
        ),
        (
            {"MCD_API_CONFIG_FILE": "/nonexistent/mcsd.toml"},
            "FileNotFoundError: [Errno 2] No such file or directory: "
            "'/nonexistent/mcsd.toml'",
        ),
    ],
    ids=["invalid-settings", "missing-config-file"],
)
def test_main_reports_a_configuration_fault_in_one_line(
    env: dict[str, str],
    line: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A configuration the settings refuse, or a config file that cannot be read,
    exits 2 like a bad argument, with one line naming the first failing key and
    how many failed rather than pydantic's message or a traceback (issue #3942)."""

    for name, value in env.items():
        monkeypatch.setenv(name, value)

    async def _fake_run(
        _settings: Settings, *, server_id: ServerId | None
    ) -> SweepSummary:
        raise AssertionError("run must not be called on a bad configuration")

    monkeypatch.setattr(integrity_sweep_cli, "run", _fake_run)

    assert integrity_sweep_cli.main([]) == 2

    assert (
        capsys.readouterr().err == f"integrity sweep: invalid configuration: {line}\n"
    )


@pytest.mark.parametrize(
    ("stalled", "setting"),
    [
        pytest.param(black_hole_database, "connect_timeout_seconds", id="connect"),
        pytest.param(silent_database, "statement_timeout_seconds", id="statement"),
    ],
)
async def test_the_sweep_holds_a_database_that_stopped_answering_to_its_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stalled: Callable[[], AbstractContextManager[str]],
    setting: str,
) -> None:
    """A database that hangs fails the sweep at the configured bound (issue #4042)
    rather than leaving it waiting, as an outage."""

    monkeypatch.setenv("MCD_API_STORAGE__BACKEND", "fs")
    monkeypatch.setenv("MCD_API_STORAGE__FS__ROOT", str(tmp_path))
    monkeypatch.setenv(f"MCD_API_DATABASE__{setting.upper()}", "0.2")
    with stalled() as url:
        monkeypatch.setenv("MCD_API_DATABASE__URL", url)
        settings = load_settings(config_file=None)
        error, elapsed = await first_failure(
            lambda: integrity_sweep_cli.run(settings, server_id=None)
        )

    assert elapsed < 2.0
    assert is_transient_database_error(error) is True
