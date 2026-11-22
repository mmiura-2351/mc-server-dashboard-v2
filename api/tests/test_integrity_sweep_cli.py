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

import datetime as dt
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
from mc_server_dashboard_api.servers.application.backups import DanglingBackups
from mc_server_dashboard_api.servers.application.integrity_sweep import SweepSummary
from mc_server_dashboard_api.servers.domain.backup import (
    Backup,
    BackupHealth,
    BackupId,
    BackupSource,
)
from mc_server_dashboard_api.servers.domain.errors import (
    BackupStorageUnavailableError,
    LifecycleLockLostError,
    StoreNotOwnedError,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId
from mc_server_dashboard_api.storage.adapters import ownership
from mc_server_dashboard_api.storage.adapters.ownership import OWNER_MARKER
from tests.core.fakes import (
    RecordingEngines,
    black_hole_database,
    first_failure,
    silent_database,
)

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


# A stall guard: without the bound the call never returns.
@pytest.mark.timeout(10)
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


async def test_the_sweep_bounds_each_engines_wait_for_a_free_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both of the sweep's engines, its lock's included, wait the configured bound
    for a free connection, not SQLAlchemy's 30 s (issue #4051)."""

    monkeypatch.setenv("MCD_API_STORAGE__BACKEND", "fs")
    monkeypatch.setenv("MCD_API_STORAGE__FS__ROOT", str(tmp_path))
    monkeypatch.setenv(
        "MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@127.0.0.1:5/none"
    )
    monkeypatch.setenv("MCD_API_DATABASE__POOL_TIMEOUT_SECONDS", "0.2")
    engines = RecordingEngines()
    monkeypatch.setattr(integrity_sweep_cli, "create_engine", engines)

    with pytest.raises(DatabaseConnectError):
        await integrity_sweep_cli.run(load_settings(config_file=None), server_id=None)

    assert engines.pool_timeouts() == [0.2, 0.2]


# --- --delete-dangling (issue #4107) ---------------------------------------------

_DANGLING_AT = dt.datetime(2026, 10, 15, 12, 19, 36, tzinfo=dt.timezone.utc)


def _dangling_row(server_id: ServerId) -> Backup:
    return Backup(
        id=BackupId.new(),
        server_id=server_id,
        storage_ref=uuid.uuid4().hex,
        size_bytes=None,
        source=BackupSource.SCHEDULED,
        health=BackupHealth.HEALTHY,
        created_by=None,
        created_at=_DANGLING_AT,
    )


_NONE_FOUND = DanglingBackups(
    servers_scanned=0, dangling=(), skipped_store_error=(), skipped_busy=()
)


def _stub_delete_dangling(
    monkeypatch: pytest.MonkeyPatch,
    outcome: DanglingBackups | Exception,
    calls: list[tuple[ServerId | None, bool]] | None = None,
) -> None:
    async def _fake_run(
        _settings: Settings, *, server_id: ServerId | None
    ) -> SweepSummary:
        raise AssertionError("--delete-dangling must not run the sweep")

    async def _fake_delete_dangling(
        _settings: Settings, *, server_id: ServerId | None, apply: bool
    ) -> DanglingBackups:
        if calls is not None:
            calls.append((server_id, apply))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(integrity_sweep_cli, "run", _fake_run)
    monkeypatch.setattr(
        integrity_sweep_cli, "run_delete_dangling", _fake_delete_dangling
    )


def test_delete_dangling_is_a_dry_run_over_every_server_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[ServerId | None, bool]] = []
    _stub_delete_dangling(monkeypatch, _NONE_FOUND, calls)

    assert integrity_sweep_cli.main(["--delete-dangling"]) == 0

    assert calls == [(None, False)]


def test_delete_dangling_applies_only_with_apply_and_takes_a_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = uuid.uuid4()
    calls: list[tuple[ServerId | None, bool]] = []
    _stub_delete_dangling(monkeypatch, _NONE_FOUND, calls)

    argv = ["--delete-dangling", "--apply", "--server", str(sid)]
    assert integrity_sweep_cli.main(argv) == 0

    assert calls == [(ServerId(sid), True)]


