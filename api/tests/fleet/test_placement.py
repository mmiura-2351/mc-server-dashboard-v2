"""Unit tests for the pure greedy placement function (FR-WRK-3).

Exercises the filter (driver capability, free capacity), least-loaded selection,
the deterministic tie-break, and the typed empty-candidate outcome. The function
is pure: it takes candidate snapshots and returns a chosen ``WorkerId`` or a
``NoEligibleWorker`` result, never an exception used for flow control.
"""

from __future__ import annotations

import pytest

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
    memory_capacity_mb: int = 0,
    committed_memory_mb: int = 0,
    committed_cpu_millis: int = 0,
) -> PlacementCandidate:
    return PlacementCandidate(
        worker_id=WorkerId(worker_id),
        drivers=drivers,
        capacity=capacity,
        load=load,
        memory_capacity_mb=memory_capacity_mb,
        committed_memory_mb=committed_memory_mb,
        committed_cpu_millis=committed_cpu_millis,
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


def test_can_host_rejects_non_positive_needed() -> None:
    with pytest.raises(ValueError):
        _candidate("worker-1").can_host(
            required_driver=DriverKind.HOST_PROCESS, needed=0
        )


def test_memory_gate_excludes_over_committed_host() -> None:
    # 8192 MiB host, reserve = max(1024, 819) = 1024 -> usable 7168. Committed
    # 6144 + request 2048 = 8192 > 7168, so the host is excluded.
    result = place(
        [_candidate("worker-1", memory_capacity_mb=8192, committed_memory_mb=6144)],
        required_driver=DriverKind.HOST_PROCESS,
        needed_memory_mb=2048,
    )
    assert isinstance(result, NoEligibleWorker)


def test_memory_gate_admits_host_with_room() -> None:
    # Committed 2048 + request 2048 = 4096 <= 7168 usable, so it fits.
    result = place(
        [_candidate("worker-1", memory_capacity_mb=8192, committed_memory_mb=2048)],
        required_driver=DriverKind.HOST_PROCESS,
        needed_memory_mb=2048,
    )
    assert result == WorkerId("worker-1")


def test_memory_gate_picks_a_host_that_fits_over_one_that_does_not() -> None:
    # worker-1 is least-loaded but over-committed on memory; worker-2 fits.
    result = place(
        [
            _candidate(
                "worker-1", load=0, memory_capacity_mb=4096, committed_memory_mb=3072
            ),
            _candidate(
                "worker-2", load=1, memory_capacity_mb=8192, committed_memory_mb=1024
            ),
        ],
        required_driver=DriverKind.HOST_PROCESS,
        needed_memory_mb=2048,
    )
    assert result == WorkerId("worker-2")


def test_unset_request_memory_skips_the_gate() -> None:
    # No declared request memory -> count-only filter, even on a full-ish host.
    result = place(
        [_candidate("worker-1", memory_capacity_mb=2048, committed_memory_mb=2048)],
        required_driver=DriverKind.HOST_PROCESS,
        needed_memory_mb=None,
    )
    assert result == WorkerId("worker-1")


def test_host_advertising_no_memory_is_not_gated() -> None:
    # memory_capacity_mb == 0 means resources were never advertised; fall back to
    # the count-only filter rather than excluding the host.
    result = place(
        [_candidate("worker-1", memory_capacity_mb=0, committed_memory_mb=0)],
        required_driver=DriverKind.HOST_PROCESS,
        needed_memory_mb=4096,
    )
    assert result == WorkerId("worker-1")


def test_no_eligible_worker_when_nothing_fits_memory() -> None:
    result = place(
        [
            _candidate("worker-1", memory_capacity_mb=4096, committed_memory_mb=4096),
            _candidate("worker-2", memory_capacity_mb=2048, committed_memory_mb=1024),
        ],
        required_driver=DriverKind.HOST_PROCESS,
        needed_memory_mb=2048,
    )
    assert isinstance(result, NoEligibleWorker)


def test_cpu_breaks_a_count_tie_toward_least_committed() -> None:
    # Equal load; the host carrying less committed CPU wins (soft CPU rank),
    # ahead of the lexicographic worker-id tie-break.
    result = place(
        [
            _candidate("worker-a", load=2, committed_cpu_millis=4000),
            _candidate("worker-b", load=2, committed_cpu_millis=1000),
        ],
        required_driver=DriverKind.HOST_PROCESS,
    )
    assert result == WorkerId("worker-b")


def test_count_outranks_cpu() -> None:
    # CPU is only a tie-break: a less-loaded host wins even with more committed
    # CPU than a more-loaded one.
    result = place(
        [
            _candidate("worker-1", load=1, committed_cpu_millis=8000),
            _candidate("worker-2", load=3, committed_cpu_millis=500),
        ],
        required_driver=DriverKind.HOST_PROCESS,
    )
    assert result == WorkerId("worker-1")


def test_worker_id_breaks_a_cpu_tie() -> None:
    result = place(
        [
            _candidate("worker-b", load=1, committed_cpu_millis=1000),
            _candidate("worker-a", load=1, committed_cpu_millis=1000),
        ],
        required_driver=DriverKind.HOST_PROCESS,
    )
    assert result == WorkerId("worker-a")
