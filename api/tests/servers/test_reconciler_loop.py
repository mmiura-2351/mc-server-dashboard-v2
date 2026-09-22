"""Tests for reconciler-specific startup work around the periodic runner."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

import pytest

from mc_server_dashboard_api.servers.adapters import reconciler_loop
from mc_server_dashboard_api.servers.application.reconciler import RunReconcilerTick
from mc_server_dashboard_api.servers.application.startup_reset import (
    ResetUnverifiableObservedStates,
)
from mc_server_dashboard_api.servers.application.warn_missing_ports import (
    WarnLegacyMissingPorts,
)

Callback = Callable[[], Awaitable[object]]
ErrorHandler = Callable[[], None]


class _SpyReconciler:
    def __init__(self) -> None:
        self.ticks = 0

    async def tick(self) -> None:
        self.ticks += 1


class _SpyReset:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls = 0
        self._fail_times = fail_times

    async def __call__(self) -> int:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("db down")
        return 0


class _SpyWarnPorts:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls = 0
        self._fail_times = fail_times

    async def __call__(self) -> int:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("db down")
        return 0


async def _run_callbacks(
    callback: Callback,
    *,
    on_error: ErrorHandler,
    calls: int,
) -> None:
    """Drive the callback; generic failure recovery belongs to the runner suite."""

    for _ in range(calls):
        try:
            await callback()
        except Exception:  # noqa: BLE001 - model the runner's callback boundary
            on_error()


async def test_reset_and_warning_run_before_the_first_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reconciler = _SpyReconciler()
    reset = _SpyReset()
    warning = _SpyWarnPorts()

    async def runner(
        callback: Callback,
        *,
        tick_seconds: float,
        on_error: ErrorHandler,
        run_immediately: bool,
    ) -> None:
        assert tick_seconds == 17
        assert run_immediately
        await _run_callbacks(callback, on_error=on_error, calls=1)

    monkeypatch.setattr(reconciler_loop, "run_periodic", runner)

    await reconciler_loop.run_reconciler_loop(
        cast(RunReconcilerTick, reconciler),
        reset=cast(ResetUnverifiableObservedStates, reset),
        warn_missing_ports=cast(WarnLegacyMissingPorts, warning),
        tick_seconds=17,
    )

    assert reset.calls == 1
    assert warning.calls == 1
    assert reconciler.ticks == 1


async def test_failed_reset_skips_ticks_until_it_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reconciler = _SpyReconciler()
    reset = _SpyReset(fail_times=2)
    warning = _SpyWarnPorts()

    async def runner(
        callback: Callback,
        *,
        tick_seconds: float,
        on_error: ErrorHandler,
        run_immediately: bool,
    ) -> None:
        assert tick_seconds == 17
        assert run_immediately
        await _run_callbacks(callback, on_error=on_error, calls=3)

    monkeypatch.setattr(reconciler_loop, "run_periodic", runner)

    await reconciler_loop.run_reconciler_loop(
        cast(RunReconcilerTick, reconciler),
        reset=cast(ResetUnverifiableObservedStates, reset),
        warn_missing_ports=cast(WarnLegacyMissingPorts, warning),
        tick_seconds=17,
    )

    assert reset.calls == 3
    assert warning.calls == 1
    assert reconciler.ticks == 1


async def test_successful_startup_work_is_not_repeated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reconciler = _SpyReconciler()
    reset = _SpyReset()
    warning = _SpyWarnPorts()

    async def runner(
        callback: Callback,
        *,
        tick_seconds: float,
        on_error: ErrorHandler,
        run_immediately: bool,
    ) -> None:
        assert tick_seconds == 17
        assert run_immediately
        await _run_callbacks(callback, on_error=on_error, calls=3)

    monkeypatch.setattr(reconciler_loop, "run_periodic", runner)

    await reconciler_loop.run_reconciler_loop(
        cast(RunReconcilerTick, reconciler),
        reset=cast(ResetUnverifiableObservedStates, reset),
        warn_missing_ports=cast(WarnLegacyMissingPorts, warning),
        tick_seconds=17,
    )

    assert reset.calls == 1
    assert warning.calls == 1
    assert reconciler.ticks == 3


async def test_warning_failure_does_not_gate_ticks_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reconciler = _SpyReconciler()
    reset = _SpyReset()
    warning = _SpyWarnPorts(fail_times=2)

    async def runner(
        callback: Callback,
        *,
        tick_seconds: float,
        on_error: ErrorHandler,
        run_immediately: bool,
    ) -> None:
        assert tick_seconds == 17
        assert run_immediately
        await _run_callbacks(callback, on_error=on_error, calls=3)

    monkeypatch.setattr(reconciler_loop, "run_periodic", runner)

    await reconciler_loop.run_reconciler_loop(
        cast(RunReconcilerTick, reconciler),
        reset=cast(ResetUnverifiableObservedStates, reset),
        warn_missing_ports=cast(WarnLegacyMissingPorts, warning),
        tick_seconds=17,
    )

    assert reset.calls == 1
    assert warning.calls == 3
    assert reconciler.ticks == 3
