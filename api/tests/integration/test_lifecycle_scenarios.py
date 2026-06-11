"""Lifecycle SCENARIO tests: chain operator flows across use cases (issue #207).

The cited bugs (#206 stop->start, #197+#205 EULA repair, #217 timeout-unassign)
share one shape: each use case passes in isolation against a fabricated row, but
the *row state one use case leaves behind* breaks the next. The unit tests in
``tests/servers/test_lifecycle.py`` pin each use case alone; the repository tests
in ``tests/integration/test_lifecycle_repositories.py`` pin each SQL write alone.
Neither chains a realistic operator sequence over ONE persisted row.

These tests close that gap. They run the real use cases (StartServer / StopServer)
against the real repositories + UnitOfWork + the real ServersServerStateSink over
a real PostgreSQL row, with a fake control plane for command dispatch (the network
seam) and -- except where storage is the point -- a fake file store. They assert
the row-state invariant that links one use case to the next, so a regression that
only shows up in the *handoff* between use cases is caught structurally.

DB-gated (TESTING.md Section 5): run only when ``MCD_TEST_DATABASE_URL`` is set
(the CI Postgres service), skipped otherwise.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

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
from mc_server_dashboard_api.servers.adapters.file_store import (
    StorageFileStoreAdapter,
)
from mc_server_dashboard_api.servers.adapters.server_state_sink import (
    ServersServerStateSink,
)
from mc_server_dashboard_api.servers.adapters.unit_of_work import (
    SqlAlchemyUnitOfWork as ServersUnitOfWork,
)
from mc_server_dashboard_api.servers.application.lifecycle import (
    StartServer,
    StopServer,
)
from mc_server_dashboard_api.servers.application.manage_server import CreateServer
from mc_server_dashboard_api.servers.application.reconciler import RunReconcilerTick
from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
    WorkerUnavailableError,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.ports import PortRange
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    WorkerId,
)
from mc_server_dashboard_api.storage.adapters.fs import FsStorage
from mc_server_dashboard_api.storage.domain.value_objects import (
    CommunityId as StorageCommunityId,
)
from mc_server_dashboard_api.storage.domain.value_objects import (
    ServerId as StorageServerId,
)
from tests.integration.migrate import downgrade_base, upgrade_head
from tests.servers.fakes import (
    FakeControlPlane,
    FakeFileStore,
    FakeJarProvisioner,
    FakeStoreGenerationReader,
    FakeVersionValidator,
)
from tests.storage.helpers import drain, read_tar

_DB_URL = os.environ.get("MCD_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    _DB_URL is None, reason="MCD_TEST_DATABASE_URL not set (no real database)"
)

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


class _AdvancingClock(Clock):
    """A clock that advances one second on every ``now()`` call.

    Production wall-clock time always moves forward between use cases, so a later
    observed-state write is stamped after an earlier one and the repository's
    monotonic guard (issue #216, ``observed_at < new``) accepts it. A single fixed
    instant shared across a chain would instead make every cross-use-case write
    collide on the guard and be silently dropped -- a test artifact, not the
    behaviour under test. A monotonically advancing clock models real time so the
    chain exercises the guard exactly as production does.

    Each call across the WHOLE chain must advance, so one instance is shared by
    every use case and sink in a test.
    """

    def __init__(self, start: dt.datetime) -> None:
        self._next = start

    def now(self) -> dt.datetime:
        current = self._next
        self._next = current + dt.timedelta(seconds=1)
        return current


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


def _create_server_use_case(
    engine: AsyncEngine,
    file_store: FakeFileStore | StorageFileStoreAdapter,
    clock: Clock,
) -> CreateServer:
    return CreateServer(
        uow=ServersUnitOfWork(create_session_factory(engine)),
        clock=clock,
        version_validator=FakeVersionValidator(),
        file_store=file_store,
        port_range=PortRange(start=25565, end=25664),
    )


def _start_server_use_case(
    engine: AsyncEngine, control_plane: FakeControlPlane, clock: Clock
) -> StartServer:
    return StartServer(
        uow=ServersUnitOfWork(create_session_factory(engine)),
        control_plane=control_plane,
        clock=clock,
        jar_provisioner=FakeJarProvisioner(),
        store_generation=FakeStoreGenerationReader(),
    )


def _stop_server_use_case(
    engine: AsyncEngine, control_plane: FakeControlPlane, clock: Clock
) -> StopServer:
    return StopServer(
        uow=ServersUnitOfWork(create_session_factory(engine)),
        control_plane=control_plane,
        clock=clock,
    )


def _sink(engine: AsyncEngine, clock: Clock) -> ServersServerStateSink:
    return ServersServerStateSink(create_session_factory(engine), clock=clock)


async def _reconciler_tick(
    engine: AsyncEngine, control_plane: FakeControlPlane, clock: Clock
) -> None:
    # grace_seconds=0 so a divergence is actionable immediately (the _AdvancingClock
    # already moves time forward between each use case, so the wedged row is past its
    # zero-second grace by the time the tick reads it).
    factory = create_session_factory(engine)
    await RunReconcilerTick(
        uow=ServersUnitOfWork(factory),
        make_start_server=lambda: _start_server_use_case(engine, control_plane, clock),
        make_stop_server=lambda: _stop_server_use_case(engine, control_plane, clock),
        control_plane=control_plane,
        clock=clock,
        grace_seconds=0,
        backoff_base_seconds=30,
        backoff_max_seconds=3600,
    ).tick()


async def _create_server(
    engine: AsyncEngine,
    file_store: FakeFileStore | StorageFileStoreAdapter,
    clock: Clock,
) -> Server:
    community_id = await _seed_community(engine)
    return await _create_server_use_case(engine, file_store, clock)(
        community_id=CommunityId(community_id),
        name="survival",
        mc_edition="java",
        mc_version="1.21.1",
        server_type="vanilla",
        execution_backend="host_process",
        config={},
    )


async def _load(engine: AsyncEngine, server_id: ServerId) -> Server | None:
    factory = create_session_factory(engine)
    async with ServersUnitOfWork(factory) as uow:
        return await uow.servers.get_by_id(server_id)


# --- Chain 1: stop->start regression (issue #206) --------------------------


async def test_crash_then_stop_then_start_again_succeeds(engine: AsyncEngine) -> None:
    """create -> start -> worker reports crashed (real sink) -> stop -> start again.

    The #206 regression chain. After the worker crashes, the operator stops the
    server; the stop must clear the assignment (converge + unassign) so the next
    start's ``require_unassigned`` compare-and-set can re-place the server rather
    than 409ing forever. Every step runs against the same persisted row.
    """

    clock = _AdvancingClock(_NOW)
    server = await _create_server(engine, FakeFileStore(), clock)
    server_id = server.id
    community = server.community_id

    first_worker = uuid.uuid4()
    started = await _start_server_use_case(
        engine, FakeControlPlane(place_to=WorkerId(first_worker)), clock
    )(community_id=community, server_id=server_id)
    assert started.desired_state is DesiredState.RUNNING
    assert started.assigned_worker_id == WorkerId(first_worker)

    # The worker reports it crashed via the REAL sink (the gRPC StatusChange path).
    # Under desired=running this is a divergence the reconciler owns; the sink must
    # keep the assignment (no unassign), so the row stays (running, crashed, worker).
    await _sink(engine, clock).record_observed_state(
        server_id=str(server_id.value), worker_id=str(first_worker), state="crashed"
    )
    crashed = await _load(engine, server_id)
    assert crashed is not None
    assert crashed.observed_state is ObservedState.CRASHED
    assert crashed.assigned_worker_id == WorkerId(first_worker)

    # The operator stops the crashed server. The worker holds no live instance, so
    # its handleStop answers SERVER_NOT_FOUND; StopServer converges to stopped and
    # clears the assignment (issue #206 / #197).
    stopped = await _stop_server_use_case(
        engine,
        FakeControlPlane(
            outcomes={"stop": CommandOutcome(status=CommandStatus.SERVER_NOT_FOUND)}
        ),
        clock,
    )(community_id=community, server_id=server_id)
    assert stopped.desired_state is DesiredState.STOPPED
    assert stopped.assigned_worker_id is None

    # The persisted row is now unassigned, so the next start re-places (the bug was
    # the assignment sticking and 409ing this start forever).
    next_worker = uuid.uuid4()
    restarted = await _start_server_use_case(
        engine, FakeControlPlane(place_to=WorkerId(next_worker)), clock
    )(community_id=community, server_id=server_id)
    assert restarted.desired_state is DesiredState.RUNNING
    assert restarted.assigned_worker_id == WorkerId(next_worker)


# --- Chain 2: EULA repair (issue #197 + #205 + #208) -----------------------


async def test_crash_then_stop_then_write_eula_at_rest_then_start(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    """create (no eula) -> start -> crash -> stop -> write eula.txt at rest (real
    FsStorage) -> start succeeds, and the working set carries eula.txt for hydrate.

    The #197+#205 EULA repair flow. A server created without accepting the EULA
    crashes on first boot because the working set has no ``eula.txt``. The operator
    repairs it by writing ``eula.txt`` through the real file store at rest (the #205
    initialize-first-version path on the working set), which publishes a new
    authoritative version. The next start succeeds, and a hydrate would now ship the
    repaired ``eula.txt`` to the worker.
    """

    clock = _AdvancingClock(_NOW)
    storage = FsStorage(tmp_path)
    file_store = StorageFileStoreAdapter(storage=storage)
    # accept_eula defaults to False: only server.properties is seeded, so the
    # working set exists but is MISSING eula.txt -- the crash cause (issue #197).
    server = await _create_server(engine, file_store, clock)
    server_id = server.id
    community = server.community_id
    storage_scope = (
        StorageCommunityId(community.value),
        StorageServerId(server_id.value),
    )

    worker = uuid.uuid4()
    await _start_server_use_case(
        engine, FakeControlPlane(place_to=WorkerId(worker)), clock
    )(community_id=community, server_id=server_id)

    # The worker crashes (a vanilla server exits when eula.txt is missing/false).
    # It reports crashed via the real sink; under desired=running the assignment
    # sticks (the reconciler's divergence), matching chain 1.
    await _sink(engine, clock).record_observed_state(
        server_id=str(server_id.value), worker_id=str(worker), state="crashed"
    )

    # The operator stops the crashed server (no live instance -> SERVER_NOT_FOUND),
    # which converges to stopped and unassigns (issue #206).
    await _stop_server_use_case(
        engine,
        FakeControlPlane(
            outcomes={"stop": CommandOutcome(status=CommandStatus.SERVER_NOT_FOUND)}
        ),
        clock,
    )(community_id=community, server_id=server_id)

    # The working set at rest does NOT carry eula.txt yet (the crash cause).
    before = read_tar(await drain(storage.open_hydrate_source(*storage_scope)))
    assert "eula.txt" not in before

    # The operator writes eula.txt at rest through the real file store (the #205
    # repair path), publishing it into the authoritative working set.
    await file_store.write_file(
        community_id=community,
        server_id=server_id,
        rel_path="eula.txt",
        content=b"eula=true\n",
    )

    # Start succeeds against the repaired, unassigned row.
    next_worker = uuid.uuid4()
    restarted = await _start_server_use_case(
        engine, FakeControlPlane(place_to=WorkerId(next_worker)), clock
    )(community_id=community, server_id=server_id)
    assert restarted.desired_state is DesiredState.RUNNING
    assert restarted.assigned_worker_id == WorkerId(next_worker)

    # The working set now carries eula.txt: the next hydrate ships the repair to the
    # worker (the bytes the StartServer hydrate would stream).
    after = read_tar(await drain(storage.open_hydrate_source(*storage_scope)))
    assert after["eula.txt"] == b"eula=true\n"


# --- Chain 3: timeout-lost stop unassign (issue #217 + #220) ---------------


async def test_lost_stop_outcome_then_worker_reports_stopped_then_reconciler_clears(
    engine: AsyncEngine,
) -> None:
    """start -> stop whose dispatch outcome is LOST (WorkerUnavailableError after
    the intent committed) -> worker's StatusChange(stopped) arrives via the REAL
    sink (which must NOT unassign) -> reconciler's stale-stop arm clears the
    assignment -> start succeeds.

    The #217 scenario reworked for #847. When the stop dispatch times out, the
    in-band unassign that rode the dispatch outcome is lost: the row lands
    (desired=stopped, still assigned). The owning worker's later
    StatusChange(stopped) used to make the sink clear the assignment in the same
    write -- but that #217 sink-unassign raced the final-snapshot window (bug 1), so
    the sink no longer unassigns. The deliberate recovery is now the reconciler's
    stale-stop arm: a row wedged at (stopped, stopped, assigned) past grace has its
    assignment cleared, after which the next start's require_unassigned CAS succeeds.
    """

    clock = _AdvancingClock(_NOW)
    server = await _create_server(engine, FakeFileStore(), clock)
    server_id = server.id
    community = server.community_id

    worker = uuid.uuid4()
    await _start_server_use_case(
        engine, FakeControlPlane(place_to=WorkerId(worker)), clock
    )(community_id=community, server_id=server_id)

    # The stop dispatch's outcome is LOST: the control plane raises
    # WorkerUnavailableError AFTER StopServer committed desired=stopped (and
    # decremented load). The deferred unassign never runs.
    lost_cp = FakeControlPlane(place_to=WorkerId(worker), unavailable_kinds={"stop"})
    with pytest.raises(WorkerUnavailableError):
        await _stop_server_use_case(engine, lost_cp, clock)(
            community_id=community, server_id=server_id
        )

    # The row is now (desired=stopped, still assigned): the lost-outcome window.
    after_stop = await _load(engine, server_id)
    assert after_stop is not None
    assert after_stop.desired_state is DesiredState.STOPPED
    assert after_stop.assigned_worker_id == WorkerId(worker)

    # The owning worker's StatusChange(stopped) arrives via the real sink. It caches
    # observed=stopped but must KEEP the assignment (issue #847 bug 1): unassigning
    # here would race a live final-snapshot window in the normal stop flow.
    await _sink(engine, clock).record_observed_state(
        server_id=str(server_id.value), worker_id=str(worker), state="stopped"
    )
    converged = await _load(engine, server_id)
    assert converged is not None
    assert converged.observed_state is ObservedState.STOPPED
    assert converged.assigned_worker_id == WorkerId(worker)

    # The reconciler's stale-stop arm recovers the wedge: (stopped, stopped,
    # assigned) past grace -> clear the assignment (no command dispatched).
    await _reconciler_tick(engine, FakeControlPlane(), clock)
    recovered = await _load(engine, server_id)
    assert recovered is not None
    assert recovered.assigned_worker_id is None

    # The next start re-places against the now-unassigned row.
    next_worker = uuid.uuid4()
    restarted = await _start_server_use_case(
        engine, FakeControlPlane(place_to=WorkerId(next_worker)), clock
    )(community_id=community, server_id=server_id)
    assert restarted.desired_state is DesiredState.RUNNING
    assert restarted.assigned_worker_id == WorkerId(next_worker)


async def test_stop_cancelled_mid_snapshot_holds_then_reconciler_clears(
    engine: AsyncEngine,
) -> None:
    """start -> stop whose final snapshot is CANCELLED (client disconnect) ->
    assignment HELD (the upload is still live) -> worker's StatusChange(stopped)
    via the REAL sink keeps it held -> reconciler's stale-stop arm clears it ->
    start succeeds (issue #847 bug 2, cancel path).

    A client disconnect cancels the HTTP-request task at the snapshot await. The
    dispatched snapshot keeps uploading worker-side (the proto has no command-cancel),
    so the stop MUST keep the assignment rather than release the row mid-upload and
    let a racing start re-place elsewhere. The deliberate, grace-bounded recovery is
    the reconciler's stale-stop arm — proven here through the real sink + a real tick.
    """

    clock = _AdvancingClock(_NOW)
    server = await _create_server(engine, FakeFileStore(), clock)
    server_id = server.id
    community = server.community_id

    worker = uuid.uuid4()
    await _start_server_use_case(
        engine, FakeControlPlane(place_to=WorkerId(worker)), clock
    )(community_id=community, server_id=server_id)

    class _SnapshotCancelled(FakeControlPlane):
        async def snapshot(
            self,
            *,
            worker_id: WorkerId,
            community_id: CommunityCommunityId,  # type: ignore[override]
            server_id: ServerId,
        ) -> CommandOutcome:
            # The client disconnects: the request task is cancelled at this await.
            raise asyncio.CancelledError

    # The stop confirms (process gone), then the final snapshot await is cancelled.
    with pytest.raises(asyncio.CancelledError):
        await _stop_server_use_case(
            engine, _SnapshotCancelled(place_to=WorkerId(worker)), clock
        )(community_id=community, server_id=server_id)

    # The assignment is HELD: observed=stopped is committed before the snapshot, but
    # the deferred clear is deliberately SKIPPED on cancellation (the upload is live).
    after_stop = await _load(engine, server_id)
    assert after_stop is not None
    assert after_stop.desired_state is DesiredState.STOPPED
    assert after_stop.observed_state is ObservedState.STOPPED
    assert after_stop.assigned_worker_id == WorkerId(worker)

    # The owning worker's terminal StatusChange(stopped) arrives via the real sink;
    # it must KEEP the assignment (the sink no longer unassigns, bug 1).
    await _sink(engine, clock).record_observed_state(
        server_id=str(server_id.value), worker_id=str(worker), state="stopped"
    )
    converged = await _load(engine, server_id)
    assert converged is not None
    assert converged.assigned_worker_id == WorkerId(worker)

    # The reconciler's stale-stop arm recovers the wedge once grace lapses.
    await _reconciler_tick(engine, FakeControlPlane(), clock)
    recovered = await _load(engine, server_id)
    assert recovered is not None
    assert recovered.assigned_worker_id is None

    # The next start re-places against the now-unassigned row.
    next_worker = uuid.uuid4()
    restarted = await _start_server_use_case(
        engine, FakeControlPlane(place_to=WorkerId(next_worker)), clock
    )(community_id=community, server_id=server_id)
    assert restarted.desired_state is DesiredState.RUNNING
    assert restarted.assigned_worker_id == WorkerId(next_worker)


# --- Chain 4 (extra): disconnect marks unknown, stickiness holds (issue #206)


async def test_disconnect_marks_unknown_then_stop_then_start(
    engine: AsyncEngine,
) -> None:
    """start -> worker disconnect (mark unknown via real sink) -> stop -> start.

    The cheap extra chain from the issue body. A worker disconnect marks the row's
    observed state UNKNOWN but must KEEP the assignment (stickiness invariant, issue
    #206): only a confirmed stop unassigns. The operator then stops the server
    (graceful success path, which unassigns) and starts it again.
    """

    clock = _AdvancingClock(_NOW)
    server = await _create_server(engine, FakeFileStore(), clock)
    server_id = server.id
    community = server.community_id

    worker = uuid.uuid4()
    await _start_server_use_case(
        engine, FakeControlPlane(place_to=WorkerId(worker)), clock
    )(community_id=community, server_id=server_id)

    # The worker disconnects: the sink marks its servers unknown but keeps the
    # assignment (stickiness). The row is now (running, unknown, worker).
    await _sink(engine, clock).mark_worker_servers_unknown(worker_id=str(worker))
    after_disconnect = await _load(engine, server_id)
    assert after_disconnect is not None
    assert after_disconnect.observed_state is ObservedState.UNKNOWN
    assert after_disconnect.assigned_worker_id == WorkerId(worker)

    # The operator stops it. A graceful success confirms the process is gone, so the
    # assignment clears (issue #206) and observed converges to stopped.
    stopped = await _stop_server_use_case(engine, FakeControlPlane(), clock)(
        community_id=community, server_id=server_id
    )
    assert stopped.assigned_worker_id is None
    assert stopped.observed_state is ObservedState.STOPPED

    # Start succeeds against the unassigned row.
    next_worker = uuid.uuid4()
    restarted = await _start_server_use_case(
        engine, FakeControlPlane(place_to=WorkerId(next_worker)), clock
    )(community_id=community, server_id=server_id)
    assert restarted.desired_state is DesiredState.RUNNING
    assert restarted.assigned_worker_id == WorkerId(next_worker)
