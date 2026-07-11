"""scheduler: schedule + schedule_run

Creates the general scheduler's persistence (epic #649, issue #1835):

- ``schedule`` — a per-server recurring action (command / start / stop /
  restart / backup) firing on a cron expression XOR a fixed interval
  (``ck_schedule_cadence_xor``), in a per-schedule IANA timezone. The
  ``server_id`` FK is ``ON DELETE CASCADE``; ``UNIQUE(server_id, name)`` also
  serves as the ``server_id`` index (leading column). The partial index on
  ``next_run_at WHERE enabled`` backs the runner's due poll (#1838);
  ``next_run_at`` is NULL exactly while disabled. ``created_by`` is a plain
  nullable UUID with **no FK** — a soft reference so the row survives the
  actor's deletion (the ``backup.created_by`` posture, DATABASE.md Section 9).
- ``schedule_run`` — one recorded execution per fired occurrence, with the
  ``success`` / ``failure`` / ``skipped`` outcome CHECK. The ``schedule_id`` FK
  is ``ON DELETE CASCADE``, indexed with ``started_at`` for the newest-first
  history listing.

Revision ID: 0029_schedules
Revises: 0028_join_table_indexes
Create Date: 2026-07-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0029_schedules"
down_revision: str | None = "0028_join_table_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "schedule",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("server_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("cron", sa.String(), nullable=True),
        sa.Column("interval_seconds", sa.Integer(), nullable=True),
        sa.Column("timezone", sa.String(), nullable=False, server_default="UTC"),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        # No FK: a soft reference so the row survives the actor's deletion
        # (DATABASE.md Section 8/9).
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_schedule"),
        sa.ForeignKeyConstraint(
            ["server_id"],
            ["server.id"],
            name="fk_schedule_server_id_server",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("server_id", "name", name="uq_schedule_server_id_name"),
        sa.CheckConstraint(
            "action IN ('command', 'start', 'stop', 'restart', 'backup')",
            name="ck_schedule_action",
        ),
        # Exactly one of cron / interval_seconds is set (cron XOR interval).
        sa.CheckConstraint(
            "(cron IS NULL) != (interval_seconds IS NULL)",
            name="ck_schedule_cadence_xor",
        ),
    )
    # The runner's due poll: only enabled schedules carry a next_run_at.
    op.create_index(
        "ix_schedule_next_run_at",
        "schedule",
        ["next_run_at"],
        postgresql_where=sa.text("enabled"),
    )
    op.create_table(
        "schedule_run",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schedule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("detail", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_schedule_run"),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["schedule.id"],
            name="fk_schedule_run_schedule_id_schedule",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "outcome IN ('success', 'failure', 'skipped')",
            name="ck_schedule_run_outcome",
        ),
    )
    # List a schedule's runs newest-first; the leading column also serves the
    # FK cascade lookup.
    op.create_index(
        "ix_schedule_run_schedule_id_started_at",
        "schedule_run",
        ["schedule_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_schedule_run_schedule_id_started_at", table_name="schedule_run")
    op.drop_table("schedule_run")
    op.drop_index("ix_schedule_next_run_at", table_name="schedule")
    op.drop_table("schedule")
