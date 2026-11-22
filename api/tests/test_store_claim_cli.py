"""The operator's claim of the store (STORAGE.md Section 4.3, issue #3886).

At startup the API leaves an unmarked store that already holds objects unclaimed
when no row of its database names a stored object. The command claims such a
store for this database: it writes the owner marker naming this installation,
whatever the store holds, and deletes nothing. It never replaces a marker,
whichever installation that marker names. It exits 0 when it claimed the store, 1
when it refused, and 2, with one line and no traceback, when the configuration,
the database or the store failed. It logs through the app's handler.

These run ``main`` over the config-selected store the API builds (object, over
the in-memory S3 stub, and fs), with the real marker in place of the app suite's
stand-in and the database's installation id fixed.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

import pytest

from mc_server_dashboard_api import store_claim_cli
from mc_server_dashboard_api.config import load_settings
from mc_server_dashboard_api.core.adapters.database import is_transient_database_error
from mc_server_dashboard_api.storage.adapters import ownership as ownership_module
from mc_server_dashboard_api.storage.adapters.ownership import OWNER_MARKER
from tests.core.fakes import (
    RecordingEngines,
    black_hole_database,
    first_failure,
    silent_database,
)
from tests.storage.fake_s3 import FakeS3Store, fake_s3_factory

# ``main`` installs the app's log handler (issue #3942); take it off afterwards.
pytestmark = pytest.mark.usefixtures("app_log_handler_restored")

_OURS = uuid.UUID("00000000-0000-4000-8000-000000000001")
_THEIRS = uuid.UUID("00000000-0000-4000-8000-000000000002")


def _marker(installation_id: uuid.UUID) -> bytes:
    return json.dumps(
        {"installation_id": str(installation_id), "created_at": "2026-01-01T00:00:00Z"}
    ).encode()


async def _ours(_session_factory: object) -> uuid.UUID:
    return _OURS


@dataclass
class _Store:
    """The configured store: its marker, and one object a GC would reclaim."""

    read: Callable[[], bytes | None]
    seed: Callable[[bytes], object]
    held: Callable[[], bool]


@pytest.fixture(params=["object", "fs"])
def store(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> _Store:
    monkeypatch.setattr(store_claim_cli, "read_installation_id", _ours)
    if request.param == "object":
        backing = FakeS3Store()
        monkeypatch.setenv("MCD_API_STORAGE__BACKEND", "object")
        monkeypatch.setenv("MCD_API_STORAGE__OBJECT__ENDPOINT", "http://s3.invalid")
        monkeypatch.setenv("MCD_API_STORAGE__OBJECT__BUCKET", "mcsd")
        monkeypatch.setenv("MCD_API_STORAGE__OBJECT__ACCESS_KEY", "ak")
        monkeypatch.setenv("MCD_API_STORAGE__OBJECT__SECRET_KEY", "sk")
        monkeypatch.setattr(
            "mc_server_dashboard_api.app.make_s3_client_factory",
            lambda **_kwargs: fake_s3_factory(backing),
        )
        monkeypatch.setattr(
            "mc_server_dashboard_api.app.ObjectStoreOwnership",
            ownership_module.ObjectStoreOwnership,
        )
        orphan = f"jars/{'a' * 64}.jar"
        backing.objects[orphan] = b"live"
        return _Store(
            read=lambda: backing.objects.get(OWNER_MARKER),
            seed=lambda body: backing.objects.__setitem__(OWNER_MARKER, body),
            held=lambda: orphan in backing.objects,
        )
    monkeypatch.setenv("MCD_API_STORAGE__BACKEND", "fs")
    monkeypatch.setenv("MCD_API_STORAGE__FS__ROOT", str(tmp_path))
    monkeypatch.setattr(
        "mc_server_dashboard_api.app.FsStoreOwnership",
        ownership_module.FsStoreOwnership,
    )
    marker = tmp_path / OWNER_MARKER
    jar = tmp_path / "jars" / f"{'a' * 64}.jar"
    jar.parent.mkdir()
    jar.write_bytes(b"live")
    return _Store(
        read=lambda: marker.read_bytes() if marker.exists() else None,
        seed=marker.write_bytes,
        held=jar.exists,
    )


def test_the_command_claims_an_unmarked_store_holding_objects(
    store: _Store, capsys: pytest.CaptureFixture[str]
) -> None:
    assert store_claim_cli.main([]) == 0

    body = store.read()
    assert body is not None
    assert json.loads(body)["installation_id"] == str(_OURS)
    assert store.held()
    assert "restart the API" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("owner", "says"),
    [(_OURS, "already this database's"), (_THEIRS, "another database's")],
    ids=["ours", "theirs"],
)
def test_the_command_refuses_a_store_that_has_a_marker(
    store: _Store, capsys: pytest.CaptureFixture[str], owner: uuid.UUID, says: str
) -> None:
    store.seed(_marker(owner))

    assert store_claim_cli.main([]) == 1

    assert store.read() == _marker(owner)
    assert store.held()
    err = capsys.readouterr().err
    assert says in err
    assert str(owner) in err


async def _database_down(_session_factory: object) -> uuid.UUID:
    raise OSError("connection refused")


def test_a_database_fault_exits_2_without_a_traceback(
    store: _Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(store_claim_cli, "read_installation_id", _database_down)

    assert store_claim_cli.main([]) == 2

    assert store.read() is None
    err = capsys.readouterr().err
    assert err == "store claim failed: OSError: connection refused\n"


def test_the_command_leaves_a_crashed_claims_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The command deletes nothing, not even debris a startup claim would remove.
    monkeypatch.setattr(store_claim_cli, "read_installation_id", _ours)
    monkeypatch.setenv("MCD_API_STORAGE__BACKEND", "fs")
    monkeypatch.setenv("MCD_API_STORAGE__FS__ROOT", str(tmp_path))
    monkeypatch.setattr(
        "mc_server_dashboard_api.app.FsStoreOwnership",
        ownership_module.FsStoreOwnership,
    )
    stale = tmp_path / f".{OWNER_MARKER}.{'a' * 32}.tmp"
    stale.write_bytes(b"{}")
    two_hours_ago = time.time() - 7200
    os.utime(stale, (two_hours_ago, two_hours_ago))

    assert store_claim_cli.main([]) == 0

    assert stale.exists()


def test_a_store_fault_exits_2_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The configured fs root is a file, so the marker can be neither read nor
    # written.
    root = tmp_path / "not-a-dir"
    root.write_bytes(b"")
    monkeypatch.setattr(store_claim_cli, "read_installation_id", _ours)
    monkeypatch.setenv("MCD_API_STORAGE__BACKEND", "fs")
    monkeypatch.setenv("MCD_API_STORAGE__FS__ROOT", str(root))
    monkeypatch.setattr(
        "mc_server_dashboard_api.app.FsStoreOwnership",
        ownership_module.FsStoreOwnership,
    )

    assert store_claim_cli.main([]) == 2

    err = capsys.readouterr().err
    assert err.startswith("store claim failed: NotADirectoryError: ")
    assert "Traceback" not in err


def test_a_configuration_fault_exits_2_in_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The database URL is a required key, so the settings do not load. The line
    # names the first failing key and how many failed, not pydantic's message.
    monkeypatch.delenv("MCD_API_DATABASE__URL")

    assert store_claim_cli.main([]) == 2

    err = capsys.readouterr().err
    assert err == (
        "store claim failed: invalid configuration: database: Field required "
        "(1 error)\n"
    )


def test_a_bad_argument_exits_2_before_the_configuration_is_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The command takes no arguments; argparse refuses one before the settings
    # load, so a bad configuration does not hide it.
    monkeypatch.delenv("MCD_API_DATABASE__URL")

    with pytest.raises(SystemExit) as exited:
        store_claim_cli.main(["--bogus"])

    assert exited.value.code == 2
    err = capsys.readouterr().err
    assert "unrecognized arguments: --bogus" in err
    assert "store claim failed" not in err


def test_the_command_logs_in_the_configured_format(
    store: _Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The command logs through the app's handler at the configured ``log.level``
    and ``log.format`` (issue #3942), so its lines come out escaped, as the API's
    do (issue #3938)."""

    monkeypatch.setenv("MCD_API_LOG__LEVEL", "debug")
    monkeypatch.setenv("MCD_API_LOG__FORMAT", "text")

    async def _ours_logged(_session_factory: object) -> uuid.UUID:
        logging.getLogger("mc_server_dashboard_api.core.adapters.installation").debug(
            "installation \x1b[31mred\x1b[0m\nINFO forged"
        )
        return _OURS

    monkeypatch.setattr(store_claim_cli, "read_installation_id", _ours_logged)

    assert store_claim_cli.main([]) == 0

    err = capsys.readouterr().err
    assert (
        "DEBUG mc_server_dashboard_api.core.adapters.installation "
        "installation \\x1b[31mred\\x1b[0m\\nINFO forged\n"
    ) in err
    assert "\x1b" not in err


