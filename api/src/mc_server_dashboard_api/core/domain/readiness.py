"""Readiness domain: the per-component readiness report (issue #282).

``/readyz`` answers "is this process ready to serve" — distinct from ``/healthz``,
which stays the cheap liveness probe (DB ping only, unchanged). Readiness is the
AND of the critical components: the database is reachable and, when the control
plane is enabled, its gRPC server has started. Each component's boolean is
reported so an operator sees *which* one is not ready.

This is pure domain: a frozen report value plus the two component Ports the use
case probes. The control-plane component is a started/enabled flag, not a Port
method that does work, so it is passed in as a value by the wiring layer.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass(frozen=True)
class ComponentStatus:
    """One readiness component: its name and whether it is ready."""

    name: str
    ready: bool


@dataclass(frozen=True)
class ReadinessReport:
    """Outcome of a readiness check.

    ``ready`` is the overall verdict (the AND of every critical component);
    ``components`` carries each component's detail so the endpoint can render a
    per-component status object and the operator can see which one failed.
    """

    ready: bool
    components: tuple[ComponentStatus, ...]


class ControlPlaneReadiness(abc.ABC):
    """Port: whether the control-plane component is ready.

    When the control plane is disabled this reports ready (there is nothing to
    start); when enabled it reports whether the gRPC server has started.
    """

    @abc.abstractmethod
    def is_ready(self) -> bool:
        """Return whether the control plane is ready (or not enabled)."""
