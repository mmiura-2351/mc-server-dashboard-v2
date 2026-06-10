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
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    WorkerId,
)
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.fakes import (
    FakeClock,
    FakeFileStore,
    FakeVersionValidator,
)

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_OLD = dt.datetime(2026, 6, 4, 11, 0, tzinfo=dt.timezone.utc)
_NEWER = dt.datetime(2026, 6, 4, 13, 0, tzinfo=dt.timezone.utc)


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
        file_store=FakeFileStore(),
        port_range=PortRange(start=25565, end=25664),
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


async def test_record_observed_state_drops_stale_write(engine: AsyncEngine) -> None:
    # Monotonic guard (issue #216): a write stamped older than the row's current
    # observed_at is a no-op, so a stale convergence write cannot clobber a fresher
    # StatusChange that already landed.
    community_id = await _seed_community(engine)
    server_id = await _create_server(engine, community_id, "survival")
    factory = create_session_factory(engine)

    async with ServersUnitOfWork(factory) as uow:
        await uow.servers.record_observed_state(
            server_id, observed_state=ObservedState.RUNNING, observed_at=_NOW
        )
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        await uow.servers.record_observed_state(
            server_id, observed_state=ObservedState.STOPPED, observed_at=_OLD
        )
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(server_id)
    assert loaded is not None
    assert loaded.observed_state is ObservedState.RUNNING
    assert loaded.observed_at == _NOW


async def test_record_observed_state_applies_fresh_write(engine: AsyncEngine) -> None:
    # A write stamped newer than the row's current observed_at wins.
    community_id = await _seed_community(engine)
    server_id = await _create_server(engine, community_id, "survival")
    factory = create_session_factory(engine)

    async with ServersUnitOfWork(factory) as uow:
        await uow.servers.record_observed_state(
            server_id, observed_state=ObservedState.RUNNING, observed_at=_OLD
        )
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        await uow.servers.record_observed_state(
            server_id, observed_state=ObservedState.STOPPED, observed_at=_NOW
        )
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(server_id)
    assert loaded is not None
    assert loaded.observed_state is ObservedState.STOPPED
    assert loaded.observed_at == _NOW


async def test_record_observed_state_returns_applied_flag(engine: AsyncEngine) -> None:
    # Honesty fix (issue #292): the method reports whether the write actually landed
    # (rowcount == 1) so a convergence caller can mutate its returned entity only
    # when the #216 guard accepted the write. A same-instant (equal-stamped) write is
    # dropped and must report applied=False.
    community_id = await _seed_community(engine)
    server_id = await _create_server(engine, community_id, "survival")
    factory = create_session_factory(engine)

    async with ServersUnitOfWork(factory) as uow:
        first = await uow.servers.record_observed_state(
            server_id, observed_state=ObservedState.RUNNING, observed_at=_NOW
        )
        await uow.commit()
    # First write on a NULL observed_at lands.
    assert first is True

    async with ServersUnitOfWork(factory) as uow:
        same_instant = await uow.servers.record_observed_state(
            server_id, observed_state=ObservedState.STOPPED, observed_at=_NOW
        )
        await uow.commit()
    # Equal stamp -> the guard drops it (same-instant duplicate).
    assert same_instant is False

    async with ServersUnitOfWork(factory) as uow:
        fresher = await uow.servers.record_observed_state(
            server_id, observed_state=ObservedState.STOPPED, observed_at=_NEWER
        )
        await uow.commit()
    # A strictly fresher stamp lands.
    assert fresher is True


async def test_record_observed_state_accepts_first_write_on_null_observed_at(
    engine: AsyncEngine,
) -> None:
    # A never-observed row has observed_at IS NULL; the guard must still accept the
    # first write (NULL < anything would otherwise drop it).
    community_id = await _seed_community(engine)
    server_id = await _create_server(engine, community_id, "survival")
    factory = create_session_factory(engine)

    async with ServersUnitOfWork(factory) as uow:
        created = await uow.servers.get_by_id(server_id)
    assert created is not None
    assert created.observed_at is None

    async with ServersUnitOfWork(factory) as uow:
        await uow.servers.record_observed_state(
            server_id, observed_state=ObservedState.RUNNING, observed_at=_NOW
        )
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(server_id)
    assert loaded is not None
    assert loaded.observed_state is ObservedState.RUNNING
    assert loaded.observed_at == _NOW


