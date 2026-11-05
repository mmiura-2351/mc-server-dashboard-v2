"""Value objects for the fleet context (ARCHITECTURE.md Section 5.1).

Pure, framework-free types describing a Worker's identity and advertised
capabilities (FR-WRK-1, FR-WRK-3). The wire types live in ``proto/``; these are
the domain's own shapes, mapped from the wire at the gRPC edge so the domain
never imports the generated stubs.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass

from mc_server_dashboard_api.fleet.domain.errors import InvalidWorkerIdError


class DriverKind(enum.Enum):
    """An execution backend a Worker can offer (FR-EXE-2)."""

    CONTAINER = "container"


@dataclass(frozen=True)
class WorkerId:
    """A Worker's stable identifier (CONFIGURATION.md Section 6.1 worker.id)."""

    value: str

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise InvalidWorkerIdError("worker id must be a non-empty string")

    @classmethod
    def parse(cls, raw: str) -> WorkerId:
        """Parse an externally supplied id into its canonical UUID form.

        Every stored reference to a Worker (``assigned_worker_id`` is a uuid column)
        reads back lowercase-hyphenated, so an id arriving from outside — the
        Register message, a REST path — is canonicalised once at that edge; any
        spelling of the same UUID then names the same Worker (issue #3542).
        """

        try:
            return cls(str(uuid.UUID(raw)))
        except ValueError as exc:
            raise InvalidWorkerIdError("worker id must be a UUID") from exc


@dataclass(frozen=True)
class HostResources:
    """Coarse host resources a Worker advertises for placement (FR-WRK-3)."""

    cpu_cores: int = 0
    memory_bytes: int = 0


@dataclass(frozen=True)
class WorkerCapabilities:
    """What a Worker advertises at registration (FR-WRK-1).

    ``drivers`` is the set of execution backends offered; ``max_servers`` is a
    free-capacity hint where ``0`` means no advertised cap; ``resources`` is the
    coarse host description.
    """

    drivers: frozenset[DriverKind]
    max_servers: int = 0
    resources: HostResources = HostResources()
