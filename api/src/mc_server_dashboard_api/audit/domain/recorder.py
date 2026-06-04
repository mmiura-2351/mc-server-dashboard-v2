"""The :class:`AuditRecorder` Port: the must-not-raise recording seam (FR-AUD-2).

This is the *one* explicit mechanism callers use to record an event. Routes (and
use cases, where they own the post-commit point) depend on this Port and call it
**after** their business transaction has committed, with a typed
:class:`AuditEvent`. The guarantee is in the name: :meth:`record` must never raise
-- a failed audit write is logged and swallowed by the adapter, so a broken trail
can neither roll back nor fail the operation it describes. No middleware magic;
the call is explicit at each recording point.
"""

from __future__ import annotations

from typing import Protocol

from mc_server_dashboard_api.audit.domain.events import AuditEvent


class AuditRecorder(Protocol):
    """Records an :class:`AuditEvent` fire-after-commit, must-not-raise."""

    async def record(self, event: AuditEvent) -> None:
        """Append ``event`` to the trail; never raise on a write failure."""
        ...
