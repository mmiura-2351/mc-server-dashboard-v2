"""Integration tests for the lifecycle repository writes + the state sink (PostgreSQL).

Runs only when ``MCD_TEST_DATABASE_URL`` is set (the CI Postgres service); skipped
otherwise (TESTING.md Section 5). Exercises against the real 0001-0005 schema the
DB-backed paths the control-plane lifecycle relies on: persisting desired state +
assignment, caching observed state, marking a worker's servers unknown on
disconnect, and the running-server tally that rebuilds a reconnected worker's
load (epic #7 obligation), driven through the :class:`ServersServerStateSink`.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from mc_server_dashboard_api.community.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as CommunityUnitOfWork,
)
from mc_server_dashboard_api.community.domain.entities import Community
from mc_server_dashboard_api.community.domain.value_objects import (
    CommunityId as CommunityCommunityId,
)
from mc_server_dashboard_api.community.domain.value_objects import CommunityName
from mc_server_dashboard_api.core.adapters.database import create_session_factory
from mc_server_dashboard_api.servers.adapters.repositories import (
    SqlAlchemyServerRepository,
)
from mc_server_dashboard_api.servers.adapters.server_state_sink import (
    ServersServerStateSink,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.manage_server import CreateServer
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    WorkerId,
)
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.fakes import FakeClock, FakeVersionValidator

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


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


async def _seed_community(engine: AsyncEngine) -> uuid.UUID:
    community = Community(
        id=CommunityCommunityId(uuid.uuid4()),
        name=CommunityName("guild"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    factory = create_session_factory(engine)
    async with CommunityUnitOfWork(factory) as uow:
        await uow.communities.add(community)
        await uow.commit()
    return community.id.value


async def _create_server(
    engine: AsyncEngine, community_id: uuid.UUID, name: str
) -> ServerId:
    factory = create_session_factory(engine)
    create = CreateServer(
        uow=ServersUnitOfWork(factory),
        clock=FakeClock(_NOW),
        version_validator=FakeVersionValidator(),
    )
    server = await create(
        community_id=CommunityId(community_id),
        name=name,
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="host_process",
        config={},
    )
    return server.id


async def test_update_lifecycle_persists_desired_and_assignment(
    engine: AsyncEngine,
) -> None:
    community_id = await _seed_community(engine)
    server_id = await _create_server(engine, community_id, "survival")
    worker = uuid.uuid4()
    factory = create_session_factory(engine)

    async with ServersUnitOfWork(factory) as uow:
        server = await uow.servers.get_by_id(server_id)
        assert server is not None
        server.desired_state = DesiredState.RUNNING
        server.assigned_worker_id = WorkerId(worker)
        server.updated_at = _NOW
        applied = await uow.servers.update_lifecycle(
            server, expected_from=DesiredState.STOPPED, require_unassigned=True
        )
        assert applied is True
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(server_id)
    assert loaded is not None
    assert loaded.desired_state is DesiredState.RUNNING
    assert loaded.assigned_worker_id == WorkerId(worker)


async def test_record_observed_state_unassign_clears_assignment(
    engine: AsyncEngine,
) -> None:
    # A confirmed stop records observed=stopped and clears the assignment in one
    # write, so a later start can re-place under require_unassigned (issue #206).
    community_id = await _seed_community(engine)
    server_id = await _create_server(engine, community_id, "survival")
    worker = uuid.uuid4()
    factory = create_session_factory(engine)

    async with ServersUnitOfWork(factory) as uow:
        server = await uow.servers.get_by_id(server_id)
        assert server is not None
        server.desired_state = DesiredState.RUNNING
        server.assigned_worker_id = WorkerId(worker)
        server.updated_at = _NOW
        await uow.servers.update_lifecycle(
            server, expected_from=DesiredState.STOPPED, require_unassigned=True
        )
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        await uow.servers.record_observed_state(
            server_id,
            observed_state=ObservedState.STOPPED,
            observed_at=_NOW,
            unassign=True,
        )
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(server_id)
    assert loaded is not None
    assert loaded.observed_state is ObservedState.STOPPED
    assert loaded.assigned_worker_id is None


async def test_update_lifecycle_compare_and_set_rejects_lost_race(
    engine: AsyncEngine,
) -> None:
    # Two sequential conflicting transitions: the first start CAS (stopped ->
    # running, unassigned) applies; a second start CAS with the same stale
    # expectation matches no row and reports the lost race, leaving the row as the
    # first transition committed it.
    community_id = await _seed_community(engine)
    server_id = await _create_server(engine, community_id, "survival")
    first_worker = uuid.uuid4()
    second_worker = uuid.uuid4()
    factory = create_session_factory(engine)

    async with ServersUnitOfWork(factory) as uow:
        server = await uow.servers.get_by_id(server_id)
        assert server is not None
        server.desired_state = DesiredState.RUNNING
        server.assigned_worker_id = WorkerId(first_worker)
        server.updated_at = _NOW
        applied = await uow.servers.update_lifecycle(
            server, expected_from=DesiredState.STOPPED, require_unassigned=True
        )
        assert applied is True
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        stale = await uow.servers.get_by_id(server_id)
        assert stale is not None
        # The losing transition still believes the row is stopped/unassigned.
        stale.desired_state = DesiredState.RUNNING
        stale.assigned_worker_id = WorkerId(second_worker)
        stale.updated_at = _NOW
        applied = await uow.servers.update_lifecycle(
            stale, expected_from=DesiredState.STOPPED, require_unassigned=True
        )
        assert applied is False
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(server_id)
    assert loaded is not None
    assert loaded.assigned_worker_id == WorkerId(first_worker)


async def test_sink_records_observed_state_from_assigned_worker(
    engine: AsyncEngine,
) -> None:
    community_id = await _seed_community(engine)
    server_id = await _create_server(engine, community_id, "survival")
    worker = uuid.uuid4()
    factory = create_session_factory(engine)

    async with ServersUnitOfWork(factory) as uow:
        server = await uow.servers.get_by_id(server_id)
        assert server is not None
        server.desired_state = DesiredState.RUNNING
        server.assigned_worker_id = WorkerId(worker)
        server.updated_at = _NOW
        await uow.servers.update_lifecycle(
            server, expected_from=DesiredState.STOPPED, require_unassigned=True
        )
        await uow.commit()

    sink = ServersServerStateSink(factory, clock=FakeClock(_NOW))
    await sink.record_observed_state(
        server_id=str(server_id.value), worker_id=str(worker), state="running"
    )

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(server_id)
    assert loaded is not None
    assert loaded.observed_state is ObservedState.RUNNING
    assert loaded.observed_at == _NOW


async def test_sink_drops_status_from_non_owning_worker(engine: AsyncEngine) -> None:
    community_id = await _seed_community(engine)
    server_id = await _create_server(engine, community_id, "survival")
    owner = uuid.uuid4()
    intruder = uuid.uuid4()
    factory = create_session_factory(engine)

    async with ServersUnitOfWork(factory) as uow:
        server = await uow.servers.get_by_id(server_id)
        assert server is not None
        server.desired_state = DesiredState.RUNNING
        server.assigned_worker_id = WorkerId(owner)
        server.updated_at = _NOW
        await uow.servers.update_lifecycle(
            server, expected_from=DesiredState.STOPPED, require_unassigned=True
        )
        await uow.commit()

    sink = ServersServerStateSink(factory, clock=FakeClock(_NOW))
    # A report from a worker that does not own the server is dropped, not applied.
    await sink.record_observed_state(
        server_id=str(server_id.value), worker_id=str(intruder), state="crashed"
    )

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(server_id)
    assert loaded is not None
    # Observed state is unchanged from its created default (the write was dropped).
    assert loaded.observed_state is ObservedState.STOPPED


async def test_sink_marks_worker_servers_unknown(engine: AsyncEngine) -> None:
    community_id = await _seed_community(engine)
    server_id = await _create_server(engine, community_id, "survival")
    worker = uuid.uuid4()
    factory = create_session_factory(engine)

    async with ServersUnitOfWork(factory) as uow:
        server = await uow.servers.get_by_id(server_id)
        assert server is not None
        server.assigned_worker_id = WorkerId(worker)
        server.desired_state = DesiredState.RUNNING
        await uow.servers.update_lifecycle(
            server, expected_from=DesiredState.STOPPED, require_unassigned=True
        )
        await uow.commit()

    sink = ServersServerStateSink(factory, clock=FakeClock(_NOW))
    await sink.mark_worker_servers_unknown(worker_id=str(worker))

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(server_id)
    assert loaded is not None
    assert loaded.observed_state is ObservedState.UNKNOWN
    # Worker disconnect keeps the assignment (stickiness invariant, issue #206):
    # only a confirmed stop unassigns; the row stays assigned to its Worker.
    assert loaded.assigned_worker_id == WorkerId(worker)


async def test_sink_counts_running_assignments(engine: AsyncEngine) -> None:
    community_id = await _seed_community(engine)
    worker = uuid.uuid4()
    factory = create_session_factory(engine)

    running = await _create_server(engine, community_id, "running")
    stopped = await _create_server(engine, community_id, "stopped")
    async with ServersUnitOfWork(factory) as uow:
        for sid, desired in (
            (running, DesiredState.RUNNING),
            (stopped, DesiredState.STOPPED),
        ):
            server = await uow.servers.get_by_id(sid)
            assert server is not None
            server.assigned_worker_id = WorkerId(worker)
            server.desired_state = desired
            await uow.servers.update_lifecycle(
                server, expected_from=DesiredState.STOPPED, require_unassigned=True
            )
        await uow.commit()

    sink = ServersServerStateSink(factory, clock=FakeClock(_NOW))
    count = await sink.count_running_assignments(worker_id=str(worker))
    assert count == 1


async def test_repository_count_running_for_worker(engine: AsyncEngine) -> None:
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    server_id = await _create_server(engine, community_id, "survival")
    worker = uuid.uuid4()
    async with ServersUnitOfWork(factory) as uow:
        server = await uow.servers.get_by_id(server_id)
        assert server is not None
        server.assigned_worker_id = WorkerId(worker)
        server.desired_state = DesiredState.RUNNING
        await uow.servers.update_lifecycle(
            server, expected_from=DesiredState.STOPPED, require_unassigned=True
        )
        await uow.commit()

    async with factory() as session:
        repo = SqlAlchemyServerRepository(session)
        assert await repo.count_running_for_worker(WorkerId(worker)) == 1
        assert await repo.count_running_for_worker(WorkerId(uuid.uuid4())) == 0


async def test_repository_list_running_assigned(engine: AsyncEngine) -> None:
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    running = await _create_server(engine, community_id, "running")
    await _create_server(engine, community_id, "stopped")  # stays desired=stopped
    worker = uuid.uuid4()
    async with ServersUnitOfWork(factory) as uow:
        server = await uow.servers.get_by_id(running)
        assert server is not None
        server.assigned_worker_id = WorkerId(worker)
        server.desired_state = DesiredState.RUNNING
        await uow.servers.update_lifecycle(
            server, expected_from=DesiredState.STOPPED, require_unassigned=True
        )
        await uow.commit()

    async with factory() as session:
        repo = SqlAlchemyServerRepository(session)
        candidates = await repo.list_running_assigned()
    # Only the running, Worker-assigned server is a snapshot candidate.
    assert [s.id for s in candidates] == [running]
