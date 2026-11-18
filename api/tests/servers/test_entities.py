"""Entity invariants for the servers domain: the at-rest policy and the last crash."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    CrashReason,
    DesiredState,
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


_EARLIER = _NOW - dt.timedelta(minutes=1)
_LATER = _NOW + dt.timedelta(minutes=1)


@pytest.mark.parametrize(
    ("observed", "reason", "observed_at", "expected"),
    [
        (ObservedState.CRASHED, CrashReason.FORGE_INSTALL_OUT_OF_MEMORY, _LATER, True),
        (
            ObservedState.CRASHED,
            CrashReason.FORGE_INSTALL_JAVA_INCOMPATIBLE,
            _LATER,
            True,
        ),
        # The Worker retries this one itself, and an unclassified crash may pass.
        (ObservedState.CRASHED, CrashReason.FORGE_INSTALL_FAILED, _LATER, False),
        (ObservedState.CRASHED, None, _LATER, False),
        # A write to the row since the crash (a start, an edit) re-arms one restart;
        # a tie falls to the restart too.
        (
            ObservedState.CRASHED,
            CrashReason.FORGE_INSTALL_OUT_OF_MEMORY,
            _EARLIER,
            False,
        ),
        (ObservedState.CRASHED, CrashReason.FORGE_INSTALL_OUT_OF_MEMORY, _NOW, False),
        (ObservedState.CRASHED, CrashReason.FORGE_INSTALL_OUT_OF_MEMORY, None, False),
        (ObservedState.RUNNING, CrashReason.FORGE_INSTALL_OUT_OF_MEMORY, _LATER, False),
    ],
    ids=[
        "oom",
        "java",
        "install-failed",
        "unclassified",
        "row-written-since",
        "tie",
        "never-observed",
        "not-crashed",
    ],
)
def test_crash_not_restarted(
    observed: ObservedState,
    reason: CrashReason | None,
    observed_at: dt.datetime | None,
    expected: bool,
) -> None:
    # Issue #3281: the crash the reconciler leaves alone, as the read model
    # reports it.
    server = _server(desired=DesiredState.RUNNING, observed=observed)
    server.observed_at = observed_at
    server.crash_reason = reason

    assert server.crash_not_restarted() is expected


def test_crash_not_restarted_only_under_a_running_intent() -> None:
    # Issue #3281: a crash that lands after a stop commit is newer than the row's
    # last write, but no restart is owed under a stopped intent, so the read model
    # does not report one as withheld.
    server = _server(desired=DesiredState.STOPPED, observed=ObservedState.CRASHED)
    server.observed_at = _LATER
    server.crash_reason = CrashReason.FORGE_INSTALL_OUT_OF_MEMORY

    assert server.crash_not_restarted() is False


@pytest.mark.parametrize(
    ("state", "kept"),
    [(ObservedState.CRASHED, True), (ObservedState.RUNNING, False)],
)
def test_observe_reflects_a_landed_write_with_the_crash_it_keeps_or_clears(
    state: ObservedState, kept: bool
) -> None:
    server = _server(desired=DesiredState.RUNNING, observed=ObservedState.CRASHED)
    server.crash_reason = CrashReason.FORGE_INSTALL_OUT_OF_MEMORY
    server.crash_detail = "installer ran out of memory"

    server.observe(state, _LATER)

    assert (server.observed_state, server.observed_at) == (state, _LATER)
    assert (server.crash_reason, server.crash_detail) == (
        (CrashReason.FORGE_INSTALL_OUT_OF_MEMORY, "installer ran out of memory")
        if kept
        else (None, None)
    )
