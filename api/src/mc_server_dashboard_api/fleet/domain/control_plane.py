"""The ``ControlPlane`` Port: dispatch commands to a connected Worker.

ARCHITECTURE.md Section 5.1 places the ``ControlPlane`` on the API side: it
reaches Worker behaviour by sending an ``ApiCommand`` over the Worker's live
session stream and awaiting the correlated ``CommandResult`` (CONTROL_PLANE.md
Sections 3, 5). The interface lives in the fleet domain (the worker-facing
context that owns the stream); the gRPC servicer adapter fulfils it by routing
the command to the right outbound stream and matching the result by
``command_id``.

The command and result types here are the domain's own framework-free shapes,
not the generated wire types — the fleet domain never imports the ``mcsd`` stubs
(import-linter contract). The adapter maps these to/from the proto at the
transport edge.
"""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass

from mc_server_dashboard_api.fleet.domain.errors import FleetError
from mc_server_dashboard_api.fleet.domain.value_objects import DriverKind, WorkerId


class WorkerNotConnectedError(FleetError):
    """The target Worker has no live session to dispatch a command on.

    Raised by :meth:`ControlPlane.dispatch` when ``worker_id`` is unknown to the
    control plane (never connected, or disconnected). The caller maps this to a
    transport error; a server assigned to a gone Worker cannot be commanded
    until it reconnects (FR-WRK-4).
    """


class CommandTimedOutError(FleetError):
    """A dispatched command was not answered within the deadline.

    The Worker is connected but did not return a ``CommandResult`` for the
    ``command_id`` in time (CONTROL_PLANE.md Section 4.2). The command may or may
    not have taken effect; the caller treats it as a failure.
    """


class CommandResultCode(enum.Enum):
    """The result classification of a dispatched command.

    ``OK`` mirrors a ``CommandResult.success``; the failure codes mirror the
    wire ``CommandErrorCode`` (CONTROL_PLANE.md Section 7) so the caller can map
    a Worker-reported failure to a typed outcome without touching the stubs.
    """

    OK = "ok"
    SERVER_NOT_FOUND = "server_not_found"
    INVALID_STATE = "invalid_state"
    DRIVER_UNAVAILABLE = "driver_unavailable"
    FILE_ACCESS_DENIED = "file_access_denied"
    TRANSFER_FAILED = "transfer_failed"
    INTERNAL = "internal"
    # Sanitized start-failure categories the Worker classifies (issue #225).
    PORT_CONFLICT = "port_conflict"
    IMAGE_MISSING = "image_missing"
    # Another mutating lifecycle command is already in flight for the server, so
    # the Worker refused this one without applying it (issue #824). Distinct from
    # INVALID_STATE: the in-flight command's outcome is unknown, so the caller
    # keeps its assignment/intent and retries rather than converging a state.
    BUSY = "busy"


class FileAccessReason(enum.Enum):
    """Refines a ``FILE_ACCESS_DENIED`` result (issue #548).

    The Worker emits ``FILE_ACCESS_DENIED`` for several conditions that are NOT
    path-syntax problems; this mirrors the wire ``FileAccessReason`` so the
    caller can map each to an honest problem reason without touching the stubs.
    ``UNSPECIFIED`` is a non-file failure or a genuine path denial.
    """

    UNSPECIFIED = "unspecified"
    IS_A_DIRECTORY = "is_a_directory"
    NOT_A_DIRECTORY = "not_a_directory"
    SYMLINK_REFUSED = "symlink_refused"
    PAYLOAD_TOO_LARGE = "payload_too_large"


@dataclass(frozen=True)
class FileEntry:
    """One child of a listed directory in a running server's working set.

    The shape mirrors the API's authoritative-Storage listing (name / is_dir /
    size) so a running-server listing unifies with an at-rest one.
    """

    name: str
    is_dir: bool
    size: int


@dataclass(frozen=True)
class FileListing:
    """A directory listing returned by a ``ListFiles`` command.

    ``truncated`` is set when the directory held more entries than the Worker's
    per-listing cap and ``entries`` was clipped to that cap.
    """

    entries: tuple[FileEntry, ...] = ()
    truncated: bool = False


@dataclass(frozen=True)
class CommandResult:
    """The Worker's answer to a dispatched command.

    ``code`` is ``OK`` on success; any other value carries the Worker's failure
    classification. ``message`` is the human-readable detail (empty on success).
    ``output`` carries the console/RCON text of a ``ServerCommand``; ``file_content``
    carries the bytes read by a ``ReadFile``; ``file_listing`` carries the directory
    listing of a ``ListFiles`` (all empty/None otherwise, and mutually exclusive,
    mirroring the wire ``CommandResult`` ``result`` oneof).
    """

    code: CommandResultCode
    message: str = ""
    output: str = ""
    file_content: bytes = b""
    file_listing: FileListing | None = None
    # Refines a FILE_ACCESS_DENIED failure (issue #548); UNSPECIFIED for any
    # other code.
    file_access_reason: FileAccessReason = FileAccessReason.UNSPECIFIED

    @property
    def success(self) -> bool:
        return self.code is CommandResultCode.OK


