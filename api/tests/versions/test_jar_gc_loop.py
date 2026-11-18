"""Wiring test for the JAR-pool GC lifespan task."""

from __future__ import annotations

import logging
from typing import cast
from unittest.mock import AsyncMock

import pytest

from mc_server_dashboard_api.versions.adapters import jar_gc_loop
from mc_server_dashboard_api.versions.application.jar_gc import (
    JarGcResult,
    RunJarPoolGc,
)
from mc_server_dashboard_api.versions.domain.errors import StoreNotOwnedError


class _SpyGc:
    def __init__(self) -> None:
        self.runs = 0

    async def __call__(self) -> JarGcResult:
        self.runs += 1
        return JarGcResult(scanned=0, deleted=0, freed_bytes=0)


async def test_wires_gc_and_interval_to_periodic_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gc = _SpyGc()
    runner = AsyncMock()
    monkeypatch.setattr(jar_gc_loop, "run_periodic", runner)

    await jar_gc_loop.run_jar_gc_loop(cast(RunJarPoolGc, gc), tick_seconds=17)

    runner.assert_awaited_once()
    call = runner.await_args
    assert call is not None
    callback = call.args[0]
    assert call.kwargs["tick_seconds"] == 17
    assert callable(call.kwargs["on_error"])
    await callback()
    assert gc.runs == 1


async def test_a_refused_pass_is_logged_as_an_error_with_the_reason(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A JAR-pool GC pass on a store another database owns raises (issue #3870);
    # the loop's error handler is what reports it, on every pass.
    reason = "the store owner marker names installation x, not y"

    async def _refused() -> JarGcResult:
        raise StoreNotOwnedError(f"jar-pool GC refused: {reason}")

    runner = AsyncMock()
    monkeypatch.setattr(jar_gc_loop, "run_periodic", runner)
    await jar_gc_loop.run_jar_gc_loop(cast(RunJarPoolGc, _refused), tick_seconds=17)
    call = runner.await_args
    assert call is not None

    with caplog.at_level(logging.ERROR):
        try:
            await call.args[0]()
        except StoreNotOwnedError:
            call.kwargs["on_error"]()

    (record,) = caplog.records
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert reason in logging.Formatter().formatException(record.exc_info)
