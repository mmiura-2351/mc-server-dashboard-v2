"""Unit tests for commit-based per-worker resource accounting (#710).

The pure summing that feeds resource-aware placement: declared
``memory_limit_mb`` / ``cpu_millis`` totalled per assigned Worker, with unset
values contributing zero and unassigned servers ignored.
"""

from __future__ import annotations

import datetime as dt
import uuid

from mc_server_dashboard_api.servers.domain.committed_resources import (
    CommittedResources,
    committed_resources_by_worker,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ExecutionBackend,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)

_NOW = dt.datetime(2026, 6, 9, 12, 0, tzinfo=dt.timezone.utc)


def _server(
    *,
    worker_id: uuid.UUID | None,
    config: dict[str, object],
) -> Server:
    return Server(
        id=ServerId(uuid.uuid4()),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        execution_backend=ExecutionBackend.HOST_PROCESS,
        config=config,
        desired_state=DesiredState.RUNNING,
        observed_state=ObservedState.RUNNING,
        observed_at=None,
        assigned_worker_id=None if worker_id is None else WorkerId(worker_id),
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_no_servers_is_empty() -> None:
    assert committed_resources_by_worker([]) == {}


def test_unassigned_servers_are_ignored() -> None:
    server = _server(
        worker_id=None, config={"memory_limit_mb": 2048, "cpu_millis": 1000}
    )
    assert committed_resources_by_worker([server]) == {}


def test_sums_declared_resources_per_worker() -> None:
    worker_a = uuid.uuid4()
    worker_b = uuid.uuid4()
    servers = [
        _server(
            worker_id=worker_a, config={"memory_limit_mb": 2048, "cpu_millis": 1000}
        ),
        _server(
            worker_id=worker_a, config={"memory_limit_mb": 1024, "cpu_millis": 500}
        ),
        _server(
            worker_id=worker_b, config={"memory_limit_mb": 4096, "cpu_millis": 2000}
        ),
    ]

    result = committed_resources_by_worker(servers)

    assert result == {
        WorkerId(worker_a): CommittedResources(memory_mb=3072, cpu_millis=1500),
        WorkerId(worker_b): CommittedResources(memory_mb=4096, cpu_millis=2000),
    }


def test_unset_resources_contribute_zero() -> None:
    worker = uuid.uuid4()
    servers = [
        _server(worker_id=worker, config={"memory_limit_mb": 2048}),
        _server(worker_id=worker, config={"cpu_millis": 1000}),
        _server(worker_id=worker, config={}),
    ]

    result = committed_resources_by_worker(servers)

    assert result == {
        WorkerId(worker): CommittedResources(memory_mb=2048, cpu_millis=1000)
    }
