"""The servers-side control-plane seam (the lifecycle layer's view of the fleet).

The lifecycle use cases must place a server on a Worker, dispatch lifecycle /
RCON commands to it, and adjust the Worker's placement load — all fleet concerns.
The servers domain and application may not import the fleet context (import-linter
contract), so they depend on this narrow Port; the wiring binds it to a fleet
adapter that drives the real ``WorkerRegistry`` and ``ControlPlane`` (mirroring
how the server-delete grant sweep is composed at the adapter layer).

The Port speaks the servers domain's own types: a :class:`WorkerId` value, the
:class:`ExecutionBackend` enum (the adapter maps its underscore spelling to the
fleet ``DriverKind`` hyphen spelling), and a plain :class:`CommandOutcome`. No
fleet type crosses the seam.
"""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass

from mc_server_dashboard_api.servers.domain.committed_resources import (
    CommittedResources,
)
from mc_server_dashboard_api.servers.domain.errors import ServerError
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ExecutionBackend,
    ServerId,
    ServerType,
    WorkerId,
)


class WorkerUnavailableError(ServerError):
    """The assigned Worker has no live session to dispatch a command on.

    Raised by the control-plane seam when the target Worker is not connected
    (never connected, disconnected, or its command timed out). The lifecycle use
    case surfaces this so the edge can return a typed transport error.
    """


class CommandStatus(enum.Enum):
    """The outcome class of a dispatched command (mirrors the wire result code)."""

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
    # INVALID_STATE: the in-flight command's outcome is unknown, so the lifecycle
    # layer keeps the assignment/intent and retries rather than converging a state.
    BUSY = "busy"


class FileAccessReason(enum.Enum):
    """Refines a ``FILE_ACCESS_DENIED`` outcome (issue #548).

    The Worker collapses several distinct conditions into ``FILE_ACCESS_DENIED``;
    this carries which one so the file use cases map each to an honest problem
    reason instead of a blanket ``invalid_path``. Same names as the wire/fleet
    enum, a distinct type on this side of the seam. ``UNSPECIFIED`` is a genuine
    path denial (the historical 422 ``invalid_path``).
    """

    UNSPECIFIED = "unspecified"
    IS_A_DIRECTORY = "is_a_directory"
    NOT_A_DIRECTORY = "not_a_directory"
    SYMLINK_REFUSED = "symlink_refused"
    PAYLOAD_TOO_LARGE = "payload_too_large"


@dataclass(frozen=True)
class FileEntry:
    """One child of a running server's directory listing (file:read).

    The shape mirrors the authoritative-Storage listing (name / is_dir / size) so
    a running-server listing unifies with an at-rest one (Section 6.9).
    """

    name: str
    is_dir: bool
    size: int


@dataclass(frozen=True)
class FileListing:
    """A directory listing returned by a ``list_files`` dispatch.

    ``truncated`` is set when the directory held more entries than the Worker's
    per-listing cap and ``entries`` was clipped to that cap.
    """

    entries: tuple[FileEntry, ...] = ()
    truncated: bool = False


@dataclass(frozen=True)
class CommandOutcome:
    """The Worker's answer to a dispatched command.

    ``status`` is ``OK`` on success; any other value carries the Worker's failure
    classification. ``message`` is the human-readable detail; ``output`` is the
    console/RCON text of a ``server_command`` (empty otherwise); ``file_content``
    is the bytes read by a ``read_file`` (empty otherwise); ``listing`` is the
    directory listing of a ``list_files`` (``None`` otherwise). The payload fields
    are mutually exclusive per command, mirroring the wire ``CommandResult``
    ``result`` oneof.
    """

    status: CommandStatus
    message: str = ""
    output: str = ""
    file_content: bytes = b""
    listing: FileListing | None = None
    # Refines a FILE_ACCESS_DENIED status (issue #548); UNSPECIFIED otherwise.
    file_access_reason: FileAccessReason = FileAccessReason.UNSPECIFIED

    @property
    def success(self) -> bool:
        return self.status is CommandStatus.OK


