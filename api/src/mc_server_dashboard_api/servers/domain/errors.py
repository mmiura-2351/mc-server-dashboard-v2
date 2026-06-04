"""Domain errors for the servers context.

Raised by the pure domain (value objects, entities, use-case policy) on invariant
or policy violations. They carry no framework type and are translated to transport
errors at the edge.
"""

from __future__ import annotations


class ServerError(Exception):
    """Base class for servers-domain invariant/policy violations."""


class InvalidServerNameError(ServerError):
    """A server name failed its validation rules (e.g. blank)."""


class InvalidServerFieldError(ServerError):
    """A required server text field (edition, version, type) was blank."""


class UnknownServerTypeError(ServerError):
    """The ``server_type`` is outside the supported M1 catalog (CHECK enum)."""


class UnknownExecutionBackendError(ServerError):
    """The ``execution_backend`` is not a known driver kind (CHECK enum, FR-EXE-2)."""


class ServerNotFoundError(ServerError):
    """The targeted server does not exist in the community.

    Raised by read/update/delete when the id is unknown or, security-critically,
    belongs to a *different* community (cross-community access): reported as
    not-found so no signal about another community's servers leaks (FR-COMM-3).
    """


class ServerNameAlreadyExistsError(ServerError):
    """Creation/rename hit the per-community server name uniqueness constraint."""


class ExecutionBackendImmutableError(ServerError):
    """An update attempted to change the execution backend.

    The backend is chosen at creation and is immutable for the server's lifetime
    in M1 (FR-EXE-3, ARCHITECTURE.md Section 7.1).
    """


class InvalidSnapshotIntervalError(ServerError):
    """A per-server snapshot-interval override was invalid (FR-DATA-7).

    The override (``config['snapshot_interval_seconds']``) must be a positive
    integer at least ``snapshot.min_interval_seconds`` (the thrash floor,
    CONFIGURATION.md Section 5.4). A non-integer or below-floor value is rejected;
    the edge maps this to 422.
    """


class ServerNotStoppedError(ServerError):
    """An operation requiring a fully stopped server ran against a live one.

    Config/name edits and deletion are allowed only while the server is at rest:
    ``desired_state == stopped`` and ``observed_state in {stopped, unknown}``
    (Section 6.9 spirit — avoid diverging from a live working set).
    """


class InvalidLifecycleTransitionError(ServerError):
    """A lifecycle op was requested against an incompatible desired state.

    Starting a server whose desired state is already ``running``, or
    stopping/restarting one whose desired state is ``stopped``, is a conflicting
    transition (FR-SRV-2). The edge maps this to 409.
    """


class LifecycleTransitionConflictError(ServerError):
    """A concurrent lifecycle transition lost a compare-and-set race.

    The in-memory transition check admitted the op, but the persisted
    compare-and-set (UPDATE ... WHERE desired_state = expected, plus any
    transition precondition) matched no row: another concurrent transition
    already moved the server out of the expected state. The use case aborts
    *before* dispatching or touching placement-load counts so a lost race causes
    no double placement/dispatch; the edge maps this to 409 ``transition_conflict``.
    """


class NoEligibleWorkerError(ServerError):
    """Placement found no Worker that can host the server (FR-WRK-3).

    No connected, non-draining Worker advertises the server's execution backend
    with free capacity. The edge maps this to a typed 409.
    """


class ServerNotRunningError(ServerError):
    """An RCON/console command targeted a server that is not observed running.

    Forwarding a console line is only meaningful for a live server
    (CONTROL_PLANE.md Section 7 ``INVALID_STATE``); the edge maps this to 409.
    """


class CommandDispatchError(ServerError):
    """A dispatched lifecycle/RCON command was refused by the Worker.

    The Worker returned a ``CommandResult`` failure (CONTROL_PLANE.md Section 7).
    For a start, the use case compensates the desired/assignment write before
    raising. The edge maps this to a typed 409.
    """
