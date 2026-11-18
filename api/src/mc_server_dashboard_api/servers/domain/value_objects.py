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

# The source fingerprint recorded alongside the pool key for update detection
# (issue #1676). On each start, the catalog is resolved and the new fingerprint
# compared to this stored value; a mismatch triggers a re-download.
JAR_SOURCE_CONFIG_FIELD = "resolved_jar_source"


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

    Set on placement, cleared when a stop releases the server or a failed start is
    undone; a Worker disconnect keeps it (FR-WRK-4). Held here as a plain UUID; the
    servers domain never imports the fleet domain.
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
    together. ``forge`` needs a worker-side installer step but is catalogued and
    validates normally (issue #307).
    """

    VANILLA = "vanilla"
    PAPER = "paper"
    FABRIC = "fabric"
    FORGE = "forge"


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
    Worker reports (FR-SRV-4). Every value mirrors the control-plane
    ``ServerState`` enum (CONTROL_PLANE.md Section 6), ``UNKNOWN`` included as of
    issue #2474: it has two producers. The API infers it when the owning Worker's
    session drops, and a Worker asserts it when it cannot currently confirm an
    instance's fate (a failed-stop orphan under an unreachable daemon) — reporting
    ``STOPPED`` or ``RUNNING`` there would be a guess recorded as fact.
    """

    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    RESTARTING = "restarting"
    CRASHED = "crashed"
    UNKNOWN = "unknown"


class CrashReason(enum.Enum):
    """Why the Worker says a server crashed (DATABASE.md Section 7 CHECK enum).

    Mirrors the control-plane ``CrashReason`` (CONTROL_PLANE.md Section 6), whose
    every classified value is a crash of the supervised Forge install. An
    unclassified crash has no reason (``None``), not a value of its own. Stored
    with the observed state as the last crash's reason while the server reads
    ``crashed`` (issue #3281).
    """

    FORGE_INSTALL_FAILED = "forge_install_failed"
    FORGE_INSTALL_OUT_OF_MEMORY = "forge_install_out_of_memory"
    FORGE_INSTALL_JAVA_INCOMPATIBLE = "forge_install_java_incompatible"


# The crash reasons the reconciler does not restart (issue #3281): the ones the
# Worker reports at once instead of retrying the install, because the next
# attempt fails the same way (CONTROL_PLANE.md Section 6). The same memory limit
# exhausts the installer again, and the same Java runtime refuses the same
# classes. ``FORGE_INSTALL_FAILED`` is not among them: the Worker retries that
# failure itself (a failed download is the usual cause) and gives the reason to
# every install crash it cannot attribute, so a restart may still succeed.
NOT_RESTARTED_CRASH_REASONS = frozenset(
    {
        CrashReason.FORGE_INSTALL_OUT_OF_MEMORY,
        CrashReason.FORGE_INSTALL_JAVA_INCOMPATIBLE,
    }
)


# The bound on a Worker report's free-text ``detail`` (issue #3281), in UTF-8
# bytes, marker included. The last crash's detail is stored with the observed
# state; a Worker's detail is a sentence or an error chain, so 4 KiB keeps all of
# it while the column cannot grow with whatever a Worker sends.
MAX_STATUS_DETAIL_BYTES = 4 * 1024
STATUS_DETAIL_TRUNCATION_MARKER = " … [truncated]"


def bound_status_detail(detail: str) -> str:
    """Return ``detail`` as it is stored and relayed (issue #3281).

    NUL is dropped: a PostgreSQL ``text`` column refuses it, and the write that
    stores the detail also records the observed state. A detail longer than
    :data:`MAX_STATUS_DETAIL_BYTES` is cut on a character boundary and ends with
    :data:`STATUS_DETAIL_TRUNCATION_MARKER`.
    """

    detail = detail.replace("\x00", "")
    encoded = detail.encode()
    if len(encoded) <= MAX_STATUS_DETAIL_BYTES:
        return detail
    budget = MAX_STATUS_DETAIL_BYTES - len(STATUS_DETAIL_TRUNCATION_MARKER.encode())
    # A cut inside a multi-byte character leaves an incomplete tail, which the
    # decode drops.
    return encoded[:budget].decode(errors="ignore") + STATUS_DETAIL_TRUNCATION_MARKER
