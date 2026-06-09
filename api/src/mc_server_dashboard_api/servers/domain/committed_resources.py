"""Commit-based per-worker resource accounting for placement (#710).

Sums the per-server resources servers *declare* (``memory_limit_mb`` /
``cpu_millis`` on the config blob) grouped by their assigned Worker. This is the
"committed" side of resource-aware placement: what a host has promised to the
servers already on it, NOT what those servers are observed to consume. Placement
(``fleet.domain.placement``) compares this against the Worker's advertised host
capacity to avoid grossly oversubscribing memory and to rank by CPU.

Pure and standard-library only — the summing is deterministic and unit-testable
in isolation (TESTING.md Section 4). The accounting lives at the
application/adapter boundary (the caller passes the loaded servers in); the
pure ``place`` function stays free of I/O.

**Unset resources contribute 0** (documented in ``placement``): a server with no
declared limit/allocation has an unknown footprint, so it adds nothing to the
sum rather than a guessed placeholder. Reuses the shipped config validators so
the read of each value matches the data model exactly.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.cpu_allocation import (
    cpu_allocation_from_config,
)
from mc_server_dashboard_api.servers.domain.entities import Server
from mc_server_dashboard_api.servers.domain.memory_limit import memory_limit_from_config
from mc_server_dashboard_api.servers.domain.value_objects import WorkerId


@dataclass(frozen=True)
class CommittedResources:
    """The summed declared resources of the servers assigned to one Worker."""

    memory_mb: int = 0
    cpu_millis: int = 0


def committed_resources_by_worker(
    servers: list[Server],
) -> dict[WorkerId, CommittedResources]:
    """Sum declared memory/CPU per assigned Worker over ``servers``.

    Servers with no ``assigned_worker_id`` are skipped (nothing is committed to a
    host yet). An unset ``memory_limit_mb``/``cpu_millis`` contributes ``0``.
    Returns a mapping from :class:`WorkerId` to its committed totals; a Worker
    with no assigned servers is simply absent from the mapping.
    """

    memory: defaultdict[WorkerId, int] = defaultdict(int)
    cpu: defaultdict[WorkerId, int] = defaultdict(int)
    for server in servers:
        worker_id = server.assigned_worker_id
        if worker_id is None:
            continue
        memory[worker_id] += memory_limit_from_config(server.config) or 0
        cpu[worker_id] += cpu_allocation_from_config(server.config) or 0
    return {
        worker_id: CommittedResources(
            memory_mb=memory[worker_id], cpu_millis=cpu[worker_id]
        )
        for worker_id in memory
    }
