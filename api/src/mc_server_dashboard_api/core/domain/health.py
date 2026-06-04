"""Health domain: the readiness report and the database-liveness Port."""

from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass(frozen=True)
class HealthReport:
    """Outcome of a health check.

    ``ok`` is the overall verdict; ``database_reachable`` is the dependency
    detail behind it. A degraded report (``ok`` False) is a normal, reportable
    state, not an error (the endpoint reports it rather than crashing).
    """

    ok: bool
    database_reachable: bool


class DatabasePing(abc.ABC):
    """Port: a cheap liveness probe of the persistence backend."""

    @abc.abstractmethod
    async def is_reachable(self) -> bool:
        """Return whether the database answers a trivial query, never raising."""