async def test_record_observed_state_drops_stale_unassign_in_same_statement(
    engine: AsyncEngine,
) -> None:
    # The guard and the #220 unassign share one UPDATE: when the write is stale its
    # unassign decision is stale too, so dropping the row keeps the assignment.
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
        # A fresh observed write lands first.
        await uow.servers.record_observed_state(
            server_id, observed_state=ObservedState.RUNNING, observed_at=_NOW
        )
        await uow.commit()

    # A stale stop-convergence write would unassign — but it is older, so the whole
    # statement is a no-op and the assignment survives.
    async with ServersUnitOfWork(factory) as uow:
        await uow.servers.record_observed_state(
            server_id,
            observed_state=ObservedState.STOPPED,
            observed_at=_OLD,
            unassign=True,
        )
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(server_id)
    assert loaded is not None
    assert loaded.observed_state is ObservedState.RUNNING
    assert loaded.assigned_worker_id == WorkerId(worker)


async def test_mark_worker_servers_unknown_overrides_fresher_observed_at(
    engine: AsyncEngine,
) -> None:
    # Bulk cache-invalidation always wins (issue #216 ruling): a worker disconnect
    # marks state LESS certain (-> unknown) and must override even a row whose
    # observed_at is newer than the invalidation stamp.
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
        await uow.servers.record_observed_state(
            server_id, observed_state=ObservedState.RUNNING, observed_at=_NOW
        )
        await uow.commit()

    # _OLD is earlier than the row's _NOW observed_at, yet the invalidation lands.
    async with ServersUnitOfWork(factory) as uow:
        await uow.servers.mark_worker_servers_unknown(WorkerId(worker), _OLD)
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(server_id)
    assert loaded is not None
    assert loaded.observed_state is ObservedState.UNKNOWN
    assert loaded.observed_at == _OLD


async def test_reset_unverifiable_overrides_fresher_observed_at(
    engine: AsyncEngine,
) -> None:
    # The startup reset is bulk cache-invalidation too: it always wins regardless
    # of the row's observed_at (issue #216 ruling).
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
        await uow.servers.record_observed_state(
            server_id, observed_state=ObservedState.RUNNING, observed_at=_NOW
        )
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        count = await uow.servers.reset_unverifiable_observed_states(_OLD)
        await uow.commit()
    assert count == 1

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(server_id)
    assert loaded is not None
    assert loaded.observed_state is ObservedState.UNKNOWN
    assert loaded.observed_at == _OLD


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


async def test_sink_unassigns_when_owning_worker_reports_stopped_under_desired_stopped(
    engine: AsyncEngine,
) -> None:
    # Timeout-resilient confirmation point (issue #217): when the stop dispatch
    # outcome times out, the in-band #209 unassign is lost. The owning worker's
    # later StatusChange(stopped) under desired=stopped is the authoritative "no
    # live instance" signal, so the sink clears the assignment in the same write.
    community_id = await _seed_community(engine)
    server_id = await _create_server(engine, community_id, "survival")
    worker = uuid.uuid4()
    factory = create_session_factory(engine)

    async with ServersUnitOfWork(factory) as uow:
        server = await uow.servers.get_by_id(server_id)
        assert server is not None
        # desired_state stays STOPPED (a graceful stop already flipped intent);
        # the row is still assigned because the outcome timed out.
        server.assigned_worker_id = WorkerId(worker)
        server.updated_at = _NOW
        await uow.servers.update_lifecycle(
            server, expected_from=DesiredState.STOPPED, require_unassigned=True
        )
        await uow.commit()

    sink = ServersServerStateSink(factory, clock=FakeClock(_NOW))
    await sink.record_observed_state(
        server_id=str(server_id.value), worker_id=str(worker), state="stopped"
    )

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(server_id)
    assert loaded is not None
    assert loaded.observed_state is ObservedState.STOPPED
    assert loaded.assigned_worker_id is None


async def test_sink_keeps_assignment_when_owning_worker_reports_stopped_under_running(
    engine: AsyncEngine,
) -> None:
    # A stopped report while desired=running is a divergence the reconciler owns;
    # the sink must not unassign (that would break stickiness, issue #206).
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
        server_id=str(server_id.value), worker_id=str(worker), state="stopped"
    )

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(server_id)
    assert loaded is not None
    assert loaded.observed_state is ObservedState.STOPPED
    assert loaded.assigned_worker_id == WorkerId(worker)


@pytest.mark.parametrize("state", ["running", "crashed"])
async def test_sink_keeps_assignment_for_non_stopped_reports_under_desired_stopped(
    engine: AsyncEngine, state: str
) -> None:
    # Only a stopped report confirms no live instance; running/crashed never
    # unassign, even under desired=stopped (issue #217).
    community_id = await _seed_community(engine)
    server_id = await _create_server(engine, community_id, "survival")
    worker = uuid.uuid4()
    factory = create_session_factory(engine)

    async with ServersUnitOfWork(factory) as uow:
        server = await uow.servers.get_by_id(server_id)
        assert server is not None
        server.assigned_worker_id = WorkerId(worker)
        server.updated_at = _NOW
        await uow.servers.update_lifecycle(
            server, expected_from=DesiredState.STOPPED, require_unassigned=True
        )
        await uow.commit()

    sink = ServersServerStateSink(factory, clock=FakeClock(_NOW))
    await sink.record_observed_state(
        server_id=str(server_id.value), worker_id=str(worker), state=state
    )

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(server_id)
    assert loaded is not None
    assert loaded.assigned_worker_id == WorkerId(worker)


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


