"""Wiring test for the resource-pack GC lifespan task (issue #3277)."""

from __future__ import annotations

import logging
from typing import cast
from unittest.mock import AsyncMock

import pytest

from mc_server_dashboard_api.servers.adapters import resource_pack_gc_loop
from mc_server_dashboard_api.servers.application.resource_pack_gc import (
    RunResourcePackGc,
)


class _SpyGc:
    def __init__(self) -> None:
        self.runs = 0

    async def __call__(self) -> None:
        self.runs += 1


async def test_wires_gc_and_interval_to_periodic_runner(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    gc = _SpyGc()
    runner = AsyncMock()
    monkeypatch.setattr(resource_pack_gc_loop, "run_periodic", runner)

    await resource_pack_gc_loop.run_resource_pack_gc_loop(
        cast(RunResourcePackGc, gc), tick_seconds=17
    )

    runner.assert_awaited_once()
    call = runner.await_args
    assert call is not None
    assert call.kwargs["tick_seconds"] == 17
    await call.args[0]()
    assert gc.runs == 1
    with caplog.at_level(logging.ERROR):
        try:
            raise RuntimeError("pass failed")
        except RuntimeError:
            call.kwargs["on_error"]()
    assert "resource-pack GC pass failed" in caplog.text
