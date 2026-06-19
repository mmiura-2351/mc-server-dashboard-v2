"""SQLAlchemy ORM model for the ``server_mods`` table (migration 0020).

Maps ``server_mods`` (server↔mod assignment, many-to-many). An adapter detail: the
framework-free domain entity (:class:`~...servers.domain.server_mod.
ServerModAssignment`) is translated to/from this model in the repository.
``server_id`` FKs ``server`` (``ON DELETE CASCADE``); ``mod_id`` FKs ``mods``.
``assigned_by`` is a plain UUID (no FK), so the row survives the actor's deletion.
``UNIQUE(server_id, mod_id)`` enforces one assignment per (server, mod), and an
index on ``server_id`` backs listing a server's mod set. Constraint/index names
match migration 0020 so Alembic autogenerate stays quiet (issue #60).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from mc_server_dashboard_api.core.adapters.database import Base


class ServerModModel(Base):
    """Row of the ``server_mods`` table (migration 0020)."""

    __tablename__ = "server_mods"
    __table_args__ = (
        UniqueConstraint("server_id", "mod_id", name="uq_server_mods_server_id_mod_id"),
        Index("ix_server_mods_server_id", "server_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "server.id",
            name="fk_server_mods_server_id_server",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    mod_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mods.id", name="fk_server_mods_mod_id_mods"),
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    # No FK: a soft reference so the row survives the actor's deletion.
    assigned_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
