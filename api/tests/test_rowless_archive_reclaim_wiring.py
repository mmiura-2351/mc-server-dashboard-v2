"""The app runs the row-less backup archive reclaim at startup and on the sweep
loop (issue #4106).

The reclaim reads the backup rows, which the adapter's sweep cannot, so the app
builds it as a servers use case. It runs once at startup, after the startup
sweep, and the storage sweep loop runs it after each pass. A startup reclaim
that fails is logged and the app still starts: the loop runs it again.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import Path
from typing import Any

import pytest

from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.servers.adapters.lifecycle_lock import PgLifecycleLock
from mc_server_dashboard_api.storage.adapters.fs import FsStorage


class _Recorder:
    """Stands in for the use case; records how the app built and ran it."""

    built: list[_Recorder]
    order: list[str]
    fail = False

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.built.append(self)

    async def __call__(self) -> None:
        self.order.append("reclaim")
        if self.fail:
            raise OSError("the database is down")


@pytest.fixture
def recorder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[type[_Recorder], dict[str, Any]]:
    monkeypatch.setenv("MCD_API_STORAGE__FS__ROOT", str(tmp_path))
    order: list[str] = []

    class _Recording(_Recorder):
        built: list[_Recorder] = []

    _Recording.order = order

    def _sweep(self: FsStorage) -> None:
        order.append("sweep")

    loop: dict[str, Any] = {}

    async def _capture(sweep: object, **kwargs: Any) -> None:
        loop.update(kwargs)

    monkeypatch.setattr(FsStorage, "sweep", _sweep)
    monkeypatch.setattr(
        "mc_server_dashboard_api.app.ReclaimRowlessBackupArchives", _Recording
    )
    monkeypatch.setattr("mc_server_dashboard_api.app.run_storage_sweep_loop", _capture)
    return _Recording, loop


async def test_reclaims_at_startup_after_the_sweep_and_on_the_sweep_loop(
    recorder: tuple[type[_Recorder], dict[str, Any]],
) -> None:
    recording, loop = recorder
    app = create_app()

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        ownership = app.state.store_ownership

    assert recording.order == ["sweep", "reclaim"]
    [built] = recording.built
    assert loop["reclaim_rowless_archives"] is built
    assert built.kwargs["ownership"] is ownership
    # The lock is what keeps a delete off a backup still being written.
    assert isinstance(built.kwargs["lifecycle_lock"], PgLifecycleLock)
    assert built.kwargs["grace"] == dt.timedelta(days=7)


async def test_a_failed_startup_reclaim_is_logged_and_the_app_starts(
    recorder: tuple[type[_Recorder], dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    recording, loop = recorder
    recording.fail = True
    app = create_app()

    with caplog.at_level(logging.ERROR):
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)

    assert "reclaim_rowless_archives" in loop
    assert any(
        "row-less backup archive reclaim at startup failed" in r.getMessage()
        and r.exc_info is not None
        for r in caplog.records
    )
