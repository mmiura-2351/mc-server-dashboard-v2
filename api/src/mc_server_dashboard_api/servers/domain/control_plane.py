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

from mc_server_dashboard_api.servers.domain.errors import ServerError
from mc_server_dashboard_api.servers.domain.value_objects import (
    CommunityId,
    ExecutionBackend,
    ServerId,
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


@dataclass(frozen=True)
class CommandOutcome:
    """The Worker's answer to a dispatched command.

    ``status`` is ``OK`` on success; any other value carries the Worker's failure
    classification. ``message`` is the human-readable detail; ``output`` is the
    console/RCON text of a ``server_command`` (empty otherwise); ``file_content``
    is the bytes read by a ``read_file`` (empty otherwise). The two payload fields
    are mutually exclusive per command, mirroring the wire ``CommandResult``
    ``result`` oneof.
    """

    status: CommandStatus
    message: str = ""
    output: str = ""
    file_content: bytes = b""

    @property
    def success(self) -> bool:
        return self.status is CommandStatus.OK


class ControlPlane(abc.ABC):
    """Port: the lifecycle layer's seam to placement + command dispatch."""

    @abc.abstractmethod
    async def place(self, *, backend: ExecutionBackend) -> WorkerId | None:
        """Choose an eligible Worker offering ``backend``, or ``None`` if none.

        ``None`` is the typed no-eligible-worker outcome (FR-WRK-3); the use case
        maps it to a transport error rather than treating placement failure as
        an exception.
        """

    @abc.abstractmethod
    def is_worker_connected(self, *, worker_id: WorkerId) -> bool:
        """Return whether ``worker_id`` currently has a live session (FR-WRK-2).

        The snapshot scheduler skips a server whose assigned Worker is gone
        rather than dispatching a doomed snapshot trigger; the server is
        re-evaluated on a later tick once the Worker reconnects (FR-DATA-5/7).
        """

    @abc.abstractmethod
    def increment_assignment(self, *, worker_id: WorkerId) -> None:
        """Record one more server placed on ``worker_id`` (placement load++)."""

    @abc.abstractmethod
    def decrement_assignment(self, *, worker_id: WorkerId) -> None:
        """Record one server removed from ``worker_id`` (placement load--)."""

    @abc.abstractmethod
    async def start(
        self,
        *,
        worker_id: WorkerId,
        server_id: ServerId,
        backend: ExecutionBackend,
        jar_relpath: str,
        minecraft_version: str,
    ) -> CommandOutcome:
        """Dispatch StartServer to ``worker_id`` and await the result (FR-SRV-2)."""

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
