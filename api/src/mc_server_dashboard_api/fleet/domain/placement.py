"""Greedy worker placement (FR-WRK-3).

A pure function over candidate snapshots, isolated so a richer scheduler can
replace it without touching callers (the lifecycle epic calls :func:`place`).
The filter keeps only candidates that advertise the required
:class:`DriverKind` and have free capacity; among those it picks the
least-loaded, breaking ties by lexicographic worker id so the choice is
deterministic. An empty result is a typed :class:`NoEligibleWorker` value, not
an exception used for flow control.

At M1 ``load`` is the count of servers currently assigned to a Worker (the
registry tracks it; epic #7 supplies real assignments). ``capacity`` is the
Worker's advertised ``max_servers``, where ``0`` means "no advertised cap".
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.fleet.domain.value_objects import DriverKind, WorkerId


@dataclass(frozen=True)
class PlacementCandidate:
    """A placement-eligible Worker projected to just what the filter needs.

    ``capacity`` is the advertised ``max_servers`` (``0`` = no cap); ``load`` is
    the current assigned-server count.
    """

    worker_id: WorkerId
    drivers: frozenset[DriverKind]
    capacity: int
    load: int

    def can_host(self, *, required_driver: DriverKind, needed: int) -> bool:
        if needed <= 0:
            raise ValueError("needed must be positive")
        if required_driver not in self.drivers:
            return False
        if self.capacity == 0:
            return True
        return self.load + needed <= self.capacity


@dataclass(frozen=True)
class NoEligibleWorker:
    """Typed outcome: no candidate satisfies the driver + capacity filter."""


def place(
    candidates: list[PlacementCandidate],
    *,
    required_driver: DriverKind,
    needed: int = 1,
) -> WorkerId | NoEligibleWorker:
    """Choose a Worker for ``needed`` server unit(s) requiring ``required_driver``.

    Returns the least-loaded eligible :class:`WorkerId`, ties broken by
    lexicographic worker id, or :class:`NoEligibleWorker` when none qualifies.
    """

    eligible = [
        candidate
        for candidate in candidates
        if candidate.can_host(required_driver=required_driver, needed=needed)
    ]
    if not eligible:
        return NoEligibleWorker()
    chosen = min(eligible, key=lambda c: (c.load, c.worker_id.value))
    return chosen.worker_id
