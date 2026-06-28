"""Use-case test for the startup observed-state reset (issue #224).

After a full-stack restart the API never observed the workers' heartbeat lapse,
so rows can persist as ``(desired=running, observed=running, assigned)`` with no
instance — phantom running forever. On startup, before the reconciler begins,
:class:`ResetUnverifiableObservedStates` marks every assigned server whose
observed state is non-terminal as ``observed=unknown`` (keeping the assignment),
so the reconciler converges truthfully. Terminal/cache-stable observed states
(``stopped``, ``crashed``, ``unknown``) and unassigned rows are left untouched.

Driven against in-memory fakes with a faked clock.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from mc_server_dashboard_api.servers.application.startup_reset import (
    ResetUnverifiableObservedStates,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
)
from tests.servers.fakes import FakeClock, FakeUnitOfWork

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_OLD = dt.datetime(2026, 6, 4, 11, 0, tzinfo=dt.timezone.utc)
_WORKER = WorkerId(uuid.uuid4())


def _server(
    *,
    observed: ObservedState,
    worker: WorkerId | None,
    desired: DesiredState = DesiredState.RUNNING,
) -> Server:
    return Server(
        id=ServerId.new(),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        config={},
        desired_state=desired,
        observed_state=observed,
        observed_at=_OLD,
        assigned_worker_id=worker,
        created_at=_OLD,
        updated_at=_OLD,
    )


_NON_TERMINAL = [
    ObservedState.RUNNING,
    ObservedState.STARTING,
    ObservedState.STOPPING,
    ObservedState.RESTARTING,
]


@pytest.mark.parametrize("observed", _NON_TERMINAL)
async def test_non_terminal_assigned_marked_unknown_keeping_assignment(
    observed: ObservedState,
) -> None:
    uow = FakeUnitOfWork()
    server = _server(observed=observed, worker=_WORKER)
    uow.servers.seed(server)

    count = await ResetUnverifiableObservedStates(uow=uow, clock=FakeClock(_NOW))()

    assert count == 1
    loaded = await uow.servers.get_by_id(server.id)
    assert loaded is not None
    assert loaded.observed_state is ObservedState.UNKNOWN
    assert loaded.observed_at == _NOW
    # Assignment is kept (stickiness): only the observed cache is invalidated.
    assert loaded.assigned_worker_id == _WORKER
    assert uow.commits == 1


@pytest.mark.parametrize(
    "observed",
    [ObservedState.STOPPED, ObservedState.CRASHED, ObservedState.UNKNOWN],
)
async def test_terminal_observed_states_untouched(observed: ObservedState) -> None:
    uow = FakeUnitOfWork()
    server = _server(observed=observed, worker=_WORKER)
    uow.servers.seed(server)

    count = await ResetUnverifiableObservedStates(uow=uow, clock=FakeClock(_NOW))()

    assert count == 0
    loaded = await uow.servers.get_by_id(server.id)
    assert loaded is not None
    assert loaded.observed_state is observed
    assert loaded.observed_at == _OLD


async def test_unassigned_rows_untouched() -> None:
    uow = FakeUnitOfWork()
    server = _server(observed=ObservedState.RUNNING, worker=None)
    uow.servers.seed(server)

    count = await ResetUnverifiableObservedStates(uow=uow, clock=FakeClock(_NOW))()

    assert count == 0
    loaded = await uow.servers.get_by_id(server.id)
    assert loaded is not None
    assert loaded.observed_state is ObservedState.RUNNING
    assert loaded.observed_at == _OLD
