"""Domain errors for the fleet context.

Raised by the pure domain on invariant violations. They carry no framework type
and are translated to transport/wire errors at the edge.
"""

from __future__ import annotations


class FleetError(Exception):
    """Base class for fleet-domain invariant violations."""


class InvalidWorkerIdError(FleetError):
    """A worker id failed its validation rules (e.g. blank)."""
