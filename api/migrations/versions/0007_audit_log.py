"""audit: audit_log

Creates the ``audit_log`` table exactly per docs/app/DATABASE.md Section 9: the
activity trail (FR-AUD-1) -- actor, Community, operation, target, outcome,
timestamp -- written fire-after-commit, must-not-raise (FR-AUD-2). ``actor_id``,
``community_id``, and ``target_id`` are plain nullable UUIDs with **no foreign
keys**: soft references, by design, so the row outlives the entities it describes
(Section 9, "No foreign keys on purpose"). ``outcome`` is the CHECK-constrained
``success`` / ``denied`` / ``error`` enum. Three indexes back the member-scoped
``(community_id, created_at)``, per-actor ``(actor_id, created_at)``, and
platform-admin ``(created_at)`` query paths (FR-AUD-3). Append-only.

Revision ID: 0007_audit_log
Revises: 0006_backups
Create Date: 2026-06-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_audit_log"
down_revision: str | None = "0006_backups"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        # No FKs: soft references so the trail outlives its subjects (Section 9).
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("community_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("target_type", sa.String(), nullable=True),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
        sa.CheckConstraint(
            "outcome IN ('success', 'denied', 'error')",
            name="ck_audit_log_outcome",
        ),
    )
    # Member-scoped, Community-bounded queries (FR-AUD-3).
    op.create_index(
        "ix_audit_log_community_id_created_at",
        "audit_log",
        ["community_id", "created_at"],
    )
    # "What did this user do" (FR-AUD-3).
    op.create_index(
        "ix_audit_log_actor_id_created_at",
        "audit_log",
        ["actor_id", "created_at"],
    )
    # The platform-admin global view (FR-AUD-3).
    op.create_index(
        "ix_audit_log_created_at",
        "audit_log",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_actor_id_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_community_id_created_at", table_name="audit_log")
    op.drop_table("audit_log")
