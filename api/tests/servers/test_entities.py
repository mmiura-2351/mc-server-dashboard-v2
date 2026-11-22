"""Entity invariants for the servers domain: the at-rest policy and the last crash."""

from __future__ import annotations

import datetime as dt
import itertools
import pathlib
import re
import uuid

import pytest

from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.errors import (
    ServerAssignedError,
    ServerFilesUnsettledError,
)
from mc_server_dashboard_api.servers.domain.value_objects import (
    NOT_RESTARTED_CRASH_REASONS,
    CommunityId,
    CrashReason,
    DesiredState,
    ObservedState,
    ServerId,
    ServerName,
    ServerType,
    WorkerId,
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


_AT_REST_OBSERVED = (
    ObservedState.STOPPED,
    ObservedState.UNKNOWN,
    ObservedState.CRASHED,
)


@pytest.mark.parametrize(
    "desired,observed,assigned",
    list(itertools.product(DesiredState, ObservedState, [False, True])),
)
def test_quiescent_only_at_rest_and_unassigned(
    desired: DesiredState, observed: ObservedState, assigned: bool
) -> None:
    # Issue #3998: a stop keeps its Worker assigned until the final snapshot
    # settles, and that snapshot publishes over the store, so a server is quiescent
    # (nothing can publish over it) only once it is at rest AND released.
    server = _server(desired=desired, observed=observed)
    if assigned:
        server.assigned_worker_id = WorkerId(uuid.uuid4())

    expected = (
        desired is DesiredState.STOPPED
        and observed in _AT_REST_OBSERVED
        and not assigned
    )
    assert server.is_quiescent() is expected


@pytest.mark.parametrize("observed", _AT_REST_OBSERVED)
def test_a_door_refuses_an_assigned_server_at_rest_as_assigned(
    observed: ObservedState,
) -> None:
    server = _server(desired=DesiredState.STOPPED, observed=observed)
    server.assigned_worker_id = WorkerId(uuid.uuid4())

    error = server.not_quiescent_error(ServerFilesUnsettledError)

    assert type(error) is ServerAssignedError
    assert str(error) == str(server.id.value)


@pytest.mark.parametrize("assigned", [False, True])
def test_a_door_refuses_a_server_not_at_rest_with_its_own_error(
    assigned: bool,
) -> None:
    # The door's own not-at-rest error comes first: a running server is refused
    # as unsettled whether or not it is assigned.
    server = _server(desired=DesiredState.RUNNING, observed=ObservedState.RUNNING)
    if assigned:
        server.assigned_worker_id = WorkerId(uuid.uuid4())

    error = server.not_quiescent_error(ServerFilesUnsettledError)

    assert type(error) is ServerFilesUnsettledError
    assert str(error) == str(server.id.value)


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


def test_the_web_ui_holds_the_same_not_restarted_reasons() -> None:
    # Issue #3281: no status frame carries ``crash_not_restarted``, so the Web UI
    # derives it from a crashed frame's reason with its own copy of the set. A
    # reason added here only would leave its live flag false, and "applying..."
    # shown on a server nothing converges. The set is not in the OpenAPI
    # document, so the client mirrors it.
    state = (
        pathlib.Path(__file__).resolve().parents[3] / "webui/src/pages/serverState.ts"
    )
    declared = re.search(
        r"export const NOT_RESTARTED_CRASH_REASONS: ReadonlySet<string> = new Set\(\["
        r"(?P<body>[^\]]*)\]\);",
        state.read_text(),
    )

    assert declared is not None
    mirrored = re.findall(r'"([^"]*)"', declared["body"])
    assert sorted(mirrored) == sorted(r.value for r in NOT_RESTARTED_CRASH_REASONS)


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
