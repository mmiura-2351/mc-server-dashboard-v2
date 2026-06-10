"""Unit tests for the in-memory WorkerRegistry and its liveness model.

Drives the registry against a faked Clock (no wall-clock dependence,
TESTING.md Section 4): registration, heartbeat refresh, expiry past the
timeout, and disconnect marking offline.
"""

from __future__ import annotations

import datetime as dt

from mc_server_dashboard_api.fleet.adapters.registry import InMemoryWorkerRegistry
from mc_server_dashboard_api.fleet.domain.entities import WorkerStatus
from mc_server_dashboard_api.fleet.domain.value_objects import DriverKind, WorkerId
from tests.fleet.fakes import FakeClock, make_worker

_T0 = dt.datetime(2026, 6, 4, 12, 0, tzinfo=dt.timezone.utc)
_TIMEOUT = dt.timedelta(seconds=30)


def _registry(clock: FakeClock) -> InMemoryWorkerRegistry:
    return InMemoryWorkerRegistry(clock=clock, heartbeat_timeout=_TIMEOUT)


def test_registered_worker_is_listed_online() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))

    workers = registry.list_workers()

    assert len(workers) == 1
    assert workers[0].id == WorkerId("worker-1")
    assert workers[0].status is WorkerStatus.ONLINE
    assert workers[0].version == "1.0.0"


def test_worker_goes_offline_after_heartbeat_timeout() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))

    clock.set(_T0 + _TIMEOUT + dt.timedelta(seconds=1))

    assert registry.list_workers()[0].status is WorkerStatus.OFFLINE


def test_worker_at_exactly_timeout_is_still_online() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))

    clock.set(_T0 + _TIMEOUT)

    assert registry.list_workers()[0].status is WorkerStatus.ONLINE


def test_heartbeat_refreshes_liveness() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))

    later = _T0 + dt.timedelta(seconds=25)
    registry.record_heartbeat(WorkerId("worker-1"), later)
    # Now within the window measured from the fresh heartbeat, not registration.
    clock.set(later + dt.timedelta(seconds=20))

    snapshot = registry.list_workers()[0]
    assert snapshot.status is WorkerStatus.ONLINE
    assert snapshot.last_heartbeat_at == later


def test_disconnect_marks_offline_within_window() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    session = registry.register(make_worker(at=_T0))

    registry.mark_disconnected(WorkerId("worker-1"), session)

    # Still well inside the heartbeat window, but disconnect forces offline.
    assert registry.list_workers()[0].status is WorkerStatus.OFFLINE


def test_reregistration_replaces_prior_record() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    first = registry.register(make_worker(at=_T0))
    registry.mark_disconnected(WorkerId("worker-1"), first)

    # A fresh Session re-registers from scratch (CONTROL_PLANE.md Section 4.4).
    registry.register(make_worker(at=_T0, version="2.0.0"))

    workers = registry.list_workers()
    assert len(workers) == 1
    assert workers[0].status is WorkerStatus.ONLINE
    assert workers[0].version == "2.0.0"


def test_stale_session_disconnect_does_not_offline_reregistered_worker() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    # Session A registers, then the same id re-registers on a new Session B
    # (reconnect with backoff, CONTROL_PLANE.md Section 4.4).
    session_a = registry.register(make_worker(at=_T0))
    registry.register(make_worker(at=_T0, version="2.0.0"))

    # Session A's delayed teardown fires after B is the current session.
    registry.mark_disconnected(WorkerId("worker-1"), session_a)

    # The freshly re-registered Worker must stay ONLINE.
    workers = registry.list_workers()
    assert len(workers) == 1
    assert workers[0].status is WorkerStatus.ONLINE
    assert workers[0].version == "2.0.0"


def test_is_current_session_tracks_the_latest_registration() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    # A reconnect supersedes the prior Session: only the latest token is current,
    # so a stale teardown's bulk observed=unknown write is suppressed (issue #775).
    session_a = registry.register(make_worker(at=_T0))
    assert registry.is_current_session(WorkerId("worker-1"), session_a) is True

    session_b = registry.register(make_worker(at=_T0, version="2.0.0"))
    assert registry.is_current_session(WorkerId("worker-1"), session_a) is False
    assert registry.is_current_session(WorkerId("worker-1"), session_b) is True
    # An unknown worker is never the current session.
    assert registry.is_current_session(WorkerId("ghost"), session_b) is False


def test_heartbeat_for_unknown_worker_is_ignored() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)

    registry.record_heartbeat(WorkerId("ghost"), _T0)

    assert registry.list_workers() == []


# --- drain state (FR-WRK-5) ------------------------------------------------


def test_set_draining_makes_worker_report_draining() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))

    registry.set_draining(WorkerId("worker-1"), True)

    assert registry.list_workers()[0].status is WorkerStatus.DRAINING


