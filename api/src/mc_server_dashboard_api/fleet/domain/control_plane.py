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


@dataclass(frozen=True)
class CommandResult:
    """The Worker's answer to a dispatched command.

    ``code`` is ``OK`` on success; any other value carries the Worker's failure
    classification. ``message`` is the human-readable detail (empty on success).
    ``output`` carries the console/RCON text of a ``ServerCommand`` (empty
    otherwise).
    """

    code: CommandResultCode
    message: str = ""
    output: str = ""

    @property
    def success(self) -> bool:
        return self.code is CommandResultCode.OK


@dataclass(frozen=True)
class StartServerCommand:
    """Launch a server on the Worker (CONTROL_PLANE.md Section 5)."""

    driver: DriverKind
    jar_relpath: str
    minecraft_version: str


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


# The union of commands the lifecycle layer dispatches. Hydrate/snapshot and
# file access are deferred to later epics and intentionally not modelled here.
Command = (
    StartServerCommand | StopServerCommand | RestartServerCommand | ServerCommandCommand
)


class ControlPlane(abc.ABC):
    """Port: send an ``ApiCommand`` to a Worker and await its ``CommandResult``."""

    @abc.abstractmethod
    async def dispatch(
        self, *, worker_id: WorkerId, server_id: str, command: Command
    ) -> CommandResult:
        """Send ``command`` for ``server_id`` to ``worker_id`` and await the result.

        Raises :class:`WorkerNotConnectedError` if the Worker has no live
        session, and :class:`CommandTimedOutError` if no correlated result
        arrives before the deadline. Otherwise returns the :class:`CommandResult`
        (success or a typed failure).
        """
