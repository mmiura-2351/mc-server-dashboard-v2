"""CheckReadiness use case: probe the critical components (issue #282)."""

from __future__ import annotations

from dataclasses import dataclass

from mc_server_dashboard_api.core.domain.health import DatabasePing
from mc_server_dashboard_api.core.domain.readiness import (
    ComponentStatus,
    ControlPlaneReadiness,
    ReadinessReport,
)


@dataclass(frozen=True)
class CheckReadiness:
    """Probe the critical components and produce a :class:`ReadinessReport`.

    The process is ready iff every critical component is ready: the database is
    reachable (reusing the ``/healthz`` ping seam) and the control plane is ready
    (started when enabled, trivially ready when disabled). Storage is intentionally
    not a component here: the ``Storage`` Port exposes no cheap stat operation and
    issue #282 forbids inventing one for this check, so it is omitted.
    """

    database: DatabasePing
    control_plane: ControlPlaneReadiness

    async def __call__(self) -> ReadinessReport:
        db_ready = await self.database.is_reachable()
        cp_ready = self.control_plane.is_ready()
        components = (
            ComponentStatus(name="database", ready=db_ready),
            ComponentStatus(name="control_plane", ready=cp_ready),
        )
        return ReadinessReport(
            ready=all(component.ready for component in components),
            components=components,
        )