def test_clear_draining_returns_worker_to_online() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))
    registry.set_draining(WorkerId("worker-1"), True)

    registry.set_draining(WorkerId("worker-1"), False)

    assert registry.list_workers()[0].status is WorkerStatus.ONLINE


def test_set_draining_unknown_worker_reports_not_found() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)

    found = registry.set_draining(WorkerId("ghost"), True)

    assert found is False
    assert registry.list_workers() == []


def test_set_draining_known_worker_reports_found() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))

    assert registry.set_draining(WorkerId("worker-1"), True) is True


def test_drain_survives_reregistration() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    first = registry.register(make_worker(at=_T0))
    registry.set_draining(WorkerId("worker-1"), True)
    registry.mark_disconnected(WorkerId("worker-1"), first)

    # The Go agent reconnects automatically; the operator's drain intent must
    # not evaporate on the re-registration.
    registry.register(make_worker(at=_T0))

    assert registry.list_workers()[0].status is WorkerStatus.DRAINING
    assert registry.candidates_for_placement() == []


def test_cleared_drain_does_not_resurrect_on_reregistration() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    first = registry.register(make_worker(at=_T0))
    registry.set_draining(WorkerId("worker-1"), True)
    registry.set_draining(WorkerId("worker-1"), False)
    registry.mark_disconnected(WorkerId("worker-1"), first)

    registry.register(make_worker(at=_T0))

    assert registry.list_workers()[0].status is WorkerStatus.ONLINE


def test_draining_survives_heartbeat() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))
    registry.set_draining(WorkerId("worker-1"), True)

    registry.record_heartbeat(WorkerId("worker-1"), _T0 + dt.timedelta(seconds=5))

    assert registry.list_workers()[0].status is WorkerStatus.DRAINING


# --- assignment tracking (load) --------------------------------------------


def test_new_worker_starts_with_zero_assigned() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))

    assert registry.list_workers()[0].assigned_count == 0


def test_increment_and_decrement_assignment() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))

    # The reservation -> confirm lifecycle (#778): reserve then increment commits.
    registry.reserve(WorkerId("worker-1"), "server-a", 0)
    registry.increment_assignment(WorkerId("worker-1"), "server-a")
    registry.reserve(WorkerId("worker-1"), "server-b", 0)
    registry.increment_assignment(WorkerId("worker-1"), "server-b")
    registry.decrement_assignment(WorkerId("worker-1"))

    assert registry.list_workers()[0].assigned_count == 1


def test_reregistration_resets_assignment_count() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    first = registry.register(make_worker(at=_T0))
    registry.reserve(WorkerId("worker-1"), "server-a", 0)
    registry.increment_assignment(WorkerId("worker-1"), "server-a")
    registry.mark_disconnected(WorkerId("worker-1"), first)

    registry.register(make_worker(at=_T0))

    assert registry.list_workers()[0].assigned_count == 0


# --- placement reservations (#778) ------------------------------------------


def test_reservation_counts_toward_load_before_commit() -> None:
    # A reservation made at placement time counts as load immediately, so a
    # concurrent placement sees the slot taken (#778).
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0, max_servers=4))

    registry.reserve(WorkerId("worker-1"), "server-a", 512)

    assert registry.candidates_for_placement()[0].load == 1
    assert registry.list_workers()[0].assigned_count == 1


def test_reserved_memory_is_summed_for_the_memory_gate() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))

    registry.reserve(WorkerId("worker-1"), "server-a", 512)
    registry.reserve(WorkerId("worker-1"), "server-b", 1024)

    assert registry.reserved_memory_mb(WorkerId("worker-1")) == 1536


def test_release_reservation_frees_the_slot() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))
    registry.reserve(WorkerId("worker-1"), "server-a", 256)

    registry.release_reservation(WorkerId("worker-1"), "server-a")

    assert registry.list_workers()[0].assigned_count == 0
    assert registry.reserved_memory_mb(WorkerId("worker-1")) == 0


def test_increment_confirms_a_reservation_without_changing_load() -> None:
    # Confirming a reservation moves it from reserved to committed; load (the sum)
    # is unchanged because the reservation already counted it (#778).
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))
    registry.reserve(WorkerId("worker-1"), "server-a", 0)

    registry.increment_assignment(WorkerId("worker-1"), "server-a")

    assert registry.list_workers()[0].assigned_count == 1
    # The slot is now committed, not reserved.
    assert registry.reserved_memory_mb(WorkerId("worker-1")) == 0


def test_increment_without_reservation_is_a_no_op() -> None:
    # A reconnect rebuild (set_assignment) that already counted the committed row
    # drops the reservation; the now-stale confirm must NOT double-count (#778).
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))

    registry.increment_assignment(WorkerId("worker-1"), "server-a")

    assert registry.list_workers()[0].assigned_count == 0


