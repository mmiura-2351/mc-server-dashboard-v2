"""SQLAlchemy ORM models for the resource pack tables (migration 0018).

Maps ``resource_packs`` (global resource pack metadata) and
``server_resource_pack_assignments`` (per-server assignment linking a server to
a resource pack). An adapter detail: the domain entities are framework-free and
are translated to/from these models in the repository. ``uploaded_by`` /
``assigned_by`` are plain UUIDs (no FK), so the rows survive the actor's
deletion.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from mc_server_dashboard_api.core.adapters.database import Base


class ResourcePackModel(Base):
    """Row of the ``resource_packs`` table (migration 0018)."""

    __tablename__ = "resource_packs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    sha1_hash: Mapped[str] = mapped_column(String(40), nullable=False)
    sha256_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # No FK: a soft reference so the row survives the actor's deletion.
    uploaded_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ServerResourcePackAssignmentModel(Base):
    """Row of the ``server_resource_pack_assignments`` table (migration 0018)."""

    __tablename__ = "server_resource_pack_assignments"
    __table_args__ = (
        Index("ix_srv_rp_assignments_resource_pack_id", "resource_pack_id"),
    )

    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("server.id", ondelete="CASCADE"),
        primary_key=True,
    )
    resource_pack_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "resource_packs.id",
            name="fk_srv_rp_assignments_resource_pack_id_resource_packs",
        ),
        nullable=False,
    )
    require_resource_pack: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    resource_pack_prompt: Mapped[str | None] = mapped_column(String, nullable=True)
    # No FK: a soft reference so the row survives the actor's deletion.
    assigned_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
