"""Value objects for the servers context: ids, names, and the state/backend enums.

Pure, immutable, standard-library only. ``ServerId`` wraps the application-
generated UUID primary key (DATABASE.md Section 2). ``CommunityId`` /
``WorkerId`` are *foreign* references held by id value only: the servers domain
never imports the community or fleet domains (the FKs live at the persistence
layer, DATABASE.md Section 7). The enums mirror DATABASE.md's CHECK-constrained
``server`` columns exactly.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.errors import InvalidServerNameError

# The reserved ``config`` key under which a server's resolved JAR content address
# (its SHA-256, the storage ``JarKey``) is recorded (issue #118). DATABASE.md
# Section 7 has no dedicated JAR column, so the resolved reference lives in the
# ``config`` JSONB blob, which the doc designates for server configuration. Start
# writes it after ensuring the JAR is pooled; the hydrate data plane reads it to
# inject ``server.jar`` into the working-set tar.
JAR_KEY_CONFIG_FIELD = "resolved_jar_sha256"


@dataclass(frozen=True)
class ServerId:
    """The identity of a :class:`~.entities.Server` (a UUID primary key)."""

    value: uuid.UUID

    @classmethod
    def new(cls) -> ServerId:
        """Generate a fresh, random server id."""

        return cls(uuid.uuid4())


@dataclass(frozen=True)
class CommunityId:
    """A foreign reference to the owning community, by id value only.

    The servers domain never imports the community domain (DATABASE.md Section 6):
    the community is referenced here purely as a UUID. The persistence-layer FK to
    ``community.id`` enforces referential integrity and the delete cascade.
    """

    value: uuid.UUID


@dataclass(frozen=True)
class WorkerId:
    """A foreign reference to the assigned Worker, by id value only (nullable).

    Set on placement, cleared (``ON DELETE SET NULL``) if the Worker disconnects
    (FR-WRK-4). Held here as a plain UUID; the servers domain never imports the
    fleet domain.
    """

    value: uuid.UUID


@dataclass(frozen=True)
class ServerName:
    """A server's name, unique within its community (DATABASE.md Section 7).

    Surrounding whitespace is trimmed and a blank name is rejected; the schema's
    ``UNIQUE(community_id, name)`` handles uniqueness exactly.
    """

    value: str

    def __init__(self, value: str) -> None:
        trimmed = value.strip()
        if not trimmed:
            raise InvalidServerNameError("server name must not be blank")
        object.__setattr__(self, "value", trimmed)


class ServerType(enum.Enum):
    """Supported Minecraft server distributions (DATABASE.md Section 7 CHECK enum).

    The persisted set; new types extend both this enum and the schema CHECK
    together. Not every member is *resolvable* by the version catalog: ``forge``
    (needs a worker-side installer step) and ``spigot`` (no official distribution
    API, BuildTools-only) are accepted by the schema but rejected at create-time by
    version-validation — spigot with a recommendation to use Paper, a Spigot fork.
    """

    VANILLA = "vanilla"
    PAPER = "paper"
    FABRIC = "fabric"
    FORGE = "forge"
    SPIGOT = "spigot"


class ExecutionBackend(enum.Enum):
    """Where a server runs (DATABASE.md Section 7 CHECK enum, FR-EXE-2).

    These values deliberately mirror the fleet domain's ``DriverKind`` (the
    Worker-advertised driver) but are defined here independently: the servers
    domain must not import the fleet domain (import-linter cross-context contract;
    ARCHITECTURE.md Section 2.1). The *values* differ on purpose — the persisted
    ``execution_backend`` column uses the underscore spelling DATABASE.md's CHECK
    enum mandates (``host_process``), whereas the fleet wire-facing ``DriverKind``
    uses the hyphen spelling (``host-process``). Mapping between the two, when
    placement lands, is an adapter concern, not a shared domain type.

    ``HOST_PROCESS`` is retained but no longer a shipped backend: the Worker's
    host-process driver was removed in issue #781. The value is kept here (and in
    the DATABASE.md CHECK enum) so historical rows stay readable and the wire
    round-trip is preserved; no Worker advertises it, so a ``host_process`` server
    is unplaceable. New servers are created with ``CONTAINER`` only.
    """

    HOST_PROCESS = "host_process"
    CONTAINER = "container"


class DesiredState(enum.Enum):
    """What the operator wants (DATABASE.md Section 7 CHECK enum, FR-SRV-3).

    Source of truth for *intent*, mutated only by API operations. A freshly
    created server defaults to ``STOPPED``.
    """

    RUNNING = "running"
    STOPPED = "stopped"


class ObservedState(enum.Enum):
    """Last state reported by the Worker (DATABASE.md Section 7 CHECK enum).

    A cache of reality written only by the control-plane event handler from
    Worker reports (FR-SRV-4). The reportable values mirror the control-plane
    ``ServerState`` enum (CONTROL_PLANE.md Section 6); ``UNKNOWN`` is
    API-inferred (set when the owning Worker disconnects) and never reported by a
    Worker.
    """

    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    RESTARTING = "restarting"
    CRASHED = "crashed"
    UNKNOWN = "unknown"