def test_apply_without_delete_dangling_is_a_bad_argument(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Checked before the configuration is read, like any bad argument.
    monkeypatch.delenv("MCD_API_DATABASE__URL")
    _stub_delete_dangling(monkeypatch, AssertionError("must not run"))

    assert integrity_sweep_cli.main(["--apply"]) == 2

    assert capsys.readouterr().err == "--apply needs --delete-dangling\n"


def test_a_dry_run_lists_each_dangling_row_and_says_nothing_was_deleted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = ServerId.new()
    row = _dangling_row(sid)
    found = replace(_NONE_FOUND, servers_scanned=1, dangling=(row,))
    _stub_delete_dangling(monkeypatch, found)

    assert integrity_sweep_cli.main(["--delete-dangling"]) == 0

    out = capsys.readouterr().out
    assert "servers scanned: 1\n" in out
    assert "dangling backup rows (the store reports no archive): 1\n" in out
    assert (
        f"  backup {row.id.value} of server {sid.value}, created "
        f"2026-10-15T12:19:36+00:00, source scheduled\n"
    ) in out
    assert "dry run: nothing was deleted" in out


def test_an_applied_pass_says_how_many_rows_it_deleted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sid = ServerId.new()
    found = replace(
        _NONE_FOUND,
        servers_scanned=1,
        dangling=(_dangling_row(sid), _dangling_row(sid)),
    )
    _stub_delete_dangling(monkeypatch, found)

    assert integrity_sweep_cli.main(["--delete-dangling", "--apply"]) == 0

    out = capsys.readouterr().out
    assert "deleted: 2 (each audited as backup:delete_dangling)" in out
    assert "dry run" not in out


def test_a_row_the_store_could_not_answer_for_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The pass completes, but it could not judge every row: exit 1, the store
    # status of the command, with the row named so the operator re-runs.
    sid = ServerId.new()
    row = _dangling_row(sid)
    busy = _dangling_row(sid)
    found = replace(
        _NONE_FOUND,
        servers_scanned=1,
        skipped_store_error=(row,),
        skipped_busy=(busy,),
    )
    _stub_delete_dangling(monkeypatch, found)

    assert integrity_sweep_cli.main(["--delete-dangling", "--apply"]) == 1

    out = capsys.readouterr().out
    assert (
        "skipped (the store could not say whether the archive exists; re-run once "
        "it is healthy): 1\n"
        f"  backup {row.id.value} of server {sid.value}"
    ) in out
    assert (
        f"skipped (server busy; re-run to check them): 1\n  backup {busy.id.value}"
    ) in out


def test_a_busy_row_alone_does_not_fail_the_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = replace(_NONE_FOUND, skipped_busy=(_dangling_row(ServerId.new()),))
    _stub_delete_dangling(monkeypatch, found)

    assert integrity_sweep_cli.main(["--delete-dangling"]) == 0


@pytest.mark.parametrize(
    ("error", "status", "line"),
    [
        (
            StoreNotOwnedError("the marker names another"),
            2,
            "dangling backups: refusing to delete: the marker names another\n",
        ),
        (
            LifecycleLockLostError("srv-2"),
            3,
            "dangling backups aborted: the lifecycle lock on server srv-2 was lost, "
            "so the pass stopped. Rows already deleted this run stay deleted, and "
            "it is safe to re-run.\n",
        ),
        (
            InterfaceError("SELECT 1", None, Exception("connection is closed")),
            3,
            "dangling backups aborted: the database failed (InterfaceError: "
            "connection is closed), so the pass stopped. Rows already deleted this "
            "run stay deleted, and it is safe to re-run.\n",
        ),
    ],
    ids=["store-not-owned", "lock-lost", "database-failed"],
)
def test_a_dangling_pass_that_stops_says_so_in_one_line(
    error: Exception,
    status: int,
    line: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stub_delete_dangling(monkeypatch, error)

    assert integrity_sweep_cli.main(["--delete-dangling", "--apply"]) == status

    assert capsys.readouterr().err == line


def test_a_dangling_pass_lets_an_unexpected_error_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_delete_dangling(monkeypatch, RuntimeError("a bug"))

    with pytest.raises(RuntimeError, match="a bug"):
        integrity_sweep_cli.main(["--delete-dangling"])


@pytest.mark.parametrize(
    "marker",
    [None, b'{"installation_id": "00000000-0000-4000-8000-000000000002"}'],
    ids=["no-marker", "another-installation"],
)
async def test_apply_refuses_a_store_not_marked_for_this_database(
    marker: bytes | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A store that is not this database's answers NotFound for every row, so an
    # applied pass against it would delete them all. It checks the owner marker
    # first, before it reads a row.
    if marker is not None:
        (tmp_path / OWNER_MARKER).write_bytes(marker)
    # The real marker, in place of the suite's stand-in (tests/conftest.py).
    monkeypatch.setattr(
        "mc_server_dashboard_api.app.FsStoreOwnership", ownership.FsStoreOwnership
    )
    monkeypatch.setenv("MCD_API_STORAGE__BACKEND", "fs")
    monkeypatch.setenv("MCD_API_STORAGE__FS__ROOT", str(tmp_path))

    async def _installation(_factory: object) -> uuid.UUID:
        return uuid.UUID("00000000-0000-4000-8000-000000000001")

    monkeypatch.setattr(integrity_sweep_cli, "read_installation_id", _installation)

    with pytest.raises(StoreNotOwnedError, match=OWNER_MARKER):
        await integrity_sweep_cli.run_delete_dangling(
            load_settings(config_file=None), server_id=None, apply=True
        )


def test_an_outage_reading_the_marker_before_the_pass_stops_it_in_one_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # An applied pass cannot tell whose store it is when the marker read fails,
    # so it does not start: one line and the store status, not a traceback.
    class _MarkerReadFails(ownership.FsStoreOwnership):
        def _read_marker(self) -> bytes | None:
            raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(
        "mc_server_dashboard_api.app.FsStoreOwnership", _MarkerReadFails
    )
    monkeypatch.setenv("MCD_API_STORAGE__BACKEND", "fs")
    monkeypatch.setenv("MCD_API_STORAGE__FS__ROOT", str(tmp_path))

    async def _installation(_factory: object) -> uuid.UUID:
        return uuid.UUID("00000000-0000-4000-8000-000000000001")

    def _no_pass(**_fields: object) -> None:
        raise AssertionError("the pass must not start")

    monkeypatch.setattr(integrity_sweep_cli, "read_installation_id", _installation)
    monkeypatch.setattr(integrity_sweep_cli, "DeleteDanglingBackups", _no_pass)

    assert integrity_sweep_cli.main(["--delete-dangling", "--apply"]) == 1

    assert capsys.readouterr().err == (
        "dangling backups aborted: the store owner marker could not be read "
        "(OSError: [Errno 5] Input/output error), so the pass stopped. Rows "
        "already deleted this run stay deleted, and it is safe to re-run.\n"
    )