class ControlPlane(abc.ABC):
    """Port: the lifecycle layer's seam to placement + command dispatch."""

    @abc.abstractmethod
    async def place(
        self,
        *,
        server_id: ServerId,
        backend: ExecutionBackend,
        memory_limit_mb: int | None,
        committed_by_worker: dict[WorkerId, CommittedResources],
    ) -> WorkerId | None:
        """Choose an eligible Worker offering ``backend``, or ``None`` if none.

        Resource-aware placement (#710): ``memory_limit_mb`` is the new server's
        declared memory request (``None`` = unset, not memory-gated), and
        ``committed_by_worker`` is the commit-based per-worker accounting the
        application layer summed from the assigned servers (it owns the DB read;
        this seam stays free of persistence). The adapter merges these with the
        registry's advertised capacity and feeds the pure ``place`` function.

        On a successful choice the adapter RESERVES ``server_id`` on the chosen
        Worker atomically (#778), in the same await-free section that read the
        candidates' load, so a concurrent placement sees the slot taken and two
        starts cannot both claim a Worker's last capacity slot. The caller MUST
        later either confirm the reservation (:meth:`increment_assignment` after
        the lifecycle commit) or release it (:meth:`release_reservation` if the
        commit is lost).

        ``None`` is the typed no-eligible-worker outcome (FR-WRK-3); the use case
        maps it to a transport error rather than treating placement failure as
        an exception. No reservation is made when ``None`` is returned.
        """

    @abc.abstractmethod
    def is_worker_connected(self, *, worker_id: WorkerId) -> bool:
        """Return whether ``worker_id`` currently has a live session (FR-WRK-2).

        The snapshot scheduler skips a server whose assigned Worker is gone
        rather than dispatching a doomed snapshot trigger; the server is
        re-evaluated on a later tick once the Worker reconnects (FR-DATA-5/7).
        """

    @abc.abstractmethod
    def held_generation(
        self, *, worker_id: WorkerId, server_id: ServerId
    ) -> int | None:
        """Return the generation ``worker_id`` reported holding for ``server_id``.

        Answers from the held-working-set inventory the Worker advertised on its
        current registration (issue #763). The lifecycle layer consults it on a
        same-worker restart (``redispatch_start``): it skips the destructive hydrate
        only when the held generation is at least the authoritative store generation
        (the Worker's scratch is at least as fresh as the store, so hydrating would
        clobber the newer scratch with the last snapshot). ``None`` for a
        disconnected/unknown Worker, and ``None`` once the Worker re-registers
        without that id (e.g. its scratch was wiped or GC'd) — so the start hydrates
        rather than booting an empty/absent working set. A held generation older than
        the store generation likewise hydrates (presence at a stale generation, e.g.
        an A->B->A leftover scratch).
        """

    @abc.abstractmethod
    def increment_assignment(self, *, worker_id: WorkerId, server_id: ServerId) -> None:
        """Confirm ``server_id``'s reservation as a committed placement (#778).

        Called after the lifecycle commit lands. Converts the reservation that
        :meth:`place` made into a committed assignment; placement load is unchanged
        (the reservation already counted it). A no-op if the reservation is gone
        because a reconnect rebuild already counted the committed row.
        """

    @abc.abstractmethod
    def release_reservation(self, *, worker_id: WorkerId, server_id: ServerId) -> None:
        """Release ``server_id``'s reservation when its commit is lost (#778).

        Called when the placement fails BEFORE the lifecycle commit (a lost
        compare-and-set), freeing the tentatively-held slot without it ever
        counting as a committed assignment.
        """

    @abc.abstractmethod
    def decrement_assignment(self, *, worker_id: WorkerId, server_id: ServerId) -> None:
        """Record ``server_id`` removed from ``worker_id`` (load--, committed mem--).

        Carries ``server_id`` so the registry sheds that assignment's declared memory
        with its count (#843), keeping the committed-memory axis in step.
        """

    @abc.abstractmethod
    async def start(
        self,
        *,
        worker_id: WorkerId,
        server_id: ServerId,
        backend: ExecutionBackend,
        server_type: ServerType,
        jar_relpath: str,
        minecraft_version: str,
        memory_limit_bytes: int,
        cpu_millis: int,
    ) -> CommandOutcome:
        """Dispatch StartServer to ``worker_id`` and await the result (FR-SRV-2).

        ``server_type`` selects the Worker launch mode: ``forge`` launches via the
        supervised installer + args file, every other type via the historical JAR
        launch (issue #307).

        ``memory_limit_bytes`` is the per-server memory ceiling (issue #706); 0
        means unset, so the Worker driver picks a default heap.

        ``cpu_millis`` is the per-server CPU allocation in millicores (issue #723);
        0 means unset, so the Worker driver applies its default weight.
        """

    @abc.abstractmethod
    async def stop(
        self, *, worker_id: WorkerId, server_id: ServerId, force: bool = False
    ) -> CommandOutcome:
        """Dispatch StopServer (graceful unless ``force``) and await the result."""

    @abc.abstractmethod
    async def restart(
        self, *, worker_id: WorkerId, server_id: ServerId
    ) -> CommandOutcome:
        """Dispatch RestartServer to ``worker_id`` and await the result."""

    @abc.abstractmethod
    async def command(
        self, *, worker_id: WorkerId, server_id: ServerId, line: str
    ) -> CommandOutcome:
        """Forward an RCON/console line and await the output (FR-SRV-5)."""

    @abc.abstractmethod
    async def hydrate(
        self, *, worker_id: WorkerId, community_id: CommunityId, server_id: ServerId
    ) -> CommandOutcome:
        """Trigger a working-set hydrate before launch and await it (FR-DATA-4).

        The adapter addresses the data-plane endpoint for ``(community_id,
        server_id)`` and hands the Worker the URL + a short-lived token; the bulk
        bytes move off the control-plane stream (CONTROL_PLANE.md Section 5).
        """

    @abc.abstractmethod
    async def snapshot(
        self, *, worker_id: WorkerId, community_id: CommunityId, server_id: ServerId
    ) -> CommandOutcome:
        """Trigger a working-set snapshot and await it (FR-DATA-4, FR-DATA-7)."""

    @abc.abstractmethod
    async def read_file(
        self, *, worker_id: WorkerId, server_id: ServerId, rel_path: str
    ) -> CommandOutcome:
        """Read ``rel_path`` from a running server's live working set (Section 6.9).

        The bytes ride the result's ``file_content``; path-traversal protection is
        enforced on the Worker side (FR-FILE-4), so a rejected path comes back as
        a ``FILE_ACCESS_DENIED`` outcome rather than an exception.
        """

    @abc.abstractmethod
    async def edit_file(
        self,
        *,
        worker_id: WorkerId,
        server_id: ServerId,
        rel_path: str,
        content: bytes,
    ) -> CommandOutcome:
        """Write ``content`` to ``rel_path`` in a running server's working set."""

    @abc.abstractmethod
    async def list_files(
        self, *, worker_id: WorkerId, server_id: ServerId, rel_path: str
    ) -> CommandOutcome:
        """List ``rel_path`` in a running server's live working set (Section 6.9).

        The entries ride the outcome's ``listing``; path-traversal protection is
        enforced on the Worker side (FR-FILE-4), so a rejected path comes back as
        a ``FILE_ACCESS_DENIED`` outcome rather than an exception. The listing is
        read-only.
        """
