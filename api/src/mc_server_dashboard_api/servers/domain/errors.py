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


class ServerNotStoppedError(ServerError):
    """An operation requiring a fully stopped server ran against a live one.

    Config/name edits and deletion are allowed only while the server is at rest:
    ``desired_state == stopped`` and ``observed_state in {stopped, unknown}``
    (Section 6.9 spirit — avoid diverging from a live working set).
    """
