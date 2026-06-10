"""SQLAlchemy ORM model for the ``backup`` table (DATABASE.md Section 8).

Maps the ``backup`` table: retained-snapshot metadata for a server, pointing at
the archive bytes in ``Storage`` by an opaque ``storage_ref``. An adapter detail:
the domain entity (:class:`~mc_server_dashboard_api.servers.domain.backup.Backup`)
is framework-free and is translated to/from this model in the repository. The
``server_id`` column FKs ``server`` (``ON DELETE CASCADE``); the ``source`` CHECK
mirrors DATABASE.md's enum. ``created_by`` is a plain nullable UUID (no FK), so the
row survives the actor's deletion (the audit trail is the durable actor record,
Section 9) and a scheduled backup with no actor records ``NULL``.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from mc_server_dashboard_api.core.adapters.database import Base

_BACKUP_SOURCES = ("manual", "scheduled", "event", "uploaded")


class BackupModel(Base):
    """Row of the ``backup`` table (DATABASE.md Section 8)."""

    __tablename__ = "backup"
    __table_args__ = (
        CheckConstraint(
            "source IN ('manual', 'scheduled', 'event', 'uploaded')",
            name="ck_backup_source",
        ),
        CheckConstraint(
            "health IN ('healthy', 'quarantined', 'unknown')",
            name="ck_backup_health",
        ),
        # List a server's backups newest-first (DATABASE.md Section 8).
        Index("ix_backup_server_id_created_at", "server_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("server.id", ondelete="CASCADE"),
        nullable=False,
    )
    storage_ref: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    # Structural health of the archived contents (issue #742): 'healthy' when
    # written through the integrity-gated create path, 'unknown' for legacy and
    # uploaded rows until the sweep (#744) classifies them, 'quarantined' if found
    # corrupt. Defaulted at the column so an INSERT that omits it lands 'unknown'.
    health: Mapped[str] = mapped_column(
        String, nullable=False, server_default="unknown"
    )
    # No FK: a soft reference so the row survives the actor's deletion (Section 9).
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
