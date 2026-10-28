"""Per-server exclusion over the stop convergence flows (issue #3491).

Every flow that takes a desired-stopped server's final snapshot and then releases
its Worker assignment — the original stop, ``redispatch_stop``,
``clear_stale_assignment``, and the late-snapshot-result release — holds this
exclusion for the whole snapshot+release. The release's compare-and-set
(``clear_assignment_after_final_snapshot``) checks only the desired state and the
Worker, not WHICH snapshot the hold is waiting on. So without the exclusion, one
flow consuming its own failed snapshot could release the assignment while another
flow's upload over the retained scratch is still running. A start could then
re-place the server on a different Worker and hydrate the old generation. The
reconciler's grace window used to keep its flows apart from the original stop. An
operator stop of a still-assigned desired-stopped server skips that wait, so the
flows are kept apart here instead.

A flow that finds the exclusion held does not queue behind it. An operator stop
gets ``LifecycleTransitionConflictError`` (409 ``transition_conflict``), and the
reconciler skips the row for the tick. The late result of an abandoned snapshot
leaves the release to the holder, whose own snapshot outcome decides it.

In-process, deliberately. The API is a single instance (REQUIREMENTS.md
NFR-SCALE-1), and the state the flows coordinate on is already in this process
anyway: the Worker sessions their commands travel on and the registry. Its one
event loop (the gRPC servicer shares FastAPI's) makes the check-and-claim below
atomic. The DB-backed ``LifecycleLock`` is not used, for three reasons. It waits
out a budget instead of refusing at once. Holding it across a final snapshot of up
to 600 s would pin a pool connection, and would turn every at-rest-gated operation
in that window into ``server_busy``. And the late-result release runs on a
Worker's inbound stream, which must never block on it.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass, field

from mc_server_dashboard_api.servers.domain.errors import (
    LifecycleTransitionConflictError,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId


@dataclass
class StopConvergenceGuard:
    """The servers whose stop convergence is in flight in this process."""

    _active: set[ServerId] = field(default_factory=set)

    def in_flight(self, server_id: ServerId) -> bool:
        """Whether a stop convergence for ``server_id`` holds the exclusion."""

        return server_id in self._active

    @contextlib.contextmanager
    def claim(self, server_id: ServerId) -> Iterator[None]:
        """Hold the exclusion for the body, or refuse at once if it is held."""

        if server_id in self._active:
            raise LifecycleTransitionConflictError(str(server_id.value))
        self._active.add(server_id)
        try:
            yield
        finally:
            self._active.discard(server_id)