def test_rebuild_drops_committed_reservation_so_later_confirm_no_ops() -> None:
    # The re-register double-count variant (#778): a reservation made, then the
    # worker re-registers and the rebuild tally already includes the committed row;
    # the pending confirm must become a no-op, not add a second count.
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))
    registry.reserve(WorkerId("worker-1"), "server-a", 0)

    # Re-register (resets committed count) then rebuild from the tally, which now
    # already includes server-a (its commit landed before the rebuild ran).
    registry.register(make_worker(at=_T0))
    registry.set_assignment(WorkerId("worker-1"), {"server-a"})
    # The deferred confirm now arrives; it must NOT add a second count.
    registry.increment_assignment(WorkerId("worker-1"), "server-a")

    assert registry.list_workers()[0].assigned_count == 1


def test_rebuild_keeps_uncommitted_reservation_pending() -> None:
    # A reservation whose commit is not yet in the tally survives the rebuild so its
    # later confirm still counts (no undercount) (#778).
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))
    registry.reserve(WorkerId("worker-1"), "server-a", 0)

    registry.register(make_worker(at=_T0))
    # The tally does NOT yet include server-a (its commit has not landed).
    registry.set_assignment(WorkerId("worker-1"), set())
    # Reservation still counts as load while pending.
    assert registry.list_workers()[0].assigned_count == 1
    # When the commit finally lands, the confirm counts it as committed.
    registry.increment_assignment(WorkerId("worker-1"), "server-a")
    assert registry.list_workers()[0].assigned_count == 1


# --- per-id lookup (#322) --------------------------------------------------


def test_get_returns_registered_worker_snapshot() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))

    snapshot = registry.get(WorkerId("worker-1"))

    assert snapshot is not None
    assert snapshot.id == WorkerId("worker-1")
    assert snapshot.status is WorkerStatus.ONLINE


def test_get_returns_none_for_unknown_worker() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)

    assert registry.get(WorkerId("ghost")) is None


def test_get_reflects_disconnected_worker() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    session = registry.register(make_worker(at=_T0))

    registry.mark_disconnected(WorkerId("worker-1"), session)

    snapshot = registry.get(WorkerId("worker-1"))
    assert snapshot is not None
    assert snapshot.status is WorkerStatus.OFFLINE


# --- placement candidates ---------------------------------------------------


def test_candidates_include_online_worker_with_load() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0, max_servers=4))
    registry.reserve(WorkerId("worker-1"), "server-a", 0)
    registry.increment_assignment(WorkerId("worker-1"), "server-a")

    candidates = registry.candidates_for_placement()

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.worker_id == WorkerId("worker-1")
    assert candidate.drivers == frozenset({DriverKind.HOST_PROCESS})
    assert candidate.capacity == 4
    assert candidate.load == 1


def test_candidates_exclude_draining_worker() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))
    registry.set_draining(WorkerId("worker-1"), True)

    assert registry.candidates_for_placement() == []


def test_candidates_exclude_offline_worker() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)
    session = registry.register(make_worker(at=_T0))
    registry.mark_disconnected(WorkerId("worker-1"), session)

    assert registry.candidates_for_placement() == []


def test_held_generation_reflects_reported_servers() -> None:
    # The registry records the held working sets a Worker reports on Register with
    # the generation each is at (issue #763) and answers the generation per id.
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0), held_servers={"server-a": 5, "server-b": 0})

    assert registry.held_generation(WorkerId("worker-1"), "server-a") == 5
    assert registry.held_generation(WorkerId("worker-1"), "server-b") == 0
    assert registry.held_generation(WorkerId("worker-1"), "server-c") is None


def test_held_generation_none_for_unknown_worker() -> None:
    clock = FakeClock(_T0)
    registry = _registry(clock)

    assert registry.held_generation(WorkerId("ghost"), "server-a") is None


def test_register_replaces_held_working_set() -> None:
    # A re-registration REPLACES the held map (the control plane keeps no
    # cross-stream session state): a reconnect whose scratch was wiped/GC'd reports
    # fewer ids, so a stale "held" claim never survives (issue #763).
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0), held_servers={"server-a": 5})
    registry.register(make_worker(at=_T0), held_servers={})

    assert registry.held_generation(WorkerId("worker-1"), "server-a") is None


def test_register_without_held_servers_holds_nothing() -> None:
    # The default (an older Worker that does not report) holds nothing, so the
    # lifecycle layer hydrates as before (issue #763).
    clock = FakeClock(_T0)
    registry = _registry(clock)
    registry.register(make_worker(at=_T0))

    assert registry.held_generation(WorkerId("worker-1"), "server-a") is None
