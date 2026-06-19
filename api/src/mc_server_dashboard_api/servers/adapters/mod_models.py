"""SQLAlchemy ORM model for the ``mods`` table (migration 0019).

Maps ``mods`` (global mod library metadata, one row per uploaded jar). An adapter
detail: the framework-free domain entity (:class:`~...servers.domain.mod.Mod`) is
translated to/from this model in the repository. ``uploaded_by`` is a plain UUID
(no FK), so the row survives the actor's deletion. The CHECK constraints mirror
the enum-like columns; the unique index on ``sha256_hash`` enforces the
content-address dedup. Constraint/index names match migration 0019 so Alembic
autogenerate stays quiet (issue #60).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from mc_server_dashboard_api.core.adapters.database import Base

_LOADER_TYPES = ("fabric", "forge", "neoforge", "quilt", "paper")
_SIDES = ("server", "client", "both")
_SOURCES = ("local", "modrinth")


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class ModModel(Base):
    """Row of the ``mods`` table (migration 0019)."""

    __tablename__ = "mods"
    __table_args__ = (
        CheckConstraint(
            _in_clause("loader_type", _LOADER_TYPES), name="ck_mods_loader_type"
        ),
        CheckConstraint(_in_clause("side", _SIDES), name="ck_mods_side"),
        CheckConstraint(_in_clause("source", _SOURCES), name="ck_mods_source"),
        # Content-address dedup: an identical jar resolves to the existing entry.
        Index("uq_mods_sha256_hash", "sha256_hash", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    loader_type: Mapped[str] = mapped_column(String, nullable=False)
    mod_identifier: Mapped[str] = mapped_column(String, nullable=False)
    provides: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    version_number: Mapped[str] = mapped_column(String, nullable=False)
    mc_versions: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False, server_default="both")
    dependencies: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    sha256_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sha512_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    source_project_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_version_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # No FK: a soft reference so the row survives the actor's deletion.
    uploaded_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
