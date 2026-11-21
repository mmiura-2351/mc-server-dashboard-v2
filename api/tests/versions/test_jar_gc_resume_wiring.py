"""The JAR-pool GC's periodic pass and its manual trigger share one resume point.

A pass that ends on an unreachable store records the JAR it ended at, and the
next pass starts after it (issue #3934). The manual trigger must start there too,
so these drive the real ``create_app`` lifespan and build the trigger's use case
the way its route does.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from starlette.requests import Request

from mc_server_dashboard_api import dependencies
from mc_server_dashboard_api.app import create_app
from mc_server_dashboard_api.fleet.adapters.drain_store import (
    SqlAlchemyWorkerDrainStore,
)
from mc_server_dashboard_api.fleet.domain.value_objects import WorkerId
from mc_server_dashboard_api.gc_pass import ResumePoint
from mc_server_dashboard_api.versions.application.jar_gc import RunJarPoolGc


def _request(app: Any) -> Request:
    return Request({"type": "http", "app": app, "headers": []})


async def test_the_manual_trigger_shares_the_periodic_pass_s_resume_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The loop is wired only with the control plane on.
    monkeypatch.setenv("MCD_API_CONTROL__ENABLED", "true")
    monkeypatch.setenv("MCD_API_CONTROL__WORKER_CREDENTIAL", "shared-worker-secret")
    monkeypatch.setenv("MCD_API_SERVER__GRPC_PORT", "0")
    monkeypatch.setenv("MCD_API_SERVER__HOST", "127.0.0.1")

    async def _drained(self: SqlAlchemyWorkerDrainStore) -> frozenset[WorkerId]:
        return frozenset()

    monkeypatch.setattr(SqlAlchemyWorkerDrainStore, "drained", _drained)
    looped: list[RunJarPoolGc] = []

    async def _loop(gc: RunJarPoolGc, *, tick_seconds: float) -> None:
        looped.append(gc)

    monkeypatch.setattr("mc_server_dashboard_api.app.run_jar_gc_loop", _loop)
    app = create_app()

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        (periodic,) = looped
        manual = dependencies.get_jar_pool_gc(_request(app))

    assert manual.resume_point is periodic.resume_point


async def test_the_manual_trigger_builds_without_the_control_plane() -> None:
    # The manual trigger runs without the control plane (the suite's default),
    # so its resume point must not be wired only with the loop.
    app = create_app()

    async with app.router.lifespan_context(app):
        manual = dependencies.get_jar_pool_gc(_request(app))

    assert isinstance(manual.resume_point, ResumePoint)
