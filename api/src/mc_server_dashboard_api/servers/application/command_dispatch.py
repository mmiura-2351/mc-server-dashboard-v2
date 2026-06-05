"""Shared handling for failed control-plane command outcomes (issue #194/#200).

Every ``CommandDispatchError(outcome.message or outcome.status.value)`` raise in
the application layer flows through :func:`dispatch_failure`, so the Worker's
failure detail is recorded once at WARN — with server and command-kind context —
before the edge maps the error to a generic 409. The raw Worker message stays out
of the HTTP response (it can leak Worker host paths), so it is logged, not
returned. Lives in its own module so every use case (lifecycle, backups, files,
snapshot scheduler) shares it without a cross-module private import.
"""

from __future__ import annotations

import logging

from mc_server_dashboard_api.servers.domain.control_plane import (
    CommandOutcome,
    CommandStatus,
)
from mc_server_dashboard_api.servers.domain.errors import CommandDispatchError
from mc_server_dashboard_api.servers.domain.value_objects import ServerId

_LOG = logging.getLogger(__name__)

# Sanitized start-failure categories the Worker classifies (issue #225). Their
# status maps directly to the 409 body reason so an operator sees e.g.
# ``port_conflict`` instead of the generic ``command_failed`` -- without the raw
# daemon text (still log-only) leaking into the response.
_SANITIZED_REASONS: dict[CommandStatus, str] = {
    CommandStatus.PORT_CONFLICT: "port_conflict",
    CommandStatus.IMAGE_MISSING: "image_missing",
}


def dispatch_failure(
    *, server_id: ServerId, kind: str, outcome: CommandOutcome
) -> CommandDispatchError:
    """Log a failed command outcome at WARN and build the typed dispatch error."""

    detail = outcome.message or outcome.status.value
    _LOG.warning(
        "command %s failed for server %s: %s",
        kind,
        server_id.value,
        detail,
    )
    return CommandDispatchError(detail, reason=_SANITIZED_REASONS.get(outcome.status))
