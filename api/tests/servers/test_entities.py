"""Entity invariants for the servers domain: the at-rest policy."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

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

_NOW = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)


def _server(*, desired: DesiredState, observed: ObservedState) -> Server:
    return Server(
        id=ServerId.new(),
        community_id=CommunityId(uuid.uuid4()),
        name=ServerName("survival"),
        mc_edition="java",
        mc_version="1.21.1",
        server_type=ServerType.VANILLA,
        execution_backend=ExecutionBackend.HOST_PROCESS,
        config={},
        desired_state=desired,
        observed_state=observed,
        observed_at=None,
        assigned_worker_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


@pytest.mark.parametrize(
    "observed",
    [ObservedState.STOPPED, ObservedState.UNKNOWN, ObservedState.CRASHED],
)
def test_at_rest_when_desired_stopped_and_observed_stopped_unknown_or_crashed(
    observed: ObservedState,
) -> None:
    assert _server(desired=DesiredState.STOPPED, observed=observed).is_at_rest()


@pytest.mark.parametrize(
    "desired,observed",
    [
        (DesiredState.RUNNING, ObservedState.RUNNING),
        (DesiredState.RUNNING, ObservedState.STOPPED),
        (DesiredState.RUNNING, ObservedState.CRASHED),
        (DesiredState.STOPPED, ObservedState.RUNNING),
        (DesiredState.STOPPED, ObservedState.STOPPING),
    ],
)
def test_not_at_rest_otherwise(desired: DesiredState, observed: ObservedState) -> None:
    assert not _server(desired=desired, observed=observed).is_at_rest()
