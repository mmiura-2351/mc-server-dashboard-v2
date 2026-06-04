"""The :class:`AuditWriter` Port (ARCHITECTURE.md Section 5.1; FR-AUD-2).

Fire-after-commit, must-not-raise. The contract is narrow: append one event to
the trail in the writer's own short transaction, independent of any business
``UnitOfWork``. The *must-not-raise* guarantee is the caller's edge concern (the
``AuditRecorder`` helper wraps every call); an adapter may raise on a real
persistence failure and the recorder logs and swallows it, so a broken trail
never rolls back or fails the operation it describes.
"""

from __future__ import annotations

from typing import Protocol

from mc_server_dashboard_api.audit.domain.events import AuditEvent


class AuditWriter(Protocol):
    """Appends a single :class:`AuditEvent` to the durable audit trail."""

    async def write(self, event: AuditEvent) -> None:
        """Persist ``event`` in the writer's own transaction (no shared UoW)."""
        ...
