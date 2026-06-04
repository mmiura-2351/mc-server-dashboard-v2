"""SQLAlchemy ORM model for the ``audit_log`` table (DATABASE.md Section 9).

The activity trail: actor, Community, operation, target, outcome, timestamp.
``actor_id``, ``community_id``, and ``target_id`` are plain nullable UUIDs with
**no foreign keys** -- soft references, by design, so the row outlives the entities
it describes (Section 9). ``outcome`` is the CHECK-constrained enum. The three
indexes back the member-scoped, per-actor, and platform-admin query paths
(FR-AUD-3). Append-only: no update/delete mappings.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import CheckConstraint, DateTime, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from mc_server_dashboard_api.core.adapters.database import Base


class AuditLogModel(Base):
    """Row of the ``audit_log`` table (DATABASE.md Section 9)."""

    __tablename__ = "audit_log"
    __table_args__ = (
        # Bare name; the ``ck`` naming convention renders ``ck_audit_log_outcome``
        # (issue #60), matching the migration-created name.
        CheckConstraint(
            "outcome IN ('success', 'denied', 'error')",
            name="outcome",
        ),
        # Member-scoped, Community-bounded queries (FR-AUD-3).
        Index("ix_audit_log_community_id_created_at", "community_id", "created_at"),
        # "What did this user do" (FR-AUD-3).
        Index("ix_audit_log_actor_id_created_at", "actor_id", "created_at"),
        # The platform-admin global view (FR-AUD-3).
        Index("ix_audit_log_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    # No FKs: soft references so the trail outlives its subjects (Section 9).
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    community_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    operation: Mapped[str] = mapped_column(String, nullable=False)
    target_type: Mapped[str | None] = mapped_column(String, nullable=True)
    target_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
