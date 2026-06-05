"""The must-not-raise :class:`AuditRecorder` adapter (FR-AUD-2).

Wraps an :class:`AuditWriter` and makes the recording call swallow-and-log every
failure: this is the single place the *must-not-raise* half of FR-AUD-2 lives, so
the writer adapter can stay a plain persistence component that may raise. Callers
invoke :meth:`record` after their UoW commit (fire-after-commit); any exception
from the writer -- a down database, a constraint slip -- is logged at ``error`` and
discarded, never propagated into the operation.
"""

from __future__ import annotations

import logging

from mc_server_dashboard_api.audit.domain.events import AuditEvent
from mc_server_dashboard_api.audit.domain.recorder import AuditRecorder
from mc_server_dashboard_api.audit.domain.writer import AuditWriter
from mc_server_dashboard_api.core.adapters.metrics import audit_write_failures_total

_logger = logging.getLogger(__name__)


class LoggingAuditRecorder(AuditRecorder):
    """:class:`AuditRecorder` that delegates to a writer and never raises."""

    def __init__(self, writer: AuditWriter) -> None:
        self._writer = writer

    async def record(self, event: AuditEvent) -> None:
        try:
            await self._writer.write(event)
        except Exception:
            # FR-AUD-2: a failed audit write must never raise into or roll back
            # the (already committed) operation. Log and swallow — and count it,
            # so a silently failing audit trail is observable via /metrics (#282).
            audit_write_failures_total.inc()
            _logger.error(
                "audit write failed",
                extra={
                    "operation": event.operation,
                    "outcome": event.outcome.value,
                },
                exc_info=True,
            )
