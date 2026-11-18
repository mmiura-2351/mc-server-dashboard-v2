"""First-pass timing of every periodic loop that starts soon after boot (#3868).

Each loop runs its first pass after a random delay below ``min(interval, 10 min)``
and later passes every interval, so an API restarted more often than the
interval still runs them. These tests drive the real runner through each loop
entry point, with a recording fake ``asyncio.sleep`` and no real waits.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from types import SimpleNamespace
from typing import Any, cast

import pytest

from mc_server_dashboard_api.identity.adapters.prune_login_attempts_loop import (
    run_prune_login_attempts_loop,
)
from mc_server_dashboard_api.identity.application.prune_login_attempts import (
    PruneLoginAttempts,
)
from mc_server_dashboard_api.servers.adapters.game_session_prune_loop import (
    run_game_session_prune_loop,
)
from mc_server_dashboard_api.servers.adapters.plugin_cache_gc_loop import (
    run_plugin_cache_gc_loop,
)
from mc_server_dashboard_api.servers.adapters.resource_pack_gc_loop import (
    run_resource_pack_gc_loop,
)
from mc_server_dashboard_api.servers.adapters.snapshot_loop import run_snapshot_loop
from mc_server_dashboard_api.servers.application.game_sessions import PruneGameSessions
from mc_server_dashboard_api.servers.application.plugin_cache_gc import (
    RunPluginCacheGc,
)
from mc_server_dashboard_api.servers.application.resource_pack_gc import (
    RunResourcePackGc,
)
from mc_server_dashboard_api.servers.application.snapshot_scheduler import (
    RunSnapshotCadenceTick,
)
from mc_server_dashboard_api.storage.adapters.sweep_loop import run_storage_sweep_loop
from mc_server_dashboard_api.versions.adapters.jar_gc_loop import run_jar_gc_loop
from mc_server_dashboard_api.versions.application.jar_gc import RunJarPoolGc
from tests.core.fakes import RecordingSleep

_DAILY = 86400.0
_STARTUP_WINDOW = 600.0

Pass = Callable[[], Awaitable[None]]
StartLoop = Callable[[Pass, float], Coroutine[Any, Any, None]]


def _ticking(on_pass: Pass) -> SimpleNamespace:
    return SimpleNamespace(tick=on_pass)


_LOOPS = [
    pytest.param(
        lambda on_pass, tick: run_jar_gc_loop(
            cast(RunJarPoolGc, on_pass), tick_seconds=tick
        ),
        id="jar-pool-gc",
    ),
    pytest.param(
        lambda on_pass, tick: run_plugin_cache_gc_loop(
            cast(RunPluginCacheGc, on_pass), tick_seconds=tick
        ),
        id="plugin-cache-gc",
    ),
    pytest.param(
        lambda on_pass, tick: run_resource_pack_gc_loop(
            cast(RunResourcePackGc, on_pass), tick_seconds=tick
        ),
        id="resource-pack-gc",
    ),
    pytest.param(
        lambda on_pass, tick: run_storage_sweep_loop(on_pass, tick_seconds=tick),
        id="storage-sweep",
    ),
    pytest.param(
        lambda on_pass, tick: run_prune_login_attempts_loop(
            cast(PruneLoginAttempts, _ticking(on_pass)), tick_seconds=tick
        ),
        id="login-attempt-prune",
    ),
    pytest.param(
        lambda on_pass, tick: run_game_session_prune_loop(
            cast(PruneGameSessions, _ticking(on_pass)), tick_seconds=tick
        ),
        id="game-session-prune",
    ),
    pytest.param(
        lambda on_pass, tick: run_snapshot_loop(
            cast(RunSnapshotCadenceTick, _ticking(on_pass)), tick_seconds=tick
        ),
        id="snapshot-scheduler",
    ),
]


class _PassCounter:
    def __init__(self) -> None:
        self.passes = 0

    # A method, not ``__call__``: the storage sweep loop awaits only what
    # ``inspect.iscoroutinefunction`` recognises, and runs anything else in a thread.
    async def count(self) -> None:
        self.passes += 1


@pytest.mark.parametrize("start_loop", _LOOPS)
async def test_first_pass_runs_within_the_startup_window_then_every_interval(
    monkeypatch: pytest.MonkeyPatch, start_loop: StartLoop
) -> None:
    sleep = RecordingSleep(returns=2)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    counter = _PassCounter()
    task = asyncio.create_task(start_loop(counter.count, _DAILY))

    await sleep.parked.wait()

    first_delay, *later_delays = sleep.delays
    assert 0 <= first_delay < _STARTUP_WINDOW
    assert later_delays == [_DAILY, _DAILY]
    assert counter.passes == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("start_loop", _LOOPS)
async def test_cancelling_during_the_startup_delay_ends_the_loop_without_a_pass(
    monkeypatch: pytest.MonkeyPatch, start_loop: StartLoop
) -> None:
    sleep = RecordingSleep(returns=0)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    counter = _PassCounter()
    task = asyncio.create_task(start_loop(counter.count, _DAILY))
    await sleep.parked.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert counter.passes == 0
