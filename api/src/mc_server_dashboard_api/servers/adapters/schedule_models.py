"""SQLAlchemy ORM models for the ``schedule`` / ``schedule_run`` tables (#1835).

Map the general scheduler's persistence (DATABASE.md Section 8): a per-server
recurring action and its execution history. An adapter detail: the domain
entities (:class:`~mc_server_dashboard_api.servers.domain.schedule.Schedule` /
``ScheduleRun``) are framework-free and are translated to/from these models in
the repository — the typed payload fields (command line, warning steps) are
serialized into the ``payload`` jsonb there.

``schedule.server_id`` FKs ``server`` (``ON DELETE CASCADE``); its lookups ride
the ``UNIQUE(server_id, name)`` index (``server_id`` leading), so no separate
single-column index is needed. The cadence is cron XOR interval, pinned by the
``ck_schedule_cadence_xor`` CHECK. The partial index on ``next_run_at WHERE
enabled`` is the runner's due-schedule poll. ``created_by`` is a plain nullable
UUID (no FK) so the row survives the actor's deletion (the ``backup`` posture).
``schedule_run.schedule_id`` FKs ``schedule`` (``ON DELETE CASCADE``), indexed
with ``started_at`` for the newest-first history listing.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from mc_server_dashboard_api.core.adapters.database import Base


class ScheduleModel(Base):
    """Row of the ``schedule`` table (DATABASE.md Section 8)."""

    __tablename__ = "schedule"
    __table_args__ = (
        UniqueConstraint("server_id", "name", name="uq_schedule_server_id_name"),
        CheckConstraint(
            "action IN ('command', 'start', 'stop', 'restart', 'backup')",
            name="ck_schedule_action",
        ),
        # Exactly one of cron / interval_seconds is set (cron XOR interval).
        CheckConstraint(
            "(cron IS NULL) != (interval_seconds IS NULL)",
            name="ck_schedule_cadence_xor",
        ),
        # The runner's due poll: only enabled schedules carry a next_run_at.
        Index(
            "ix_schedule_next_run_at",
            "next_run_at",
            postgresql_where=text("enabled"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("server.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    cron: Mapped[str | None] = mapped_column(String, nullable=True)
    interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timezone: Mapped[str] = mapped_column(String, nullable=False, server_default="UTC")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # NULL exactly while disabled; the due instant the runner polls on otherwise.
    next_run_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_run_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # No FK: a soft reference so the row survives the actor's deletion (Section 9).
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ScheduleRunModel(Base):
    """Row of the ``schedule_run`` table (DATABASE.md Section 8)."""

    __tablename__ = "schedule_run"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('success', 'failure', 'skipped')",
            name="ck_schedule_run_outcome",
        ),
        # List a schedule's runs newest-first; the leading column also serves
        # the FK cascade lookup.
        Index(
            "ix_schedule_run_schedule_id_started_at",
            "schedule_id",
            "started_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    schedule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("schedule.id", ondelete="CASCADE"),
        nullable=False,
    )
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    # Sanitized outcome note (a failure category, a skip reason); never a raw
    # worker/OS message.
    detail: Mapped[str | None] = mapped_column(String, nullable=True)
