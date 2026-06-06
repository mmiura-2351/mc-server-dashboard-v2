"""Periodic ``login_attempt`` pruning, independent of login events (Section 3).

The on-success prune in the login use case keeps the table bounded for accounts
that eventually log in. A *failures-only* attack against an account that never
succeeds never triggers that path, so the append-only ``login_attempt`` table
would grow without bound. This use case is the time-based prune that closes that
gap: the edge runs :meth:`PruneLoginAttempts.tick` on a fixed cadence as a
lifespan task (the established loop pattern), deleting rows older than the longest
sliding window — the same bound the on-success prune uses
(:func:`~..domain.brute_force.prune_horizon`), so the two triggers stay
consistent. The registration per-IP rows (issue #362) share this table and are
counted over their own window, so the registration config is folded into the
horizon to keep those rows alive for their full window.
"""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.identity.domain.brute_force import (
    BruteForceConfig,
    prune_horizon,
)
from mc_server_dashboard_api.identity.domain.clock import Clock
from mc_server_dashboard_api.identity.domain.login_attempt_store import (
    LoginAttemptStore,
)
from mc_server_dashboard_api.identity.domain.registration import RegistrationConfig


@dataclass(frozen=True)
class PruneLoginAttempts:
    """Delete ``login_attempt`` rows past the longest sliding window, on demand."""

    attempts: LoginAttemptStore
    brute_force: BruteForceConfig
    clock: Clock
    registration: RegistrationConfig | None = None

    async def tick(self) -> None:
        """Prune attempts older than ``now - prune_horizon`` (one cadence tick)."""

        now = self.clock.now()
        await self.attempts.prune_attempts(
            older_than=now - prune_horizon(self.brute_force, self.registration)
        )