class LaunchMode(enum.Enum):
    """How the Worker launches a server's process (CONTROL_PLANE.md Section 5).

    ``JAR`` is the historical ``java -jar <jar> nogui`` launch (vanilla / Paper /
    Fabric). ``FORGE_ARGSFILE`` runs the supervised Forge installer on first start
    then launches via the generated args file (issue #305/#306/#307). The mode is
    carried explicitly on the command, never inferred from the working-set
    contents.
    """

    JAR = "jar"
    FORGE_ARGSFILE = "forge_argsfile"


@dataclass(frozen=True)
class StartServerCommand:
    """Launch a server on the Worker (CONTROL_PLANE.md Section 5)."""

    driver: DriverKind
    jar_relpath: str
    minecraft_version: str
    launch_mode: LaunchMode = LaunchMode.JAR
    # The per-server memory ceiling in bytes (the operator-declared limit, issue
    # #706). 0 means "unset" — the Worker driver picks a default heap, preserving
    # the pre-#706 launch. The Worker derives ``-Xmx`` from this limit; it is not a
    # pre-computed JVM flag.
    memory_limit_bytes: int = 0
    # The per-server CPU allocation in millicores (the operator-declared soft share,
    # issue #723). 0 means "unset" — the Worker driver applies its default weight.
    # Carried as-is; there is no derivation step (unlike ``-Xmx``).
    cpu_millis: int = 0


@dataclass(frozen=True)
class StopServerCommand:
    """Stop a running server; ``force`` skips the graceful path."""

    force: bool = False


@dataclass(frozen=True)
class RestartServerCommand:
    """Stop then start the server in place."""


@dataclass(frozen=True)
class ServerCommandCommand:
    """Forward an RCON/console line; the output rides the result (FR-SRV-5)."""

    line: str


@dataclass(frozen=True)
class HydrateCommand:
    """Trigger the Worker to pull its working set from the data plane (FR-DATA-4).

    Carries the API-terminated HTTP transfer URL and a short-lived token; the
    bulk bytes never traverse the control-plane stream (CONTROL_PLANE.md Section 5).
    """

    transfer_url: str
    transfer_token: str


@dataclass(frozen=True)
class SnapshotCommand:
    """Trigger the Worker to push its working set to the data plane (FR-DATA-4)."""

    transfer_url: str
    transfer_token: str


@dataclass(frozen=True)
class ReadFileCommand:
    """Read ``path`` from a running server's live working set (Section 6.9, 7.2).

    The bytes ride the result's ``file_content``; the small, latency-sensitive
    read stays on the control-plane stream (ARCHITECTURE.md Section 7.2), not the
    bulk data plane. Path-traversal protection is enforced on the Worker side.
    """

    path: str


@dataclass(frozen=True)
class EditFileCommand:
    """Write ``content`` to ``path`` in a running server's live working set."""

    path: str
    content: bytes


@dataclass(frozen=True)
class ListFilesCommand:
    """List a directory in a running server's live working set (Section 6.9, 7.2).

    The listing rides the result's ``file_listing``; ``path == "."`` lists the
    working-set root. Path-traversal protection is enforced on the Worker side
    (FR-FILE-4), and the listing is read-only.
    """

    path: str


# The union of commands the lifecycle layer dispatches. File access (ReadFile /
# EditFile / ListFiles) rides this stream for running servers (ARCHITECTURE.md
# Section 7.2).
Command = (
    StartServerCommand
    | StopServerCommand
    | RestartServerCommand
    | ServerCommandCommand
    | HydrateCommand
    | SnapshotCommand
    | ReadFileCommand
    | EditFileCommand
    | ListFilesCommand
)


class ControlPlane(abc.ABC):
    """Port: send an ``ApiCommand`` to a Worker and await its ``CommandResult``."""

    @abc.abstractmethod
    async def dispatch(
        self,
        *,
        worker_id: WorkerId,
        server_id: str,
        command: Command,
        timeout_override: float | None = None,
        snapshot_is_final: bool = False,
    ) -> CommandResult:
        """Send ``command`` for ``server_id`` to ``worker_id`` and await the result.

        Raises :class:`WorkerNotConnectedError` if the Worker has no live
        session, and :class:`CommandTimedOutError` if no correlated result
        arrives before the deadline. Otherwise returns the :class:`CommandResult`
        (success or a typed failure).

        ``timeout_override`` replaces the adapter's default command deadline for
        this one dispatch (issue #822): the hydrate phase of a start gets a longer
        budget than the general command timeout because pulling a large working set
        routinely outlasts it. ``None`` keeps the default.

        ``snapshot_is_final`` marks the stop-flow final snapshot (issue #891): only
        a final snapshot wedges the row at (stopped, stopped, assigned), so only its
        timeout may promote a late-result record. Periodic/on-demand snapshots share
        the command type but take no worker reservation, so a stale late result must
        never clear a subsequent final-snapshot hold; they leave ``False``.
        """
