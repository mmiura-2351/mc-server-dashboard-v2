"""Control-plane readiness adapter (issue #282).

Binds the :class:`ControlPlaneReadiness` Port to the wiring layer's two facts:
whether the control plane is *enabled* (config) and whether its gRPC server has
*started* (set on app state once ``grpc_server.start()`` returns). When disabled
the component is trivially ready (nothing to start); when enabled it is ready only
once the server has started.
"""

from __future__ import annotations

from mc_server_dashboard_api.core.domain.readiness import ControlPlaneReadiness


class FlagControlPlaneReadiness(ControlPlaneReadiness):
    """Report control-plane readiness from the enabled + started flags."""

    def __init__(self, *, enabled: bool, started: bool) -> None:
        self._enabled = enabled
        self._started = started

    def is_ready(self) -> bool:
        return self._started if self._enabled else True
