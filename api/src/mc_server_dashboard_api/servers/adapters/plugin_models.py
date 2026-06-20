"""SQLAlchemy ORM model for the ``server_plugin`` table (migration 0018).

Maps the ``server_plugin`` table: installed plugin/mod metadata for a server.
An adapter detail: the domain entity
(:class:`~mc_server_dashboard_api.servers.domain.plugin.ServerPlugin`) is
framework-free and is translated to/from this model in the repository. The
``server_id`` column FKs ``server`` (``ON DELETE CASCADE``); the
``loader_type`` and ``source`` CHECK constraints mirror the domain enums.
``installed_by`` is a plain nullable UUID (no FK), so the row survives the
actor's deletion.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from mc_server_dashboard_api.core.adapters.database import Base


class ServerPluginModel(Base):
    """Row of the ``server_plugin`` table (migration 0018)."""

    __tablename__ = "server_plugin"
    __table_args__ = (
        CheckConstraint(
            "loader_type IN ('mod', 'plugin')",
            name="ck_server_plugin_loader_type",
        ),
        CheckConstraint(
            "source IN ('local', 'modrinth')",
            name="ck_server_plugin_source",
        ),
        UniqueConstraint("server_id", "rel_path", name="uq_server_plugin_server_rel"),
        Index("ix_server_plugin_server_id", "server_id"),
        # Download-cache lookup: a Modrinth version's published sha512 -> cached
        # sha256 content address, so the same version is not re-downloaded per
        # server (issue #1306).
        Index("ix_server_plugin_checksum_sha512", "checksum_sha512"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("server.id", ondelete="CASCADE"),
        nullable=False,
    )
    rel_path: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    loader_type: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    source_project_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    version_number: Mapped[str | None] = mapped_column(String, nullable=True)
    checksum_sha512: Mapped[str | None] = mapped_column(String, nullable=True)
    # Content address for the content-addressed cache (issue #1306).
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    # No FK: a soft reference so the row survives the actor's deletion.
    installed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
