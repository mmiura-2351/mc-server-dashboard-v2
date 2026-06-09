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

**Resource-aware placement (#710).** Beyond the count cap, placement now
considers the per-server resources servers *declare* (commit-based accounting,
not observed usage) against what a Worker *advertises*. The two dimensions are
deliberately asymmetric, mirroring how they are enforced:

- **Memory gates.** ``memory_limit_mb`` is an OOM-enforced hard ceiling, so a
  host that cannot fit the committed memory of its servers will kill processes.
  A candidate is therefore EXCLUDED when its committed memory plus the new
  request would exceed advertised memory minus a host reserve
  (:data:`MEMORY_RESERVE_MB`-floored fraction). This is advisory/best-effort:
  it avoids GROSS oversubscription, it is not strict admission control.
- **CPU ranks.** ``cpu_millis`` is a soft relative share that oversubscribes
  fine, so CPU never excludes a candidate; instead committed CPU is a tie-break
  preference among the memory-eligible candidates (prefer the host carrying less
  declared CPU).

**Unset resources are unaccounted.** A server (committed or requested) without a
declared ``memory_limit_mb``/``cpu_millis`` contributes ``0`` to the relevant
sum: its real footprint is unknown, the worker driver later picks a default, and
inventing a placeholder would either spuriously exclude hosts (too high) or be a
no-op (zero) — so zero is the honest, documented choice. A request whose memory
is unset is likewise not memory-gated (there is nothing to fit). Hosts that
advertise no memory capacity (``0``) are not memory-gated either (no capacity to
reason about), preserving the count-only behavior for workers that do not
advertise resources.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.fleet.domain.value_objects import DriverKind, WorkerId

# Memory withheld from each host's advertised total for the OS, the worker agent,
# and per-process JVM off-heap/native overhead that the declared ceilings do not
# capture. The reserve is the LARGER of a flat floor and a fraction of advertised
# memory, so it scales: a small host keeps a meaningful absolute margin while a
# large host keeps proportionally more headroom. Deliberately generous because
# placement is advisory — under-committing a host is cheaper than an OOM kill.
MEMORY_RESERVE_MB = 1024
MEMORY_RESERVE_FRACTION = 0.1


def _reserve_mb(memory_capacity_mb: int) -> int:
    """Host memory withheld from placement: max(flat floor, fraction of total)."""

    return max(MEMORY_RESERVE_MB, int(memory_capacity_mb * MEMORY_RESERVE_FRACTION))


@dataclass(frozen=True)
class PlacementCandidate:
    """A placement-eligible Worker projected to just what the filter needs.

    ``capacity`` is the advertised ``max_servers`` (``0`` = no cap); ``load`` is
    the current assigned-server count. ``memory_capacity_mb`` /
    ``cpu_capacity_millis`` are the Worker's advertised host resources (``0`` =
    not advertised); ``committed_memory_mb`` / ``committed_cpu_millis`` are the
    summed declared resources of the servers already assigned to it (commit-based
    accounting, supplied by the caller at the adapter/application boundary).
    """

    worker_id: WorkerId
    drivers: frozenset[DriverKind]
    capacity: int
    load: int
    memory_capacity_mb: int = 0
    cpu_capacity_millis: int = 0
    committed_memory_mb: int = 0
    committed_cpu_millis: int = 0

    def can_host(
        self,
        *,
        required_driver: DriverKind,
        needed: int,
        needed_memory_mb: int | None = None,
    ) -> bool:
        if needed <= 0:
            raise ValueError("needed must be positive")
        if required_driver not in self.drivers:
            return False
        if self.capacity != 0 and self.load + needed > self.capacity:
            return False
        return self._fits_memory(needed_memory_mb)

    def _fits_memory(self, needed_memory_mb: int | None) -> bool:
        # Memory is the hard gate (OOM-enforced). Skip it when the request does
        # not declare memory (nothing to fit) or the host advertises none (no
        # capacity to reason about) — both fall back to the count-only filter.
        if not needed_memory_mb or self.memory_capacity_mb == 0:
            return True
        usable_mb = self.memory_capacity_mb - _reserve_mb(self.memory_capacity_mb)
        return self.committed_memory_mb + needed_memory_mb <= usable_mb


@dataclass(frozen=True)
class NoEligibleWorker:
    """Typed outcome: no candidate satisfies the driver + capacity filter."""


def place(
    candidates: list[PlacementCandidate],
    *,
    required_driver: DriverKind,
    needed: int = 1,
    needed_memory_mb: int | None = None,
) -> WorkerId | NoEligibleWorker:
    """Choose a Worker for ``needed`` server unit(s) requiring ``required_driver``.

    Filters to candidates that offer the driver, have free count capacity, and
    can fit ``needed_memory_mb`` (the memory gate, #710). Among those, picks the
    least-loaded by assigned count, then the one carrying the least committed CPU
    (the soft CPU rank), then the lexicographically smallest worker id so the
    choice is deterministic. Returns :class:`NoEligibleWorker` when none qualify.
    """

    eligible = [
        candidate
        for candidate in candidates
        if candidate.can_host(
            required_driver=required_driver,
            needed=needed,
            needed_memory_mb=needed_memory_mb,
        )
    ]
    if not eligible:
        return NoEligibleWorker()
    chosen = min(
        eligible,
        key=lambda c: (c.load, c.committed_cpu_millis, c.worker_id.value),
    )
    return chosen.worker_id