async def test_sink_returns_running_assignment_ids(engine: AsyncEngine) -> None:
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
    ids = await sink.running_assignment_ids(worker_id=str(worker))
    # id -> declared memory (#843); these servers declare no limit, so 0.
    assert ids == {str(running.value): 0}


async def test_repository_running_assignment_ids_for_worker(
    engine: AsyncEngine,
) -> None:
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
        assert await repo.running_assignment_ids_for_worker(WorkerId(worker)) == {
            str(server_id.value): 0
        }
        assert (
            await repo.running_assignment_ids_for_worker(WorkerId(uuid.uuid4())) == {}
        )


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


async def _assign_with_observed(
    engine: AsyncEngine,
    server_id: ServerId,
    worker: uuid.UUID,
    observed: ObservedState,
) -> None:
    factory = create_session_factory(engine)
    async with ServersUnitOfWork(factory) as uow:
        server = await uow.servers.get_by_id(server_id)
        assert server is not None
        server.assigned_worker_id = WorkerId(worker)
        server.desired_state = DesiredState.RUNNING
        await uow.servers.update_lifecycle(
            server, expected_from=DesiredState.STOPPED, require_unassigned=True
        )
        await uow.servers.record_observed_state(
            server_id, observed_state=observed, observed_at=_OLD
        )
        await uow.commit()


async def test_reset_marks_non_terminal_assigned_unknown_keeping_assignment(
    engine: AsyncEngine,
) -> None:
    # Each non-terminal observed state on an assigned row is invalidated to
    # unknown with a fresh observed_at; the assignment is kept (stickiness).
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    worker = uuid.uuid4()
    by_state: dict[ObservedState, ServerId] = {}
    for observed in (
        ObservedState.STARTING,
        ObservedState.RUNNING,
        ObservedState.STOPPING,
        ObservedState.RESTARTING,
    ):
        sid = await _create_server(engine, community_id, observed.value)
        await _assign_with_observed(engine, sid, worker, observed)
        by_state[observed] = sid

    async with ServersUnitOfWork(factory) as uow:
        count = await uow.servers.reset_unverifiable_observed_states(_NOW)
        await uow.commit()
    assert count == 4

    for observed, sid in by_state.items():
        async with ServersUnitOfWork(factory) as uow:
            loaded = await uow.servers.get_by_id(sid)
        assert loaded is not None
        assert loaded.observed_state is ObservedState.UNKNOWN
        assert loaded.observed_at == _NOW
        assert loaded.assigned_worker_id == WorkerId(worker)


@pytest.mark.parametrize(
    "observed", [ObservedState.STOPPED, ObservedState.CRASHED, ObservedState.UNKNOWN]
)
async def test_reset_leaves_terminal_observed_states_untouched(
    engine: AsyncEngine, observed: ObservedState
) -> None:
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    worker = uuid.uuid4()
    server_id = await _create_server(engine, community_id, "survival")
    await _assign_with_observed(engine, server_id, worker, observed)

    async with ServersUnitOfWork(factory) as uow:
        count = await uow.servers.reset_unverifiable_observed_states(_NOW)
        await uow.commit()
    assert count == 0

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(server_id)
    assert loaded is not None
    assert loaded.observed_state is observed
    assert loaded.observed_at == _OLD


async def test_reset_leaves_unassigned_rows_untouched(engine: AsyncEngine) -> None:
    # An unassigned row keeps its observed state even when non-terminal: there is
    # no worker to make the cache unverifiable.
    community_id = await _seed_community(engine)
    factory = create_session_factory(engine)
    server_id = await _create_server(engine, community_id, "survival")
    async with ServersUnitOfWork(factory) as uow:
        await uow.servers.record_observed_state(
            server_id, observed_state=ObservedState.RUNNING, observed_at=_OLD
        )
        await uow.commit()

    async with ServersUnitOfWork(factory) as uow:
        count = await uow.servers.reset_unverifiable_observed_states(_NOW)
        await uow.commit()
    assert count == 0

    async with ServersUnitOfWork(factory) as uow:
        loaded = await uow.servers.get_by_id(server_id)
    assert loaded is not None
    assert loaded.observed_state is ObservedState.RUNNING
    assert loaded.assigned_worker_id is None
