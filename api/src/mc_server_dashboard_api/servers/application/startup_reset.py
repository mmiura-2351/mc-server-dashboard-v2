"""Startup observed-state reset (issue #224).

The observed-state column is a cache of worker reports; the API marks it
``unknown`` only when it observes a worker's heartbeat lapse (FR-WRK-4). A
full-stack restart kills the API before that 30s timeout elapses, so nothing
invalidates the cache: a row can persist as ``(desired=running,
observed=running, assigned)`` with no live instance, and the reconciler treats
``observed=running`` as converged — phantom running forever.

:class:`ResetUnverifiableObservedStates` closes the gap. Run once on API startup
before the reconciler loop begins, it marks every assigned server whose observed
state is non-terminal as ``observed=unknown`` (keeping the assignment), so the
reconciler converges truthfully within one grace+tick: a live instance is
re-confirmed via redispatch, a vanished one gets a genuine start.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.clock import Clock
from mc_server_dashboard_api.servers.domain.unit_of_work import UnitOfWork

_LOG = logging.getLogger(__name__)


@dataclass
class ResetUnverifiableObservedStates:
    """Invalidate the stale observed-state cache for assigned, in-flight servers."""

    uow: UnitOfWork
    clock: Clock

    async def __call__(self) -> int:
        async with self.uow as uow:
            count = await uow.servers.reset_unverifiable_observed_states(
                self.clock.now()
            )
            await uow.commit()
        if count:
            _LOG.info(
                "reset unverifiable observed state on startup",
                extra={"count": count},
            )
        return count
