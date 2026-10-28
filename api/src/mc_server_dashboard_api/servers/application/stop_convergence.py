"""Per-server exclusion over the stop convergence flows (issue #3491).

Every flow that takes a desired-stopped server's final snapshot and then releases
its Worker assignment — the original stop, ``redispatch_stop``,
``clear_stale_assignment``, and the late-snapshot-result release — runs under this
exclusion. The release's compare-and-set
(``clear_assignment_after_final_snapshot``) checks only the desired state and the
Worker, not WHICH snapshot the hold is waiting on. So without the exclusion, one
flow consuming its own failed snapshot could release the assignment while another
flow's upload over the retained scratch is still running. A start could then
re-place the server on a different Worker and hydrate the old generation. The
reconciler's grace window used to keep its flows apart from the original stop. An
operator stop of a still-assigned desired-stopped server skips that wait, so the
flows are kept apart here instead.

A server's convergence is in flight while any of these exists, not only while an
API caller is alive:

- a caller holds the claim;
- a shielded release a flow started has not finished — it outlives the caller's
  cancellation, so it keeps the server in flight on its own until it completes;
- a final-snapshot command for the server is outstanding: dispatched, with its
  result not yet consumed by the caller or by the late-result sink. The
  control-plane state already tracks these by command id for the late-result
  path, and ``outstanding`` asks it.

A flow that finds the server in flight does not queue behind it. An operator stop
gets ``LifecycleTransitionConflictError`` (409 ``transition_conflict``), and the
reconciler skips the row for the tick. A late result whose own command record is
already consumed releases only if nothing else is in flight; otherwise the flow
still in flight, or a later reconciler tick, converges the row. Every path ends:
an outstanding record is dropped when its result arrives, when the Worker's
session ends or is superseded, or when the command is dropped unsent as stale.
On a live session a result always comes, because the Worker bounds every transfer
(CONTROL_PLANE.md Section 5.2). Once nothing is in flight, the reconciler's
stale-stop arm converges a still-held row.

In-process, deliberately. The API is a single instance (REQUIREMENTS.md
NFR-SCALE-1), and the state the flows coordinate on is already in this process
anyway: the Worker sessions their commands travel on, the command records, and
the registry. Its one event loop (the gRPC servicer shares FastAPI's) makes the
check-and-claim below atomic. The DB-backed ``LifecycleLock`` is not used, for
three reasons. It waits out a budget instead of refusing at once. Holding it
across a final snapshot of up to 600 s would pin a pool connection, and would turn
every at-rest-gated operation in that window into ``server_busy``. And the
late-result release runs on a Worker's inbound stream, which must never block on
it.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass, field
from typing import Any

from mc_server_dashboard_api.servers.domain.errors import (
    LifecycleTransitionConflictError,
)
from mc_server_dashboard_api.servers.domain.value_objects import ServerId


def _nothing_outstanding(server_id: ServerId) -> bool:
    return False


@dataclass
class StopConvergenceGuard:
    """Which servers' stop convergence is in flight in this process.

    ``outstanding`` reports whether a final snapshot of a server still awaits its
    result. The application factory wires it to the control-plane state. The
    default answers no, which only fits unit tests whose control plane keeps no
    such records.
    """

    outstanding: Callable[[ServerId], bool] = _nothing_outstanding
    _claims: set[ServerId] = field(default_factory=set)
    _shielded: dict[ServerId, asyncio.Task[None]] = field(default_factory=dict)

    def in_flight(self, server_id: ServerId) -> bool:
        """Whether any flow, release or final snapshot for the server is live."""

        return (
            server_id in self._claims
            or server_id in self._shielded
            or self.outstanding(server_id)
        )

    @contextlib.contextmanager
    def claim(self, server_id: ServerId) -> Iterator[None]:
        """Hold the claim for the body, or refuse at once if anything is in flight."""

        if self.in_flight(server_id):
            raise LifecycleTransitionConflictError(str(server_id.value))
        self._claims.add(server_id)
        try:
            yield
        finally:
            self._claims.discard(server_id)

    async def shield(
        self, server_id: ServerId, work: Coroutine[Any, Any, None]
    ) -> None:
        """Run a claim holder's release shielded from the holder's cancellation.

        The server stays in flight until ``work`` itself finishes, not just until
        the holder's claim ends. A cancelled holder drops its claim at once, while
        the shielded release keeps running in its own task. Only a claim holder
        calls this, so at most one shielded release per server is ever live.
        """

        task = asyncio.ensure_future(work)
        self._shielded[server_id] = task
        task.add_done_callback(lambda _: self._shielded.pop(server_id, None))
        await asyncio.shield(task)
