"""Value objects for the audit trail (FR-AUD-1; DATABASE.md Section 9).

Pure, immutable, standard-library only. An :class:`AuditEvent` is what a caller
asks to record (actor, Community, operation, target, outcome) once a business
operation has committed; the writer stamps an id and the event time and persists
an :class:`AuditRecord`. ``actor_id``, ``community_id``, and ``target_id`` are
plain UUID values, not typed ids from other contexts: the trail is intentionally
decoupled from the entities it describes (no foreign keys; the row outlives them).

This module is importable from any layer -- like a value-object module -- so route
files at the edge can name :class:`AuditEvent` and :class:`Outcome` without
coupling their context's domain to the audit context.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from dataclasses import dataclass


class Outcome(enum.Enum):
    """The outcome of an audited operation (DATABASE.md Section 9 CHECK enum)."""

    SUCCESS = "success"
    DENIED = "denied"
    ERROR = "error"


@dataclass(frozen=True)
class AuditEvent:
    """A security- or state-relevant operation to record (FR-AUD-1).

    ``operation`` is an Appendix A ``<resource>:<action>`` code. ``actor_id`` is
    the acting user (``None`` for an unauthenticated actor, e.g. a failed login by
    an unknown user). ``community_id`` is the scope for member-scoped queries
    (``None`` for platform-level events). ``target_*`` identify the affected
    resource when there is one.
    """

    operation: str
    outcome: Outcome
    actor_id: uuid.UUID | None = None
    community_id: uuid.UUID | None = None
    target_type: str | None = None
    target_id: uuid.UUID | None = None


@dataclass(frozen=True)
class AuditRecord:
    """A persisted audit-log row (DATABASE.md Section 9)."""

    id: uuid.UUID
    operation: str
    outcome: Outcome
    created_at: dt.datetime
    actor_id: uuid.UUID | None = None
    community_id: uuid.UUID | None = None
    target_type: str | None = None
    target_id: uuid.UUID | None = None
