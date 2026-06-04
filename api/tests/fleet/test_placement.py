"""Unit tests for the pure greedy placement function (FR-WRK-3).

Exercises the filter (driver capability, free capacity), least-loaded selection,
the deterministic tie-break, and the typed empty-candidate outcome. The function
is pure: it takes candidate snapshots and returns a chosen ``WorkerId`` or a
``NoEligibleWorker`` result, never an exception used for flow control.
"""

from __future__ import annotations

from mc_server_dashboard_api.fleet.domain.placement import (
    NoEligibleWorker,
    PlacementCandidate,
    place,
)
from mc_server_dashboard_api.fleet.domain.value_objects import DriverKind, WorkerId


def _candidate(
    worker_id: str,
    *,
    drivers: frozenset[DriverKind] = frozenset({DriverKind.HOST_PROCESS}),
    capacity: int = 4,
    load: int = 0,
) -> PlacementCandidate:
    return PlacementCandidate(
        worker_id=WorkerId(worker_id),
        drivers=drivers,
        capacity=capacity,
        load=load,
    )


def test_picks_the_only_eligible_worker() -> None:
    result = place([_candidate("worker-1")], required_driver=DriverKind.HOST_PROCESS)
    assert result == WorkerId("worker-1")


def test_filters_out_worker_lacking_required_driver() -> None:
    result = place(
        [_candidate("worker-1", drivers=frozenset({DriverKind.CONTAINER}))],
        required_driver=DriverKind.HOST_PROCESS,
    )
    assert isinstance(result, NoEligibleWorker)


def test_filters_out_full_worker() -> None:
    result = place(
        [_candidate("worker-1", capacity=2, load=2)],
        required_driver=DriverKind.HOST_PROCESS,
    )
    assert isinstance(result, NoEligibleWorker)


def test_zero_capacity_means_no_advertised_cap() -> None:
    result = place(
        [_candidate("worker-1", capacity=0, load=99)],
        required_driver=DriverKind.HOST_PROCESS,
    )
    assert result == WorkerId("worker-1")


def test_picks_least_loaded_candidate() -> None:
    result = place(
        [
            _candidate("worker-1", load=3),
            _candidate("worker-2", load=1),
            _candidate("worker-3", load=2),
        ],
        required_driver=DriverKind.HOST_PROCESS,
    )
    assert result == WorkerId("worker-2")


def test_tie_break_is_lexicographic_worker_id() -> None:
    result = place(
        [
            _candidate("worker-b", load=1),
            _candidate("worker-a", load=1),
        ],
        required_driver=DriverKind.HOST_PROCESS,
    )
    assert result == WorkerId("worker-a")


def test_empty_candidate_list_is_no_eligible_worker() -> None:
    result = place([], required_driver=DriverKind.HOST_PROCESS)
    assert isinstance(result, NoEligibleWorker)
