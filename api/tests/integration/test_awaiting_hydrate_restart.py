"""The hydrate a kept start owes survives an API restart (issue #3890).

A start whose hydrate the Worker refused keeps that Worker, and until the Worker
hydrates, nothing may publish its scratch. The periodic scheduler forgets its
schedule on a restart and snapshots every running server within its jitter, so
an in-memory mark would be gone exactly when it matters. These run the keep on
PostgreSQL, then read the server through process objects built afresh, as a
restarted API would.

Runs only when ``MCD_TEST_DATABASE_URL`` is set; skipped otherwise.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.dataplane.api.uploads import SnapshotUploads
from mc_server_dashboard_api.dependencies import (
    WorkerAssignment,
    get_assigned_worker_lookup,
)
from mc_server_dashboard_api.fleet.adapters.real_time_events import (
    InProcessRealTimeEvents,
)
from mc_server_dashboard_api.servers.adapters.file_store import StorageFileStoreAdapter
from mc_server_dashboard_api.servers.adapters.store_generation import (
    StorageGenerationReader,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.lifecycle import StartServer
from mc_server_dashboard_api.servers.application.snapshot_scheduler import (
    RunSnapshotCadenceTick,
)
from mc_server_dashboard_api.servers.domain.control_plane import CommandOutcome
from mc_server_dashboard_api.servers.domain.errors import CommandDispatchError
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ServerId,
    WorkerId,
)
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.contract_table import worker_status
from tests.servers.fakes import FakeClock, FakeControlPlane, FakeJarProvisioner

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 11, 18, 12, 0, tzinfo=dt.timezone.utc)
_WORKER = WorkerId(uuid.uuid4())


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    assert _DB_URL is not None
    await downgrade_base(_DB_URL)
    await upgrade_head(_DB_URL)
    eng = create_async_engine(_DB_URL)
    try:
        yield eng
    finally:
        await eng.dispose()
        await downgrade_base(_DB_URL)


async def _seed_stopped_server(engine: AsyncEngine) -> tuple[CommunityId, ServerId]:
    community_id = uuid.uuid4()
    server_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO community (id, name, created_at, updated_at) "
                "VALUES (:id, 'guild', :at, :at)"
            ),
            {"id": community_id, "at": _NOW},
        )
        await conn.execute(
            text(
                "INSERT INTO server (id, community_id, name, mc_edition, mc_version, "
                "server_type, config, slug, desired_state, "
                "observed_state, created_at, updated_at) VALUES "
                "(:id, :cid, 'srv', 'java', '1.21.1', 'vanilla', "
                "'{}', :slug, 'stopped', 'stopped', :at, :at)"
            ),
            {
                "id": server_id,
                "cid": community_id,
                "slug": f"srv-{str(server_id)[:8]}",
                "at": _NOW,
            },
        )
    return CommunityId(community_id), ServerId(server_id)


def _start(
    engine: AsyncEngine, storage: FsStorage, cp: FakeControlPlane, clock: FakeClock
) -> StartServer:
    return StartServer(
        uow=ServersUnitOfWork(
            create_session_factory(engine), InProcessRealTimeEvents()
        ),
        control_plane=cp,
        clock=clock,
        jar_provisioner=FakeJarProvisioner(),
        store_generation=StorageGenerationReader(
            storage=storage, uploads=SnapshotUploads(), upload_wait_seconds=60
        ),
        file_store=StorageFileStoreAdapter(storage=storage),
    )


# A handful of fsyncs on the fs backend (the EULA write), at 10 s each plus 30 s
# (issue #3651).
@pytest.mark.timeout(4 * 10 + 30)
async def test_a_kept_start_owes_its_hydrate_across_an_api_restart(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    community_id, server_id = await _seed_stopped_server(engine)
    storage = FsStorage(tmp_path)
    await StorageFileStoreAdapter(storage=storage).write_file(
        community_id=community_id,
        server_id=server_id,
        rel_path="eula.txt",
        content=b"eula=true\n",
    )
    refusing = FakeControlPlane(
        place_to=_WORKER,
        outcomes={
            "hydrate": CommandOutcome(
                status=worker_status("HydrateTrigger", "command_in_flight"),
                message="instancemanager: a lifecycle command is already in flight",
            )
        },
    )
    with pytest.raises(CommandDispatchError):
        await _start(engine, storage, refusing, FakeClock(_NOW))(
            community_id=community_id, server_id=server_id
        )

    # The restarted API: a fresh engine pool for the data plane's fence, a fresh
    # scheduler with an empty schedule, and a fresh control plane.
    request = cast(
        Request,
        SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(assignment_fence_engine=engine))
        ),
    )
    lookup = get_assigned_worker_lookup(request)
    assert await lookup(community_id.value, server_id.value) == WorkerAssignment(
        worker_id=str(_WORKER.value), awaiting_hydrate=True
    )
    cp = FakeControlPlane()
    clock = FakeClock(_NOW + dt.timedelta(minutes=5))
    scheduler = RunSnapshotCadenceTick(
        uow=ServersUnitOfWork(
            create_session_factory(engine), InProcessRealTimeEvents()
        ),
        control_plane=cp,
        clock=clock,
        default_interval_seconds=3600,
        min_interval_seconds=300,
    )
    await scheduler.tick()
    clock.set(_NOW + dt.timedelta(hours=2))
    await scheduler.tick()
    assert cp.dispatched == []

    # The reconciler's re-sent start hydrates on that Worker, and the mark clears.
    await _start(engine, storage, cp, clock).redispatch_start(
        community_id=community_id, server_id=server_id
    )
    assignment = await lookup(community_id.value, server_id.value)
    assert assignment is not None
    assert assignment.awaiting_hydrate is False
    await scheduler.tick()

    assert [kind for kind, _, _ in cp.dispatched] == ["hydrate", "start", "snapshot"]
