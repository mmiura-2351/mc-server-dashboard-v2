"""SQLAlchemy ORM model for the ``server`` table (DATABASE.md Section 7).

Maps the ``server`` table. An adapter detail: the domain entity
(:class:`~mc_server_dashboard_api.servers.domain.entities.Server`) is
framework-free and is translated to/from this model in the repository. The
``community_id`` column FKs the community ``community`` table at the persistence
layer (``ON DELETE CASCADE``); ``assigned_worker_id`` is a plain nullable UUID —
the ``worker`` table is not yet a persisted relation (the fleet registry is
in-memory), so DATABASE.md's ``ON DELETE SET NULL`` FK to ``worker.id`` lands
when that table does. The CHECK constraints mirror DATABASE.md's enum columns.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from mc_server_dashboard_api.core.adapters.database import Base

_SERVER_TYPES = ("vanilla", "paper", "forge")
_EXECUTION_BACKENDS = ("host_process", "container")
_DESIRED_STATES = ("running", "stopped")
_OBSERVED_STATES = (
    "starting",
    "running",
    "stopping",
    "stopped",
    "restarting",
    "crashed",
    "unknown",
)


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class ServerModel(Base):
    """Row of the ``server`` table (DATABASE.md Section 7)."""

    __tablename__ = "server"
    __table_args__ = (
        # A server name is unique within its community (DATABASE.md Section 7).
        UniqueConstraint("community_id", "name", name="uq_server_community_name"),
        CheckConstraint(
            _in_clause("server_type", _SERVER_TYPES), name="ck_server_type"
        ),
        CheckConstraint(
            _in_clause("execution_backend", _EXECUTION_BACKENDS),
            name="ck_server_execution_backend",
        ),
        CheckConstraint(
            _in_clause("desired_state", _DESIRED_STATES),
            name="ck_server_desired_state",
        ),
        CheckConstraint(
            _in_clause("observed_state", _OBSERVED_STATES),
            name="ck_server_observed_state",
        ),
        # Index on (assigned_worker_id) for "all servers on Worker X" (FR-WRK-4).
        Index("ix_server_assigned_worker_id", "assigned_worker_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    community_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("community.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    mc_edition: Mapped[str] = mapped_column(String, nullable=False)
    mc_version: Mapped[str] = mapped_column(String, nullable=False)
    server_type: Mapped[str] = mapped_column(String, nullable=False)
    execution_backend: Mapped[str] = mapped_column(String, nullable=False)
    config: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    desired_state: Mapped[str] = mapped_column(String, nullable=False)
    observed_state: Mapped[str] = mapped_column(String, nullable=False)
    observed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # No FK: the worker table is not yet persisted (DATABASE.md Section 7 note).
    assigned_worker_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
