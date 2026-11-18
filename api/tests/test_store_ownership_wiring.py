"""Every destructive GC and sweep the app runs is gated on the owner marker (#3870).

These drive the real ``create_app`` lifespan on the object backend, over the
in-memory S3 stub, with the control plane on so every GC is wired: the startup
sweep, the periodic sweep, the JAR-pool GC (periodic and manual), the plugin-cache
GC and the resource-pack GC. The app suite's default stand-in for the marker
(``tests/conftest.py``) is replaced with the real one, and the database's
installation id is fixed.

With a marker naming another installation, none of them deletes anything. With
no marker, the app writes one naming its own installation and the startup sweep
runs. On the default fs backend, a marker file at the root naming another
installation blocks the startup and periodic sweeps the same way.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from starlette.requests import Request

from mc_server_dashboard_api import dependencies
from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.fleet.adapters.drain_store import (
    SqlAlchemyWorkerDrainStore,
)
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId
from mc_server_dashboard_api.storage.adapters import ownership as ownership_module
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.adapters.object_store import ObjectStorage
from mc_server_dashboard_api.storage.adapters.ownership import OWNER_MARKER
from mc_server_dashboard_api.versions.domain.errors import StoreNotOwnedError
from tests.storage.fake_s3 import FakeS3Store, fake_s3_factory

_OURS = uuid.UUID("00000000-0000-4000-8000-000000000001")
_THEIRS = uuid.UUID("00000000-0000-4000-8000-000000000002")
_LONG_AGO = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

_STAGED = "communities/c/servers/s/incoming/t1/level.dat"
_JAR = f"jars/{'a' * 64}.jar"
_PLUGIN = f"plugin-cache/{'b' * 64}"
_PACK = f"resource-packs/{uuid.uuid4()}/pack.zip"


def _marker(installation_id: uuid.UUID) -> bytes:
    return json.dumps(
        {"installation_id": str(installation_id), "created_at": "2026-01-01T00:00:00Z"}
    ).encode()


def _seed(store: FakeS3Store) -> None:
    """One object of every kind a GC or sweep would delete, all long orphaned."""

    for key in (_STAGED, _JAR, _PLUGIN, _PACK):
        store.objects[key] = b"orphan"
        store.mtimes[key] = _LONG_AGO
    store.multipart_uploads["upload-old"] = (f"jars/{'c' * 64}.jar", _LONG_AGO)


def _survivors(store: FakeS3Store) -> set[str]:
    kept = {k for k in (_STAGED, _JAR, _PLUGIN, _PACK) if k in store.objects}
    if "upload-old" in store.multipart_uploads:
        kept.add("upload-old")
    return kept


_EVERYTHING = {_STAGED, _JAR, _PLUGIN, _PACK, "upload-old"}


class _Captured:
    """The callables the lifespan hands its periodic loops."""

    def __init__(self) -> None:
        self.loops: dict[str, Any] = {}

    def loop(self, name: str) -> Callable[..., Any]:
        async def _record(target: Any, *, tick_seconds: float) -> None:
            self.loops[name] = target

        return _record


async def _ours(_session_factory: object) -> uuid.UUID:
    return _OURS


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeS3Store:
    backing = FakeS3Store()
    monkeypatch.setenv("MCD_API_STORAGE__BACKEND", "object")
    monkeypatch.setenv("MCD_API_STORAGE__OBJECT__ENDPOINT", "http://s3.invalid:8333")
    monkeypatch.setenv("MCD_API_STORAGE__OBJECT__BUCKET", "mcsd")
    monkeypatch.setenv("MCD_API_STORAGE__OBJECT__ACCESS_KEY", "ak")
    monkeypatch.setenv("MCD_API_STORAGE__OBJECT__SECRET_KEY", "sk")
    monkeypatch.setenv("MCD_API_CONTROL__ENABLED", "true")
    monkeypatch.setenv("MCD_API_CONTROL__WORKER_CREDENTIAL", "shared-worker-secret")
    monkeypatch.setenv("MCD_API_SERVER__GRPC_PORT", "0")
    monkeypatch.setenv("MCD_API_SERVER__HOST", "127.0.0.1")
    monkeypatch.setattr(
        "mc_server_dashboard_api.app.make_s3_client_factory",
        lambda **_kwargs: fake_s3_factory(backing),
    )
    # The real marker, in place of the app suite's stand-in.
    monkeypatch.setattr(
        "mc_server_dashboard_api.app.ObjectStoreOwnership",
        ownership_module.ObjectStoreOwnership,
    )

    monkeypatch.setattr("mc_server_dashboard_api.app.read_installation_id", _ours)

    async def _drained(self: SqlAlchemyWorkerDrainStore) -> frozenset[WorkerId]:
        return frozenset()

    monkeypatch.setattr(SqlAlchemyWorkerDrainStore, "drained", _drained)
    return backing


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> _Captured:
    seen = _Captured()
    for name in (
        "run_storage_sweep_loop",
        "run_jar_gc_loop",
        "run_plugin_cache_gc_loop",
        "run_resource_pack_gc_loop",
    ):
        monkeypatch.setattr(f"mc_server_dashboard_api.app.{name}", seen.loop(name))
    return seen


def _request(app: Any) -> Request:
    return Request({"type": "http", "app": app, "headers": []})


async def test_a_bucket_another_database_owns_blocks_every_gc_and_sweep(
    store: FakeS3Store, captured: _Captured, caplog: pytest.LogCaptureFixture
) -> None:
    store.objects[OWNER_MARKER] = _marker(_THEIRS)
    _seed(store)
    app = create_app()

    with caplog.at_level(logging.ERROR):
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)
            assert isinstance(app.state.storage, ObjectStorage)
            # The startup sweep ran and was refused.
            assert _survivors(store) == _EVERYTHING
            # So is each periodic pass.
            await captured.loops["run_storage_sweep_loop"]()
            with pytest.raises(StoreNotOwnedError):
                await captured.loops["run_jar_gc_loop"]()
            await captured.loops["run_plugin_cache_gc_loop"]()
            await captured.loops["run_resource_pack_gc_loop"]()
            # And the manual JAR-pool GC.
            with pytest.raises(StoreNotOwnedError):
                await dependencies.get_jar_pool_gc(_request(app))()

    assert _survivors(store) == _EVERYTHING
    assert store.objects[OWNER_MARKER] == _marker(_THEIRS)
    refused = [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.ERROR and str(_THEIRS) in rec.getMessage()
    ]
    for gate in ("storage sweep refused", "plugin-cache GC", "resource-pack GC"):
        assert any(gate in message for message in refused), gate


async def test_an_unmarked_bucket_is_claimed_and_swept(
    store: FakeS3Store, captured: _Captured
) -> None:
    _seed(store)
    app = create_app()

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        assert json.loads(store.objects[OWNER_MARKER])["installation_id"] == str(_OURS)
        # The startup sweep reclaimed the orphan staging and the old upload.
        assert _STAGED not in store.objects
        assert "upload-old" not in store.multipart_uploads


async def test_an_fs_root_another_database_owns_blocks_both_sweeps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured: _Captured,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The default backend: the marker is a file at the fs root (issue #3870).
    monkeypatch.setenv("MCD_API_STORAGE__BACKEND", "fs")
    monkeypatch.setenv("MCD_API_STORAGE__FS__ROOT", str(tmp_path))
    monkeypatch.setattr(
        "mc_server_dashboard_api.app.FsStoreOwnership",
        ownership_module.FsStoreOwnership,
    )
    monkeypatch.setattr("mc_server_dashboard_api.app.read_installation_id", _ours)
    (tmp_path / OWNER_MARKER).write_bytes(_marker(_THEIRS))
    staged = tmp_path / "communities" / "c" / "servers" / "s" / "incoming" / "t1"
    staged.mkdir(parents=True)
    (staged / "level.dat").write_bytes(b"in flight")
    app = create_app()

    with caplog.at_level(logging.ERROR):
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)
            assert isinstance(app.state.storage, FsStorage)
            assert staged.exists()
            # The fs sweep is blocking; its loop runs it off the event loop.
            await asyncio.to_thread(captured.loops["run_storage_sweep_loop"])

    assert staged.exists()
    assert (tmp_path / OWNER_MARKER).read_bytes() == _marker(_THEIRS)
    assert any(
        rec.levelno == logging.ERROR
        and "storage sweep refused" in rec.getMessage()
        and str(_THEIRS) in rec.getMessage()
        for rec in caplog.records
    )