@pytest.mark.parametrize(
    ("stalled", "setting"),
    [
        pytest.param(black_hole_database, "connect_timeout_seconds", id="connect"),
        pytest.param(silent_database, "statement_timeout_seconds", id="statement"),
    ],
)
async def test_the_command_holds_a_database_that_stopped_answering_to_its_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stalled: Callable[[], AbstractContextManager[str]],
    setting: str,
) -> None:
    # A database that hangs fails the claim at the configured bound (issue
    # #4042), as an outage, before the store is touched.
    monkeypatch.setenv("MCD_API_STORAGE__BACKEND", "fs")
    monkeypatch.setenv("MCD_API_STORAGE__FS__ROOT", str(tmp_path))
    monkeypatch.setenv(f"MCD_API_DATABASE__{setting.upper()}", "0.2")
    with stalled() as url:
        monkeypatch.setenv("MCD_API_DATABASE__URL", url)
        settings = load_settings(config_file=None)
        error, elapsed = await first_failure(lambda: store_claim_cli.run(settings))

    assert elapsed < 2.0
    assert is_transient_database_error(error) is True


async def test_the_command_bounds_its_wait_for_a_free_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The configured wait, not SQLAlchemy's 30 s (issue #4051).
    monkeypatch.setenv("MCD_API_STORAGE__BACKEND", "fs")
    monkeypatch.setenv("MCD_API_STORAGE__FS__ROOT", str(tmp_path))
    monkeypatch.setenv(
        "MCD_API_DATABASE__URL", "postgresql+asyncpg://u:p@127.0.0.1:5/none"
    )
    monkeypatch.setenv("MCD_API_DATABASE__POOL_TIMEOUT_SECONDS", "0.2")
    engines = RecordingEngines()
    monkeypatch.setattr(store_claim_cli, "create_engine", engines)

    with pytest.raises(OSError):
        await store_claim_cli.run(load_settings(config_file=None))

    assert engines.pool_timeouts() == [0.2]
