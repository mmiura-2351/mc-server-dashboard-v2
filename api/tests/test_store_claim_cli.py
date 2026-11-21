"""The operator's claim of the store (STORAGE.md Section 4.3, issue #3886).

At startup the API leaves an unmarked store that already holds objects unclaimed
when no row of its database names a stored object. The command claims such a
store for this database: it writes the owner marker naming this installation,
whatever the store holds, and deletes nothing. It never replaces a marker,
whichever installation that marker names. It exits 0 when it claimed the store, 1
when it refused, and 2, with one line and no traceback, when the database or the
store failed.

These run ``main`` over the config-selected store the API builds (object, over
the in-memory S3 stub, and fs), with the real marker in place of the app suite's
stand-in and the database's installation id fixed.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from mc_server_dashboard_api import store_claim_cli
from mc_server_dashboard_api.storage.adapters import ownership as ownership_module
from mc_server_dashboard_api.storage.adapters.ownership import OWNER_MARKER
from tests.storage.fake_s3 import FakeS3Store, fake_s3_factory

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
