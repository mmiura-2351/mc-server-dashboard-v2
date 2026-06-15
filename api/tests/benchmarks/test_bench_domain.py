"""Benchmark: pure domain logic (issue #1122).

Measures ``Server.is_at_rest()`` — a predicate evaluated on every
config-edit and delete gate.

To add a new domain-logic benchmark, add a ``test_bench_<function>`` that
calls ``benchmark(callable, *args)``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ExecutionBackend,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
)


def _make_server(
    desired: DesiredState = DesiredState.STOPPED,
    observed: ObservedState = ObservedState.STOPPED,
) -> Server:
    now = dt.datetime.now(dt.timezone.utc)
    return Server(
        id=ServerId(uuid.uuid4()),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("bench-server"),
        mc_edition="java",
        mc_version="1.21.5",
        server_type=ServerType.VANILLA,
        execution_backend=ExecutionBackend.CONTAINER,
        config={},
        desired_state=desired,
        observed_state=observed,
        observed_at=now,
        assigned_worker_id=None,
        created_at=now,
        updated_at=now,
    )


def test_bench_is_at_rest(benchmark: Any) -> None:
    """Server.is_at_rest() evaluation."""
    server = _make_server()
    benchmark(server.is_at_rest)
