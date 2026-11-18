"""The app builds one storage adapter, and the sweep and requests share it (#3893).

A publish keeps the sweep off the snapshot it is still filling by registering it
on the adapter instance, in memory (STORAGE.md Section 4.3). That holds only while
the sweep and every publisher use the same instance, so the app must build exactly
one and hand that one to the sweep loop and to the request path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from starlette.requests import Request

from mc_server_dashboard_api import dependencies
from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.storage.adapters.fs import FsStorage


async def test_the_app_builds_one_storage_adapter_for_every_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[FsStorage] = []

    class _Recorded(FsStorage):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            built.append(self)

    swept: list[object] = []

    async def _capture(sweep: object, *, tick_seconds: float) -> None:
        swept.append(sweep)

    monkeypatch.setenv("MCD_API_STORAGE__BACKEND", "fs")
    monkeypatch.setenv("MCD_API_STORAGE__FS__ROOT", str(tmp_path))
    monkeypatch.setattr("mc_server_dashboard_api.app.FsStorage", _Recorded)
    monkeypatch.setattr("mc_server_dashboard_api.app.run_storage_sweep_loop", _capture)
    app = create_app()

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        request = Request({"type": "http", "app": app, "headers": []})
        served = dependencies.get_storage(request)

    assert len(built) == 1
    assert served is built[0]
    assert swept == [built[0].sweep]
