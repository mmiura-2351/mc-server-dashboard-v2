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

# Sanitized start-failure categories the Worker classifies (issue #225/#824).
# Their status maps directly to the 409 body reason so an operator sees e.g.
# ``port_conflict`` instead of the generic ``command_failed`` -- without the raw
# daemon text (still log-only) leaking into the response.
#
# ``worker_busy`` (issue #867): the Worker refused the command without applying it
# because the id is not free yet -- either an in-flight reservation (a mutating
# lifecycle command already running on the Worker side) or a failed-stop orphan its
# converger is still resolving (issue #2476). Both are unsettled, and both mean the
# refused command succeeds on a later attempt, so the row keeps desired=running +
# the assignment and self-heals. Clients can distinguish this from ``server_busy``
# (#876), which is an API-side lifecycle lock contention (a gated op holds the lock
# past the acquire budget), and retry in a moment rather than treating it as a
# settled failure. It is not lifecycle-only: the backup-create route renders the
# same reason for a SnapshotTrigger the Worker refused with BUSY (issue #2436),
# which is the only sanitized status that kind can produce.
_SANITIZED_REASONS: dict[CommandStatus, str] = {
    CommandStatus.PORT_CONFLICT: "port_conflict",
    CommandStatus.IMAGE_MISSING: "image_missing",
    CommandStatus.BUSY: "worker_busy",
}

# The Worker refused the command because it holds a failed-stop orphan for the
# server: an instance whose driver Stop could not confirm termination, so the
# process may still be alive (issue #2466, Worker issue #251). It is NOT in
# ``_SANITIZED_REASONS``: that map is keyed by status alone, and ``INVALID_STATE``
# means different things per kind -- "already running" for a start or a hydrate,
# the orphan and nothing else for ``RestartServer`` / ``ServerCommand``, whose only
# other refusals carry different codes. So the two callers that can name it pass it
# explicitly.
#
# Only those two kinds carry this reason, and that asymmetry is deliberate (issue
# #2476). A restart or a console command over an orphan is refused for what the
# state IS and will not be carried out once the orphan converges, so naming the
# state is the honest answer. A start / hydrate / stopped-id snapshot over the same
# orphan WILL succeed once it converges, so the Worker answers ``BUSY`` for those
# and they render ``worker_busy`` above -- a retryable contention, not a verdict.
FAILED_STOP_ORPHAN_REASON = "failed_stop_orphan"


def dispatch_failure(
    *,
    server_id: ServerId,
    kind: str,
    outcome: CommandOutcome,
    reason: str | None = None,
) -> CommandDispatchError:
    """Log a failed command outcome at WARN and build the typed dispatch error.

    ``reason`` names the 409 body reason for a refusal the caller classified from
    the *kind* as well as the status (e.g. ``FAILED_STOP_ORPHAN_REASON``), which
    the status-keyed ``_SANITIZED_REASONS`` cannot express. It wins over that map;
    omitted, the map decides as before.
    """

    detail = outcome.message or outcome.status.value
    _LOG.warning(
        "command %s failed for server %s: %s",
        kind,
        server_id.value,
        detail,
    )
    return CommandDispatchError(
        detail, reason=reason or _SANITIZED_REASONS.get(outcome.status)
    )
